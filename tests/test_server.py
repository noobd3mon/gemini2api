"""Server auth, rate limiting, admin route, and usage tests."""
import json
import unittest

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.server import (GeminiHandler, _check_rate, _usage,
                                   _rate_buckets, _request_counts, _request_counter_snapshot)


class StubHandler(GeminiHandler):
    """Handler with injected headers/path and no socket; send_json is patched per-test."""

    def __init__(self, headers=None, path="/v1/chat/completions"):
        from email.message import Message
        m = Message()
        for k, v in (headers or {}).items():
            m[k] = v
        self.headers = m  # case-insensitive, like BaseHTTPRequestHandler's
        self.path = path


class AuthTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)

    def test_no_keys_open(self):
        self.assertTrue(StubHandler()._authorized())

    def test_bearer(self):
        CONFIG["api_keys"] = ["sk-a", "sk-b"]
        self.assertTrue(StubHandler({"Authorization": "Bearer sk-b"})._authorized())
        self.assertFalse(StubHandler({"Authorization": "Bearer sk-x"})._authorized())
        self.assertFalse(StubHandler({})._authorized())

    def test_header_keys(self):
        CONFIG["api_keys"] = ["sk-a"]
        self.assertTrue(StubHandler({"x-api-key": "sk-a"})._authorized())
        self.assertTrue(StubHandler({"x-goog-api-key": "sk-a"})._authorized())

    def test_query_key(self):
        CONFIG["api_keys"] = ["sk-q"]
        h = StubHandler(path="/v1/chat/completions?key=sk-q&other=1")
        self.assertTrue(h._authorized())
        self.assertFalse(StubHandler(path="/v1/chat/completions?key=nope")._authorized())

    def test_admin_key(self):
        CONFIG["admin_key"] = "adm"
        self.assertTrue(StubHandler({"x-admin-key": "adm"})._admin_authorized())
        self.assertTrue(StubHandler({"Authorization": "Bearer adm"})._admin_authorized())
        self.assertFalse(StubHandler({"x-admin-key": "nope"})._admin_authorized())
        self.assertFalse(StubHandler({})._admin_authorized())

    def test_admin_falls_back_to_api_keys(self):
        CONFIG["api_keys"] = ["sk-a"]
        self.assertTrue(StubHandler({"Authorization": "Bearer sk-a"})._admin_authorized())
        self.assertFalse(StubHandler({})._admin_authorized())


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        _rate_buckets.clear()
        _request_counts.clear()

    def tearDown(self):
        _rate_buckets.clear()
        _request_counts.clear()

    def test_disabled_by_default(self):
        allowed, retry = _check_rate("k")
        self.assertTrue(allowed)
        self.assertEqual(retry, 0)

    def test_token_bucket(self):
        CONFIG["rate_limit"] = 3
        for _ in range(3):
            allowed, _ = _check_rate("k")
            self.assertTrue(allowed)
        allowed, retry = _check_rate("k")
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)
        self.assertEqual(_request_counter_snapshot()["k"], {"total": 4, "rejected": 1})

    def test_buckets_are_per_key(self):
        CONFIG["rate_limit"] = 1
        self.assertTrue(_check_rate("a")[0])
        self.assertTrue(_check_rate("b")[0])
        self.assertFalse(_check_rate("a")[0])


class AdminCookieTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG["admin_key"] = "adm"

    def call(self, headers, body):
        h = StubHandler(headers, path="/admin/cookie")
        captured = []
        h.send_json = lambda data, status=200: captured.append((data, status))
        h._handle_admin_cookie(body.encode())
        return captured

    def test_applies_payload(self):
        captured = self.call(
            {"x-admin-key": "adm"},
            json.dumps({"cookie": "a=1; SAPISID=s1", "sapisid": "s1",
                        "auth_user": "2", "xsrf_token": "tok", "gemini_bl": "boq_assistant-bard-web-server_2099.01_p1"}))
        self.assertEqual(captured[-1][1], 200)
        self.assertEqual(CONFIG["cookie"], "a=1; SAPISID=s1")
        self.assertEqual(CONFIG["sapisid"], "s1")
        self.assertEqual(CONFIG["auth_user"], "2")
        self.assertEqual(CONFIG["xsrf_token"], "tok")
        self.assertEqual(CONFIG["gemini_bl"], "boq_assistant-bard-web-server_2099.01_p1")
        # the response reports state but never the cookie value itself
        self.assertNotIn("a=1", json.dumps(captured[-1][0]))

    def test_rejects_bad_bl(self):
        self.call({"x-admin-key": "adm"}, json.dumps({"cookie": "a=1", "gemini_bl": "not-a-bl"}))
        self.assertNotEqual(CONFIG["gemini_bl"], "not-a-bl")

    def test_requires_cookie(self):
        captured = self.call({"x-admin-key": "adm"}, json.dumps({"sapisid": "s"}))
        self.assertEqual(captured[-1][1], 400)

    def test_rejects_bad_auth(self):
        captured = self.call({"x-admin-key": "nope"}, json.dumps({"cookie": "a=1"}))
        self.assertEqual(captured[-1][1], 401)

    def test_rejects_invalid_json(self):
        captured = self.call({"x-admin-key": "adm"}, "{not json")
        self.assertEqual(captured[-1][1], 400)


class UsageTest(unittest.TestCase):
    def test_estimate(self):
        usage = _usage("x" * 40, "y" * 20)
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


if __name__ == "__main__":
    unittest.main()