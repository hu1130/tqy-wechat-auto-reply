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


if __name__ == "__main__":
    unittest.main()
