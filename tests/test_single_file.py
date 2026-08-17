"""Parity guard: the generated single-file must match the package.

Fails when gemini_web2api.py has not been regenerated after a package change -
run `python build_single_file.py` and re-run the tests.
"""
import importlib.util
import pathlib
import unittest

import gemini_web2api

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_single_file():
    spec = importlib.util.spec_from_file_location("gw_single_file", ROOT / "gemini_web2api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SingleFileParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sf = load_single_file()
        cls.sf.CONFIG["log_requests"] = False

    def test_version_matches(self):
        self.assertEqual(self.sf.__version__, gemini_web2api.__version__)

    def test_models_match(self):
        self.assertEqual(set(self.sf.MODELS.keys()), set(gemini_web2api.models.MODELS.keys()))

    def test_server_symbols_exist(self):
        for name in ("GeminiHandler", "ThreadedServer", "generate", "generate_stream",
                     "extract_response_text", "load_cookie", "load_env_config", "main"):
            self.assertTrue(hasattr(self.sf, name), f"missing {name}")

    def test_no_relative_imports_left(self):
        src = (ROOT / "gemini_web2api.py").read_text(encoding="utf-8")
        self.assertNotIn("from .", src)


if __name__ == "__main__":
    unittest.main()
