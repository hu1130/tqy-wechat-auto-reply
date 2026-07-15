from __future__ import annotations

import unittest

from wechat_ai.config import AppConfig
from wechat_ai.simulator import StaticReplyClient, simulate_message


def make_config(**overrides: object) -> AppConfig:
    config = AppConfig(
        llm_provider="ollama",
        llm_model="deepseek-r1:14b",
        ollama_base_url="http://localhost:11434",
        openai_base_url="https://api.openai.com/v1",
        openai_api_key="",
        request_timeout=120,
        wechat_listen_chats=["chat"],
        wechat_self_names=[],
        max_context_messages=8,
        dedup_window_seconds=60,
        dry_run=True,
        system_prompt="system",
        reply_allowed_chats=["chat"],
        reply_cooldown_seconds=0,
        default_chat_policy="auto",
        log_file="",
        audit_log_file="",
        state_db_file="",
        knowledge_file="",
    )
    return config.with_overrides(**overrides)


class SimulatorTests(unittest.TestCase):
    def test_simulate_message_uses_dry_run_without_sending(self) -> None:
        result = simulate_message(
            config=make_config(dry_run=True),
            llm_client=StaticReplyClient("offline reply"),
            chat_name="chat",
            sender="alice",
            content="hello",
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["sent_messages"], [])

    def test_simulate_message_can_exercise_fake_live_send_path(self) -> None:
        result = simulate_message(
            config=make_config(dry_run=False, require_manual_confirm=False),
            llm_client=StaticReplyClient("fake sent"),
            chat_name="chat",
            sender="alice",
            content="hello",
        )

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["sent_messages"], ["fake sent"])
        self.assertEqual(result["sent_count"], 1)


if __name__ == "__main__":
    unittest.main()
