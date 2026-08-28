import http.client
import base64
import json
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.gemini import _build_payload, _build_file_bindings
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.tools import google_contents_to_prompt, messages_to_prompt
from gemini_web2api.multimodal import fetch_file_bytes


def _decode_payload(payload):
    outer = json.loads(parse_qs(payload)["f.req"][0])
    return json.loads(outer[1])


def _decode_sse(body):
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            (line[len("event: "):] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line[len("data: "):] for line in lines if line.startswith("data: ")),
            None,
        )
        if event_type and data:
            events.append((event_type, json.loads(data)))
    return events


class PayloadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_temporary_chats_default_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)

    def test_persistent_chat_payload(self):
        CONFIG["temporary_chats"] = False

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [2])
        self.assertIsNone(inner[45])

    def test_temporary_chat_payload(self):
        CONFIG["temporary_chats"] = True

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_payload_includes_uploaded_image_refs(self):
        inner = _decode_payload(_build_payload("describe", 1, 4, ["/uploaded/image-ref"]))

        self.assertEqual(inner[0][0], "describe")
        # Capture-verified binding: [[[ref, kind, None, mime], filename], ...]
        self.assertEqual(
            inner[0][3],
            [[["/uploaded/image-ref", 3, None, "application/octet-stream"], "file_0"]],
        )

    def test_file_bindings_use_kind_1_for_images_and_3_for_files(self):
        refs = [
            ("/ref/img.png", "img.png", "image/png"),
            ("/ref/doc.pdf", "doc.pdf", "application/pdf"),
        ]

        bindings = _build_file_bindings(refs)

        self.assertEqual(bindings, [
            [["/ref/img.png", 1, None, "image/png"], "img.png"],
            [["/ref/doc.pdf", 3, None, "application/pdf"], "doc.pdf"],
        ])


