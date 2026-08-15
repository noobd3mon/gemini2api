"""Model resolution tests."""
import unittest

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.models import MODELS, resolve_model


class ModelsTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG["log_requests"] = False

    def test_known_models(self):
        name, mode, think, err, extra = resolve_model("gemini-3.7-flash")
        self.assertEqual((name, mode, think, err), ("gemini-3.7-flash", 1, 4, None))
        self.assertIsNone(extra)

        name, mode, think, err, extra = resolve_model("gemini-3.1-pro-enhanced")
        self.assertIsNone(err)
        self.assertEqual(mode, 3)
        self.assertEqual(extra, {31: 2, 80: 3})

    def test_unknown_falls_back_to_default(self):
        name, mode, think, err, extra = resolve_model("some-unknown-model")
        self.assertEqual(name, CONFIG["default_model"])
        self.assertIsNone(err)

    def test_think_override(self):
        name, mode, think, err, extra = resolve_model("gemini-3.5-flash-thinking@think=2")
        self.assertEqual(name, "gemini-3.5-flash-thinking")
        self.assertEqual(think, 2)

    def test_think_override_invalid(self):
        name, mode, think, err, extra = resolve_model("gemini-3.6-flash@think=x")
        self.assertIsNone(name)
        self.assertIn("Invalid think level", err)

    def test_models_table(self):
        self.assertIn("gemini-3.7-flash", MODELS)
        self.assertIn("gemini-flash-lite", MODELS)


if __name__ == "__main__":
    unittest.main()