"""Parity guard: the generated single-file must match the package.

This test fails when gemini_web2api.py has not been regenerated after a
package change - run `python build_single_file.py` and re-run the tests.
"""
import importlib.util
import pathlib
import unittest

import gemini_web2api
from gemini_web2api import gemini as pkg_gemini

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_single_file():
    spec = importlib.util.spec_from_file_location("gw_single_file", ROOT / "gemini_web2api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTRO = "Chào anh Hiếu Kỳ! Em là Bot LHK AI Assistant đây ạ. \n\n"
PARTIAL = INTRO + "Anh cần"
REVISED = INTRO + "Hôm nay anh cần em hỗ trợ công việc hay giải trí vấn đề gì không ạ?"


class SingleFileParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sf = load_single_file()
        cls.sf.CONFIG["log_requests"] = False

    def test_version_matches(self):
        self.assertEqual(self.sf.__version__, gemini_web2api.__version__)

    def test_models_match(self):
        self.assertEqual(set(self.sf.MODELS.keys()), set(gemini_web2api.models.MODELS.keys()))

    def test_delta_logic_matches_package(self):
        for emitted, t in ((INTRO, REVISED), (PARTIAL, REVISED), (REVISED, REVISED),
                           (INTRO + "dài", INTRO), (INTRO, INTRO + "thêm")):
            self.assertEqual(self.sf._delta_from(emitted, t), pkg_gemini._delta_from(emitted, t))

    def test_revision_scenario_no_intro_repeat(self):
        emitted, out = "", []
        for t in (PARTIAL, REVISED):
            if t == emitted or emitted.startswith(t):
                continue
            delta = self.sf._delta_from(emitted, t)
            emitted = t
            if delta:
                out.append(delta)
        self.assertEqual("".join(out).count(INTRO), 1)

    def test_server_symbols_exist(self):
        for name in ("GeminiHandler", "ThreadedServer", "generate", "generate_stream",
                     "extract_response_text", "update_bl_if_needed", "load_env_config",
                     "_invalidate_cookie_cache", "extract_finish_status", "main"):
            self.assertTrue(hasattr(self.sf, name), f"missing {name}")

    def test_no_relative_imports_left(self):
        src = (ROOT / "gemini_web2api.py").read_text(encoding="utf-8")
        self.assertNotIn("from .", src)


if __name__ == "__main__":
    unittest.main()