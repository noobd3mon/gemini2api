"""Response extraction tests built on capture-shaped fixtures.

The fixtures mirror the real StreamGenerate wrb.fr frame structure verified
against the 2026-08-11/14 captures: outer [["wrb.fr", null, json(inner)]],
text at inner[4][i][1], completion status at inner[26][0][0][0][1][1],
model label at inner[42]. inner is padded so lines pass the 200-char filter.
"""
import json
import unittest
from unittest import mock

from gemini_web2api import gemini
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG


def make_inner(text=None, status=0, gg_dl=None):
    inner = [None] * 130
    if text is not None:
        part = [None] * 38
        part[0] = "rc_testframe"
        part[1] = [text]
        part[8] = [1]
        inner[4] = [part]
    if status is not None:
        inner[26] = [[[[None, [None, status, text or ""]]]]]
    if gg_dl:
        inner[12] = [gg_dl]
    inner[42] = "3.6 Flash"
    return inner


def make_line(inner) -> str:
    return json.dumps([["wrb.fr", None, json.dumps(inner)]])


def make_raw(lines) -> str:
    return "\n".join(make_line(inner) for inner in lines)


class ExtractTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG["log_requests"] = False

    def test_extracts_text(self):
        raw = make_raw([make_inner("hello world")])
        self.assertEqual(gemini.extract_response_text(raw), "hello world")

    def test_last_wins_over_longest_on_revision(self):
        stale = "a" * 300
        final = "final draft"
        raw = make_raw([make_inner(stale), make_inner(final)])
        self.assertEqual(gemini.extract_response_text(raw), "final draft")

    def test_image_markdown_format(self):
        CONFIG["image_format"] = "markdown"
        with mock.patch.object(gemini, "resolve_image_url",
                               return_value="https://lh3.googleusercontent.com/rd-gg-dl/x"):
            raw = make_raw([make_inner("text part", gg_dl="https://lh3.googleusercontent.com/gg-dl/tok")])
            out = gemini.extract_response_text(raw)
        self.assertTrue(out.startswith("text part"))
        self.assertIn("![generated image](https://lh3.googleusercontent.com/rd-gg-dl/x)", out)

    def test_image_url_format(self):
        CONFIG["image_format"] = "url"
        with mock.patch.object(gemini, "resolve_image_url",
                               return_value="https://lh3.googleusercontent.com/rd-gg-dl/x"):
            raw = make_raw([make_inner("text part", gg_dl="https://lh3.googleusercontent.com/gg-dl/tok")])
            out = gemini.extract_response_text(raw)
        self.assertEqual(out, "text part\n\nhttps://lh3.googleusercontent.com/rd-gg-dl/x")

    def test_finish_status_extraction(self):
        self.assertEqual(gemini.extract_finish_status(make_raw([make_inner("x", status=0)])), 0)
        self.assertIsNone(gemini.extract_finish_status(make_raw([make_inner("x", status=None)])))
        self.assertIsNone(gemini.extract_finish_status(""))

    def test_bard_error_raises(self):
        raw = '["wrb.fr","x","' + "[]" + '","generic",null,null,null,"generic",[[[["BardErrorInfo",[1100]]]]]]'
        with self.assertRaises(gemini.GeminiUpstreamError):
            gemini.extract_response_text(raw)

    def test_empty_response_returns_empty(self):
        self.assertEqual(gemini.extract_response_text(""), "")


if __name__ == "__main__":
    unittest.main()