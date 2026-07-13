from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_ai.config import (
    AppConfig,
    env_template,
    validate_config,
    write_env_file,
)


class EnvTemplateTests(unittest.TestCase):
    def test_env_template_contains_user_facing_defaults(self) -> None:
        text = env_template()
        self.assertIn("LLM_PROVIDER=ollama", text)
        self.assertIn("WECHAT_LISTEN_CHATS=文件传输助手", text)
        self.assertIn("DRY_RUN=1", text)
        self.assertIn("REPLY_ALLOWED_CHATS=文件传输助手", text)
        self.assertIn("TEXT_MESSAGE_TYPES=text,friend", text)
        self.assertIn("DEFAULT_CHAT_POLICY=manual", text)
        self.assertIn("REQUIRE_MANUAL_CONFIRM=1", text)
        self.assertIn("MAX_SENDS_PER_RUN=20", text)
        self.assertIn("LOG_FILE=logs/wechat-ai.log", text)
        self.assertIn("AUDIT_LOG_FILE=logs/audit.jsonl", text)
        self.assertIn("ALERT_FILE=logs/alerts.log", text)
        self.assertIn("LOG_MAX_BYTES=10485760", text)
        self.assertIn("HEALTH_FILE=logs/health.json", text)
        self.assertIn("REPORTS_DIR=reports", text)
        self.assertIn("STATE_DB_FILE=data/wechat-ai.sqlite3", text)
        self.assertIn("KNOWLEDGE_FILE=knowledge/faq.md", text)
        self.assertIn("REQUIRE_KNOWLEDGE_MATCH=0", text)

    def test_write_env_file_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            write_env_file(path)
            with self.assertRaises(RuntimeError):
                write_env_file(path)
            write_env_file(path, force=True)


class ConfigValidationTests(unittest.TestCase):
    def test_openai_compatible_requires_api_key(self) -> None:
        config = AppConfig(
            llm_provider="openai_compatible",
            llm_model="gpt-4o-mini",
            ollama_base_url="http://localhost:11434",
            openai_base_url="https://api.openai.com/v1",
            openai_api_key="",
            request_timeout=120,
            wechat_listen_chats=["文件传输助手"],
            wechat_self_names=[],
            max_context_messages=8,
            dedup_window_seconds=60,
            dry_run=True,
            system_prompt="hello",
        )

        self.assertIn(
            "OPENAI_API_KEY is required when LLM_PROVIDER=openai_compatible.",
            validate_config(config),
        )

    def test_negative_safety_values_are_invalid(self) -> None:
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
            system_prompt="hello",
        ).with_overrides(
            max_reply_chars=-1,
            reply_cooldown_seconds=-1,
            max_sends_per_run=-1,
            max_sends_per_chat=-1,
            max_sends_per_day=-1,
            max_sends_per_chat_per_day=-1,
            max_errors_before_pause=-1,
            log_max_bytes=-1,
            log_backup_count=-1,
            knowledge_top_k=-1,
        )

        warnings = validate_config(config)

        self.assertIn("MAX_REPLY_CHARS should not be negative.", warnings)
        self.assertIn("REPLY_COOLDOWN_SECONDS should not be negative.", warnings)
        self.assertIn("MAX_SENDS_PER_RUN should not be negative.", warnings)
        self.assertIn("MAX_SENDS_PER_CHAT should not be negative.", warnings)
        self.assertIn("MAX_SENDS_PER_DAY should not be negative.", warnings)
        self.assertIn("MAX_SENDS_PER_CHAT_PER_DAY should not be negative.", warnings)
        self.assertIn("MAX_ERRORS_BEFORE_PAUSE should not be negative.", warnings)
        self.assertIn("LOG_MAX_BYTES should not be negative.", warnings)
        self.assertIn("LOG_BACKUP_COUNT should not be negative.", warnings)
        self.assertIn("KNOWLEDGE_TOP_K should not be negative.", warnings)

    def test_allowed_chats_are_required(self) -> None:
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
            system_prompt="hello",
        )

        self.assertIn(
            "REPLY_ALLOWED_CHATS is required. Start with a small whitelist such as 文件传输助手.",
            validate_config(config),
        )

    def test_invalid_chat_policy_is_rejected(self) -> None:
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
            system_prompt="hello",
            reply_allowed_chats=["chat"],
            default_chat_policy="manual",
            chat_policies={"chat": "unsafe"},
        )

        self.assertIn("CHAT_POLICIES contains invalid policies: unsafe.", validate_config(config))


if __name__ == "__main__":
    unittest.main()
