"""Cookie parsing tests."""
import unittest

from gemini_web2api.gemini import _parse_sapisid, _cookie_from_string
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG


class CookieTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)

    def test_parse_sapisid(self):
        self.assertEqual(_parse_sapisid("a=1; SAPISID=xyz123; b=2"), "xyz123")

    def test_parse_sapisid_3p_fallback(self):
        self.assertEqual(_parse_sapisid("a=1; __Secure-3PAPISID=abc"), "abc")

    def test_parse_sapisid_missing(self):
        self.assertEqual(_parse_sapisid("a=1; b=2"), "")

    def test_cookie_from_raw_header(self):
        text, sapisid = _cookie_from_string("a=1; SAPISID=xyz")
        self.assertEqual(text, "a=1; SAPISID=xyz")
        self.assertEqual(sapisid, "xyz")

    def test_cookie_from_quoted_header(self):
        text, _ = _cookie_from_string('"a=1; SAPISID=xyz"')
        self.assertEqual(text, "a=1; SAPISID=xyz")

    def test_cookie_from_json(self):
        text, sapisid = _cookie_from_string('{"cookie": "a=1; b=2", "sapisid": "s1"}')
        self.assertEqual(text, "a=1; b=2")
        self.assertEqual(sapisid, "s1")

    def test_cookie_json_prefers_config_sapisid(self):
        CONFIG["sapisid"] = "override"
        _, sapisid = _cookie_from_string('{"cookie": "a=1", "sapisid": "inner"}')
        self.assertEqual(sapisid, "override")

    def test_cookie_empty(self):
        text, sapisid = _cookie_from_string('""')
        self.assertEqual(text, "")
        self.assertIsNone(sapisid)


if __name__ == "__main__":
    unittest.main()