class MessageParsingTests(unittest.TestCase):
    def test_messages_to_prompt_extracts_openai_image_url_data_url(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(attachments, [(b"fake png", "image/png", "")])

    def test_messages_to_prompt_extracts_responses_input_image_url(self):
        prompt, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(attachments, [("https://example.com/image.png", "", "image.png")])

    def test_messages_to_prompt_extracts_input_file_pdf(self):
        pdf_data = base64.b64encode(b"%PDF-fake").decode()

        prompt, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Summarize"},
                {"type": "input_file", "filename": "notes.pdf",
                 "file_data": f"data:application/pdf;base64,{pdf_data}"},
            ],
        }])

        self.assertEqual(prompt, "Summarize [Attached file: notes.pdf]")
        self.assertEqual(attachments, [(b"%PDF-fake", "application/pdf", "notes.pdf")])

    def test_messages_to_prompt_extracts_chat_file_part(self):
        pdf_data = base64.b64encode(b"%PDF-fake").decode()

        _, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "file",
                 "file": {"filename": "doc.pdf", "file_data": f"data:application/pdf;base64,{pdf_data}"}},
            ],
        }])

        self.assertEqual(attachments, [(b"%PDF-fake", "application/pdf", "doc.pdf")])

    def test_messages_to_prompt_extracts_anthropic_source_image(self):
        image_data = base64.b64encode(b"fake jpeg").decode()

        _, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": image_data}},
            ],
        }])

        self.assertEqual(attachments, [(b"fake jpeg", "image/jpeg", "")])

    def test_messages_to_prompt_tolerates_string_and_non_dict_parts(self):
        prompt, attachments = messages_to_prompt([{
            "role": "user",
            "content": ["hello", {"type": "text", "text": "world"}, 42, None],
        }])

        self.assertEqual(prompt, "hello world")
        self.assertEqual(attachments, [])

    def test_messages_to_prompt_ignores_malformed_image_data_url(self):
        prompt, attachments = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
            ],
        }])

        self.assertEqual(prompt, "Describe")
        self.assertEqual(attachments, [])

    def test_google_contents_to_prompt_extracts_inline_image_data(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, attachments = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": image_data}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(attachments, [(b"fake png", "image/png", "")])

    def test_google_contents_to_prompt_extracts_inline_pdf_data(self):
        pdf_data = base64.b64encode(b"%PDF-fake").decode()

        prompt, attachments = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Summarize"},
                    {"inlineData": {"mimeType": "application/pdf", "data": pdf_data,
                                    "displayName": "report.pdf"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Summarize\n[Attached file: report.pdf]")
        self.assertEqual(attachments, [(b"%PDF-fake", "application/pdf", "report.pdf")])

    def test_google_contents_to_prompt_extracts_file_data_uri(self):
        prompt, attachments = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"fileData": {"mimeType": "image/png", "fileUri": "https://example.com/a.png"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(attachments, [("https://example.com/a.png", "", "a.png")])

    def test_google_contents_to_prompt_ignores_malformed_inline_image_data(self):
        prompt, attachments = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe")
        self.assertEqual(attachments, [])


class SsrfProtectionTests(unittest.TestCase):
    def test_fetch_file_bytes_blocks_loopback(self):
        self.assertEqual(fetch_file_bytes("http://127.0.0.1:8081/secret"), (b"", ""))

    def test_fetch_file_bytes_blocks_link_local_metadata(self):
        self.assertEqual(fetch_file_bytes("http://169.254.169.254/latest/meta-data/"), (b"", ""))

    def test_fetch_file_bytes_blocks_private_range(self):
        self.assertEqual(fetch_file_bytes("http://192.168.1.10/admin"), (b"", ""))

    def test_fetch_file_bytes_blocks_unsupported_scheme(self):
        self.assertEqual(fetch_file_bytes("file:///etc/passwd"), (b"", ""))


class StreamingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def post_chunked_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    @mock.patch("gemini_web2api.server.generate_stream")
    def test_chat_stream_starts_with_assistant_role(self, generate_stream):
        generate_stream.return_value = iter(["hel", "lo"])

        status, headers, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "hel"})
        self.assertEqual(chunks[2]["choices"][0]["delta"], {"content": "lo"})
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.generate", return_value="chunked ok")
    def test_chat_accepts_chunked_body(self, _generate):
        status, _, body = self.post_chunked_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "chunked ok")

    @mock.patch("gemini_web2api.server.upload_file", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="looks good")
    def test_chat_accepts_openai_image_url_data_url(self, generate, upload_file):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(upload_file.call_count, 1)
        args = upload_file.call_args.args
        self.assertEqual(args[0], b"fake png")
        self.assertEqual(args[2], "image/png")
        self.assertRegex(args[1], r"image_\d+_0\.png")
        self.assertEqual(generate.call_args.args[3],
                         [("/uploaded/image-ref", args[1], "image/png")])
        self.assertIn("[Image attached]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "looks good")

    @mock.patch("gemini_web2api.multimodal.fetch_file_bytes",
                return_value=(b"\xff\xd8\xffremote jpeg", "image/jpeg"))
    @mock.patch("gemini_web2api.server.upload_file", return_value="/uploaded/remote-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="remote ok")
    def test_responses_accepts_input_image_url(self, generate, upload_file, _fetch):
        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is shown?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.jpg",
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_file.assert_called_once_with(
            b"\xff\xd8\xffremote jpeg", "image.jpg", "image/jpeg")
        self.assertEqual(generate.call_args.args[3],
                         [("/uploaded/remote-ref", "image.jpg", "image/jpeg")])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_file", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="top-level image ok")
    def test_responses_accepts_top_level_input_image(self, generate, upload_file):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [
                    {"type": "input_text", "text": "What is shown?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        args = upload_file.call_args.args
        self.assertEqual(args[0], b"fake png")
        self.assertEqual(args[2], "image/png")
        self.assertEqual(generate.call_args.args[3],
                         [("/uploaded/image-ref", args[1], "image/png")])
        self.assertIn("What is shown?", generate.call_args.args[0])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_file", return_value="/uploaded/file-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="pdf ok")
    def test_chat_accepts_openai_file_part_pdf(self, generate, upload_file):
        pdf_data = base64.b64encode(b"%PDF-fake").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this document"},
                        {"type": "file",
                         "file": {"filename": "notes.pdf",
                                  "file_data": f"data:application/pdf;base64,{pdf_data}"}},
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_file.assert_called_once_with(b"%PDF-fake", "notes.pdf", "application/pdf")
        self.assertEqual(generate.call_args.args[3],
                         [("/uploaded/file-ref", "notes.pdf", "application/pdf")])
        self.assertIn("[Attached file: notes.pdf]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "pdf ok")

    @mock.patch("gemini_web2api.multimodal.fetch_file_bytes",
                return_value=(b"fake png", "image/png"))
    @mock.patch("gemini_web2api.server.upload_file", return_value="/uploaded/gref")
    @mock.patch("gemini_web2api.server.generate", return_value="filedata ok")
    def test_google_file_data_uri_is_fetched_and_uploaded(self, generate, upload_file, _fetch):
        status, _, _ = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"text": "Describe"},
                        {"fileData": {"mimeType": "image/png",
                                      "fileUri": "https://example.com/a.png"}},
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_file.assert_called_once_with(b"fake png", "a.png", "image/png")
        self.assertEqual(generate.call_args.args[3],
                         [("/uploaded/gref", "a.png", "image/png")])

    @mock.patch("gemini_web2api.server.upload_file", side_effect=RuntimeError("upload denied"))
    def test_google_image_upload_failure_returns_502(self, _upload_file):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": image_data,
                        },
                    }],
                }],
            },
        )

        self.assertEqual(status, 502)
        self.assertIn("attachment upload failed", json.loads(body)["error"]["message"])
        self.assertIn("upload denied", json.loads(body)["error"]["message"])

    @mock.patch("gemini_web2api.server.generate", return_value="partial ok")
    def test_chat_continues_when_only_some_attachments_fail(self, generate):
        def fake_upload(data, filename, mime):
            if data == b"bad":
                raise RuntimeError("denied")
            return "/ok-ref"

        bad = base64.b64encode(b"bad").decode()
        good = base64.b64encode(b"good").decode()

        with mock.patch("gemini_web2api.server.upload_file", side_effect=fake_upload):
            status, _, body = self.post_json(
                "/v1/chat/completions",
                {
                    "model": "gemini-3.6-flash",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{bad}"}},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{good}"}},
                        ],
                    }],
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "partial ok")
        refs = generate.call_args.args[3]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0][0], "/ok-ref")

    @mock.patch("gemini_web2api.server.generate_stream", return_value=iter(["streamed"]))
    def test_google_stream_generate_content_uses_sse(self, _generate_stream):
        status, headers, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": "Stream this"}],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"text": "streamed"', body)

    @mock.patch("gemini_web2api.server.generate", return_value="hello")
    def test_responses_text_stream_has_complete_event_sequence(self, _generate):
        status, headers, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "hello",
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[4][1]["delta"], "hello")
        self.assertEqual(events[-1][1]["response"]["status"], "completed")
        self.assertEqual(events[-1][1]["response"]["output"][0]["content"][0]["text"], "hello")

    @mock.patch("gemini_web2api.server.parse_tool_calls")
    @mock.patch("gemini_web2api.server.generate", return_value="tool output")
    def test_responses_function_call_stream_has_complete_event_sequence(
        self, _generate, parse_tool_calls
    ):
        parse_tool_calls.return_value = (
            "",
            [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        )

        status, _, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[2][1]["output_index"], 0)
        self.assertEqual(events[3][1]["delta"], '{"city":"Shanghai"}')
        self.assertEqual(events[4][1]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
