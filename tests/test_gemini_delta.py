"""Streaming delta logic tests, including the mid-stream revision bug."""
import unittest

from gemini_web2api.gemini import _common_prefix_len, _delta_from, clean_text


INTRO = "Chào anh Hiếu Kỳ! Em là Bot LHK AI Assistant đây ạ. \n\n"
PARTIAL = INTRO + "Anh cần"
REVISED = INTRO + "Hôm nay anh cần em hỗ trợ công việc hay giải trí vấn đề gì không ạ?"


class DeltaTest(unittest.TestCase):
    def test_common_prefix_len(self):
        self.assertEqual(_common_prefix_len("abc", "abd"), 2)
        self.assertEqual(_common_prefix_len("abc", "abc"), 3)
        self.assertEqual(_common_prefix_len("", "abc"), 0)
        self.assertEqual(_common_prefix_len("abc", ""), 0)

    def test_prefix_growth_emits_suffix(self):
        emitted = INTRO + "Hôm"
        self.assertEqual(_delta_from(emitted, INTRO + "Hôm nay"), " nay")
        self.assertEqual(_delta_from(emitted, INTRO + "Hôm nay anh"), " nay anh")

    def test_equal_and_shorter_prefix_emit_nothing(self):
        self.assertEqual(_delta_from(INTRO, INTRO), "")
        self.assertEqual(_delta_from(INTRO + "dài thêm", INTRO), "")
        self.assertEqual(_delta_from(INTRO + "dài thêm", INTRO + "dài"), "")

    def test_revision_emits_only_new_suffix(self):
        # Gemini revised mid-stream: the new draft is not a prefix-extension.
        delta = _delta_from(PARTIAL, REVISED)
        self.assertTrue(delta.startswith("Hôm nay"))
        self.assertNotIn(INTRO.strip(), delta)

    def test_shorten_revision_already_sent_emits_nothing(self):
        emitted = INTRO + "Anh cần Hôm nay anh cần gì đó"
        new = INTRO + "Hôm nay anh cần gì đó"
        self.assertEqual(_delta_from(emitted, new), "")

    def test_user_bug_scenario_no_intro_repeat(self):
        # Feed the two frames through the same loop generate_stream uses and
        # assert the intro appears exactly once in what the client receives.
        emitted, out = "", []
        for t in (PARTIAL, REVISED):
            if t == emitted or emitted.startswith(t):
                continue
            delta = _delta_from(emitted, t)
            emitted = t
            if delta:
                out.append(delta)
        received = "".join(out)
        self.assertEqual(received.count(INTRO), 1)
        self.assertIn("Hôm nay anh cần", received)

    def test_clean_text_strips_code_and_placeholder(self):
        self.assertEqual(clean_text("  hi  "), "hi")
        text = "x```python?code_reference&code_event_index=3\nCODE\n```\ny"
        self.assertEqual(clean_text(text), "xy")
        text2 = "zhttp://googleusercontent.com/card_content/abc\nw"
        self.assertEqual(clean_text(text2), "zw")


if __name__ == "__main__":
    unittest.main()