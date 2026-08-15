"""Tool calling and message conversion tests."""
import unittest

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.tools import messages_to_prompt, parse_tool_calls, google_contents_to_prompt


TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get weather", "parameters": {"city": "string"}}}]


class ToolsTest(unittest.TestCase):
    def setUp(self):
        CONFIG.clear()
        CONFIG.update(DEFAULT_CONFIG)
        CONFIG["log_requests"] = False

    def test_simple_message(self):
        prompt, images = messages_to_prompt([{"role": "user", "content": "hi"}])
        self.assertEqual(prompt, "hi")
        self.assertEqual(images, [])

    def test_system_and_user_join(self):
        prompt, _ = messages_to_prompt([
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hello"},
        ])
        self.assertEqual(prompt, "[System instruction]: be nice\n\nhello")

    def test_tools_block_includes_names(self):
        prompt, _ = messages_to_prompt([{"role": "user", "content": "weather?"}], TOOLS)
        self.assertIn("get_weather", prompt)
        self.assertIn("tool_call", prompt)

    def test_image_part_lands_in_images(self):
        prompt, images = messages_to_prompt([{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
        ]}])
        self.assertEqual(prompt, "look")
        self.assertEqual(len(images), 1)
        data, mime, _name = images[0]
        self.assertEqual(data, b"hi")
        self.assertEqual(mime, "image/png")

    def test_parse_tool_calls(self):
        text = 'before```tool_call\n{"name": "f", "arguments": {"a": 1}}\n```after'
        clean, calls = parse_tool_calls(text)
        self.assertEqual(clean, "beforeafter")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "f")
        self.assertEqual(calls[0]["function"]["arguments"], '{"a": 1}')

    def test_parse_tool_calls_none(self):
        clean, calls = parse_tool_calls("plain text")
        self.assertEqual(clean, "plain text")
        self.assertEqual(calls, [])

    def test_google_contents_basic(self):
        prompt, _ = google_contents_to_prompt({"contents": [
            {"role": "user", "parts": [{"text": "hello google"}]}]})
        self.assertEqual(prompt, "hello google")

    def test_google_contents_function_parts(self):
        prompt, _ = google_contents_to_prompt({"contents": [{"role": "model", "parts": [
            {"functionCall": {"name": "f", "args": {"a": 1}}}]}]})
        self.assertIn("[Assistant]:", prompt)
        self.assertIn("function_call", prompt)


if __name__ == "__main__":
    unittest.main()