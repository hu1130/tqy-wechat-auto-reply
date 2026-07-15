from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from wechat_ai.admin import (
    pause_chat,
    render_pending_replies,
    render_report,
    render_status,
    resume_chat,
    save_daily_report,
    set_chat_policy,
)
from wechat_ai.config import AppConfig
from wechat_ai.store import SQLiteStore


def make_config(db_path: str) -> AppConfig:
    return AppConfig(
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
        state_db_file=db_path,
        reports_dir=str(Path(db_path).parent / "reports"),
    )


class AdminTests(unittest.TestCase):
    def test_pause_resume_policy_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_config(str(Path(temp_dir) / "state.sqlite3"))

            pause_chat(config, "chat", "handoff")
            set_chat_policy(config, "chat", "dry_run")
            paused_status = render_status(config)
            resume_chat(config, "chat")
            resumed_status = render_status(config)

        self.assertIn("chat: paused", paused_status)
        self.assertIn("policy=dry_run", paused_status)
        self.assertIn("reason=handoff", paused_status)
        self.assertIn("chat: active", resumed_status)

    def test_report_reads_store_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(str(db_path))
            store = SQLiteStore(db_path)
            store.record_event(
                {
                    "ts": "2026-07-11T10:00:00",
                    "action": "send",
                    "chat_name": "chat",
                    "sent": True,
                }
            )

            report = render_report(config, report_date="2026-07-11")

        self.assertIn("Daily report for 2026-07-11", report)
        self.assertIn("- send: 1", report)

    def test_save_daily_report_writes_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            config = make_config(str(db_path))
            store = SQLiteStore(db_path)
            store.record_event(
                {
                    "ts": "2026-07-11T10:00:00",
                    "action": "send",
                    "chat_name": "chat",
                    "sent": True,
                }
            )

            message = save_daily_report(config, report_date="2026-07-11")
            report_path = Path(config.reports_dir) / "2026-07-11.txt"

            self.assertIn("Saved report to", message)
            self.assertTrue(report_path.exists())

    def test_render_pending_replies_reads_recent_queue_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            pending_path = Path(temp_dir) / "pending.jsonl"
            config = make_config(str(db_path)).with_overrides(pending_replies_file=str(pending_path))
            pending_path.write_text(
                "\n".join(
                    [
                        json.dumps({"ts": "2026-07-11T10:00:00", "chat_name": "old", "reply": "old"}),
                        json.dumps({"ts": "2026-07-11T10:01:00", "chat_name": "chat", "reply": "new"}),
                    ]
                ),
                encoding="utf-8",
            )

            output = render_pending_replies(config, limit=1)

        self.assertIn("reply=new", output)
        self.assertNotIn("reply=old", output)


if __name__ == "__main__":
    unittest.main()
