from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_ai.store import SQLiteStore, format_daily_report


class SQLiteStoreTests(unittest.TestCase):
    def test_chat_state_can_be_paused_resumed_and_have_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")

            store.pause_chat("chat", reason="manual")
            store.set_chat_policy("chat", "dry_run")
            paused = store.get_chat_state("chat")
            store.resume_chat("chat")
            resumed = store.get_chat_state("chat")

        self.assertTrue(paused.paused)
        self.assertEqual(paused.pause_reason, "manual")
        self.assertEqual(paused.policy, "dry_run")
        self.assertFalse(resumed.paused)
        self.assertEqual(resumed.policy, "dry_run")

    def test_daily_report_counts_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            store.record_event(
                {
                    "ts": "2026-07-11T10:00:00",
                    "action": "send",
                    "chat_name": "chat",
                    "sent": True,
                }
            )
            store.record_event(
                {
                    "ts": "2026-07-11T10:01:00",
                    "action": "skip",
                    "chat_name": "chat",
                    "reason": "sensitive_keyword:合同",
                    "sent": False,
                }
            )

            summary = store.daily_summary(report_date="2026-07-11")
            report = format_daily_report(summary)

        self.assertEqual(summary["actions"], {"send": 1, "skip": 1})
        self.assertIn("sensitive_keyword:合同", report)
        self.assertIn("chat: 1", report)


if __name__ == "__main__":
    unittest.main()
