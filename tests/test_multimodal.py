"""Page-token cache file tests (no network: _get_page_tokens is patched)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.multimodal import (_cached_page_tokens, _get_page_tokens,
                                       _load_token_cache, _invalidate_page_tokens)


class TokenCacheTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG["log_requests"] = False
        _invalidate_page_tokens()

    def test_load_token_cache_valid_and_invalid(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"ts": 123.0, "tokens": {"push_id": "p1"}}, f)
            path = f.name
        try:
            self.assertEqual(_load_token_cache(path), {"ts": 123.0, "tokens": {"push_id": "p1"}})
            with open(path, "w") as f:
                f.write("not json")
            self.assertIsNone(_load_token_cache(path))
            os.unlink(path)
            self.assertIsNone(_load_token_cache(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_cache_file_avoids_rescrape_after_invalidate(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            CONFIG["token_cache_file"] = path
            scrapes = [0]

            def fake_scrape():
                scrapes[0] += 1
                return {"push_id": "from-page", "at": "at1", "f_sid": "fs1"}

            with mock.patch.object(__import__("gemini_web2api.multimodal",
                                              fromlist=["_get_page_tokens"]),
                                   "_get_page_tokens", side_effect=fake_scrape):
                tokens = _cached_page_tokens()
                self.assertEqual(tokens["push_id"], "from-page")
                self.assertEqual(scrapes[0], 1)
                # warm cache: no rescrape
                self.assertEqual(_cached_page_tokens()["push_id"], "from-page")
                self.assertEqual(scrapes[0], 1)
                # invalidate: loads from the cache file instead of rescraping
                _invalidate_page_tokens()
                self.assertEqual(_cached_page_tokens()["push_id"], "from-page")
                self.assertEqual(scrapes[0], 1)
                self.assertTrue(os.path.exists(path))
                saved = json.load(open(path, encoding="utf-8"))
                self.assertEqual(saved["tokens"]["push_id"], "from-page")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_no_cache_file_rescrapes(self):
        scrapes = [0]

        def fake_scrape():
            scrapes[0] += 1
            return {"push_id": "p"}

        with mock.patch.object(__import__("gemini_web2api.multimodal",
                                          fromlist=["_get_page_tokens"]),
                               "_get_page_tokens", side_effect=fake_scrape):
            _cached_page_tokens()
            _invalidate_page_tokens()
            _cached_page_tokens()
            self.assertEqual(scrapes[0], 2)


if __name__ == "__main__":
    unittest.main()