from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from wechat_ai.llm import LLMClient


class CapturingSessionClient(LLMClient):
    def __init__(self) -> None:
        config = SimpleNamespace(
            openai_base_url="https://example.test/v1/chat",
            openai_api_key="test-key",
            request_timeout=120,
            system_prompt="system",
            knowledge_fallback_reply="fallback",
        )
        super().__init__(config)  # type: ignore[arg-type]
        self.payload: dict[str, Any] = {}
        self.headers: dict[str, str] = {}

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self.payload = payload
        self.headers = headers
        return {"messages": [{"text": "ok"}]}


class SessionChatApiTests(unittest.TestCase):
    def test_session_chat_payload_matches_custom_api_contract(self) -> None:
        client = CapturingSessionClient()

        reply = client._call_session_chat_api(
            chat_name="KiraAI",
            history=[
                {"role": "user", "sender": "KiraAI", "content": "company intro"},
                {"role": "assistant", "sender": "assistant", "content": "we do AI service"},
                {"role": "user", "sender": "KiraAI", "content": "price please"},
            ],
            knowledge_context="knowledge should not be embedded for this API",
        )

        self.assertEqual(reply, "ok")
        self.assertEqual(client.payload["api_key"], "test-key")
        self.assertEqual(client.payload["session_id"], "KiraAI")
        self.assertEqual(client.payload["message"], "price please")
        self.assertEqual(client.payload["preferred_language"], "zh-CN")
        self.assertEqual(client.headers, {"Content-Type": "application/json"})


if __name__ == "__main__":
    unittest.main()
