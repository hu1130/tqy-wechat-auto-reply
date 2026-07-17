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

    def test_recent_events_and_chat_overview_power_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            store.pause_chat("chat", reason="human_handoff")
            store.record_event(
                {
                    "ts": "2026-07-16T10:00:00",
                    "action": "send",
                    "chat_name": "chat",
                    "sender": "alice",
                    "sent": True,
                    "content_preview": "hello",
                    "reply_preview": "hi",
                }
            )
            store.record_event(
                {
                    "ts": "2026-07-16T10:01:00",
                    "action": "skip",
                    "chat_name": "chat",
                    "reason": "cooldown",
                    "sent": False,
                }
            )

            events = store.list_recent_events(limit=2)
            overview = store.chat_overview()

        self.assertEqual(events[0]["action"], "skip")
        self.assertEqual(events[1]["reply_preview"], "hi")
        self.assertEqual(overview[0]["chat_name"], "chat")
        self.assertTrue(overview[0]["paused"])
        self.assertEqual(overview[0]["pause_reason"], "human_handoff")
        self.assertEqual(overview[0]["sent_count"], 1)
        self.assertEqual(overview[0]["skip_count"], 1)

    def test_customer_can_be_added_and_event_infers_lead_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            store.upsert_customer(
                chat_name="lead-a",
                display_name="Lead A",
                phone="13800000000",
                tags="vip",
                lead_level="low",
                status="contacted",
                owner="sales",
                notes="manual add",
            )
            store.record_event(
                {
                    "ts": "2026-07-16T11:00:00",
                    "action": "receive",
                    "chat_name": "lead-a",
                    "content_preview": "\u4ef7\u683c\u591a\u5c11\uff0c\u6211\u60f3\u8bd5\u7528",
                }
            )

            customer = store.get_customer("lead-a")
            customers = store.list_customers()

        self.assertEqual(customer.display_name, "Lead A")
        self.assertEqual(customer.phone, "13800000000")
        self.assertEqual(customer.lead_level, "high")
        self.assertEqual(customers[0]["chat_name"], "lead-a")

    def test_welcome_sent_state_is_persisted_per_customer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")

            before = store.has_sent_welcome("chat")
            store.mark_welcome_sent("chat", timestamp="2026-07-16T12:00:00")
            after = store.has_sent_welcome("chat")
            customer = store.get_customer("chat")

        self.assertFalse(before)
        self.assertTrue(after)
        self.assertEqual(customer.welcome_sent_at, "2026-07-16T12:00:00")


if __name__ == "__main__":
    unittest.main()
