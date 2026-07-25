import json
import os
import unittest

import httpx


PROXY_CHAT_URL = os.getenv("PROXY_CHAT_URL", "http://localhost:5001/v1/chat/completions")
PROXY_CHAT_MODEL = os.getenv("PROXY_CHAT_MODEL", "openclaw")


class OpenWebUIStreamSmokeTests(unittest.TestCase):
    def test_proxy_stream_contract_matches_openwebui_expectations(self):
        request_body = {
            "model": PROXY_CHAT_MODEL,
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly: smoke stream contract",
                }
            ],
        }

        with httpx.stream("POST", PROXY_CHAT_URL, json=request_body, timeout=120) as response:
            self.assertEqual(response.status_code, 200)
            data_lines = [
                line for line in response.iter_lines()
                if line and line.startswith("data: ")
            ]

        self.assertGreaterEqual(len(data_lines), 2)

        chunks = []
        saw_done = False
        for line in data_lines:
            payload = line[6:]
            if payload == "[DONE]":
                saw_done = True
                break
            chunks.append(json.loads(payload))

        self.assertTrue(saw_done, "stream did not terminate with [DONE]")
        self.assertGreaterEqual(len(chunks), 1)

        first_choice = chunks[0]["choices"][0]
        self.assertEqual(chunks[0]["object"], "chat.completion.chunk")
        if "role" in first_choice["delta"]:
            self.assertEqual(first_choice["delta"].get("role"), "assistant")
            self.assertIsNone(first_choice["finish_reason"])

        terminal_choice = chunks[-1]["choices"][0]
        self.assertEqual(terminal_choice["delta"], {})
        self.assertEqual(terminal_choice["finish_reason"], "stop")

        content = "".join(
            chunk["choices"][0].get("delta", {}).get("content", "")
            for chunk in chunks
        )
        if content:
            self.assertIn("smoke", content.lower())


if __name__ == "__main__":
    unittest.main()