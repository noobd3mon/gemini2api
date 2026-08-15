"""Config precedence and env parsing tests."""
import json
import os
import tempfile
import unittest

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG, load_env_config, parse_keys, parse_bool, parse_int


class ConfigTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        self._set = []

    def tearDown(self):
        for name in self._set:
            os.environ.pop(name, None)

    def setenv(self, name, value):
        self._set.append(name)
        os.environ[name] = value

    def test_env_overrides_defaults(self):
        self.setenv("PORT", "9999")
        self.setenv("GEMINI_COOKIE", "a=1; SAPISID=x")
        self.setenv("GEMINI_IMAGE_FORMAT", "url")
        self.setenv("GEMINI_RATE_LIMIT", "5")
        self.setenv("API_KEYS", "k1, k2")
        load_env_config()
        self.assertEqual(CONFIG["port"], 9999)
        self.assertEqual(CONFIG["cookie"], "a=1; SAPISID=x")
        self.assertEqual(CONFIG["image_format"], "url")
        self.assertEqual(CONFIG["rate_limit"], 5)
        self.assertEqual(CONFIG["api_keys"], ["k1", "k2"])

    def test_env_does_not_override_when_unset(self):
        load_env_config()
        self.assertEqual(CONFIG["port"], DEFAULT_CONFIG["port"])
        self.assertEqual(CONFIG["rate_limit"], 0)

    def test_env_wins_over_config_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"port": 7777, "rate_limit": 7}, f)
            path = f.name
        try:
            from gemini_web2api.config import load_config
            load_config(path)
            self.assertEqual(CONFIG["port"], 7777)
            self.setenv("PORT", "8888")
            self.setenv("GEMINI_RATE_LIMIT", "9")
            load_env_config()
            self.assertEqual(CONFIG["port"], 8888)
            self.assertEqual(CONFIG["rate_limit"], 9)
        finally:
            os.unlink(path)

    def test_parse_keys_json_array(self):
        self.assertEqual(parse_keys('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_keys("[]"), [])

    def test_parse_keys_separated_list(self):
        self.assertEqual(parse_keys("a, b;c d"), ["a", "b", "c", "d"])
        self.assertEqual(parse_keys("  single  "), ["single"])

    def test_parse_bool_int(self):
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool("off"))
        self.assertIsNone(parse_bool("wat"))
        self.assertEqual(parse_int("42"), 42)
        self.assertIsNone(parse_int("nope"))
        self.assertEqual(parse_int("7", default=1), 7)
        self.assertEqual(parse_int("x", default=1), 1)


if __name__ == "__main__":
    unittest.main()