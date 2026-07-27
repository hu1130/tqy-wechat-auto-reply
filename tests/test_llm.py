from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib import error

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

    def test_retryable_http_500_is_retried_before_success(self) -> None:
        client = LLMClient(
            SimpleNamespace(
                request_timeout=120,
            )
        )  # type: ignore[arg-type]
        calls = 0

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"messages":[{"text":"ok after retry"}]}'

        def fake_urlopen(req, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error.HTTPError(req.full_url, 500, "err", {}, None)
            return FakeResponse()

        with patch("wechat_ai.llm.request.urlopen", side_effect=fake_urlopen):
            with patch("wechat_ai.llm.time.sleep"):
                result = client._post_json("https://example.test/v1/chat", {"a": 1}, {})

        self.assertEqual(result["messages"][0]["text"], "ok after retry")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
