from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
from threading import Barrier
import unittest
from pathlib import Path

from wechat_ai.store import (
    MessageBatch,
    MessageMedia,
    OutboxJob,
    SQLiteStore,
    StoredMessage,
    format_daily_report,
)


class SQLiteStoreTests(unittest.TestCase):
    def test_message_fingerprint_is_deduplicated_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            first_store = SQLiteStore(db_path)
            second_store = SQLiteStore(db_path)

            first, first_created = first_store.record_message(
                "fingerprint-1", "chat", "alice", "text", "hello"
            )
            duplicate, duplicate_created = second_store.record_message(
                "fingerprint-1", "other-chat", "bob", "image", "different"
            )

        self.assertIsInstance(first, StoredMessage)
        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, first)
        self.assertEqual(duplicate.chat_name, "chat")

    def test_message_and_outbox_complete_successful_state_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-success", "chat", "alice", "text", "question")

            store.mark_messages_processing([message.id])
            processing = store.get_message(message.id)
            store.mark_messages_generated([message.id], "answer")
            generated = store.get_message_by_fingerprint("fp-success")
            job = store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            claimed = store.claim_outbox(job.id)
            sending_message = store.get_message(message.id)
            store.mark_outbox_sent(job.id)
            sent_job = store.list_outbox(status="sent")[0]
            sent_message = store.get_message(message.id)

        self.assertEqual(processing.status, "processing")
        self.assertEqual(generated.status, "generated")
        self.assertEqual(generated.reply, "answer")
        self.assertIsInstance(job, OutboxJob)
        self.assertEqual(job.source_message_ids, (message.id,))
        self.assertEqual(job.content_preview, "question")
        self.assertEqual(claimed.status, "sending")
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(sending_message.status, "sending")
        self.assertEqual(sent_job.status, "sent")
        self.assertEqual(sent_message.status, "sent")

    def test_outbox_is_unique_and_claim_can_only_succeed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            first_store = SQLiteStore(db_path)
            second_store = SQLiteStore(db_path)
            message, _ = first_store.record_message("fp-claim", "chat", "alice", "text", "question")
            first_store.mark_messages_processing([message.id])

            first_job = first_store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            duplicate_job = second_store.enqueue_outbox(
                [message.id], "changed", "bob", "image", "manual", "changed", "changed"
            )
            claimed = first_store.claim_outbox(first_job.id)
            duplicate_claim = second_store.claim_outbox(first_job.id)

        self.assertEqual(duplicate_job, first_job)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNone(duplicate_claim)

    def test_outbox_claim_is_atomic_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            setup_store = SQLiteStore(db_path)
            first_store = SQLiteStore(db_path)
            second_store = SQLiteStore(db_path)
            message, _ = setup_store.record_message("fp-concurrent", "chat", "alice", "text", "question")
            setup_store.mark_messages_processing([message.id])
            job = setup_store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            barrier = Barrier(2)

            def claim(store: SQLiteStore) -> OutboxJob | None:
                barrier.wait()
                return store.claim_outbox(job.id)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, (first_store, second_store)))

        successful_claims = [result for result in results if result is not None]
        self.assertEqual(len(successful_claims), 1)
        self.assertEqual(successful_claims[0].attempts, 1)

    def test_uncertain_outbox_fails_sources_and_is_not_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-uncertain", "chat", "alice", "text", "question")
            store.mark_messages_processing([message.id])
            job = store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            store.claim_outbox(job.id)

            store.mark_outbox_uncertain(job.id, "send result unknown")
            uncertain = store.list_outbox(status="uncertain")[0]
            failed_message = store.get_message(message.id)
            queued = store.list_outbox(status="queued")
            duplicate_claim = store.claim_outbox(job.id)

        self.assertEqual(uncertain.error, "send result unknown")
        self.assertEqual(failed_message.status, "failed")
        self.assertEqual(failed_message.error, "send result unknown")
        self.assertEqual(queued, [])
        self.assertIsNone(duplicate_claim)

    def test_recovery_marks_interrupted_sends_uncertain_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            first_store = SQLiteStore(db_path)
            message, _ = first_store.record_message("fp-restart", "chat", "alice", "text", "question")
            first_store.mark_messages_processing([message.id])
            job = first_store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            first_store.claim_outbox(job.id)

            restarted_store = SQLiteStore(db_path)
            recovered = restarted_store.recover_interrupted_outbox()
            recovered_again = restarted_store.recover_interrupted_outbox()
            uncertain = restarted_store.list_outbox(status="uncertain")[0]
            failed_message = restarted_store.get_message(message.id)
            queued = restarted_store.list_outbox(status="queued")

        self.assertEqual(recovered, 1)
        self.assertEqual(recovered_again, 0)
        self.assertEqual(uncertain.status, "uncertain")
        self.assertEqual(failed_message.status, "failed")
        self.assertNotEqual(failed_message.error, "")
        self.assertEqual(queued, [])

    def test_outbox_retry_is_delayed_bounded_and_preserves_part_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-retry", "chat", "alice", "text", "question")
            store.mark_messages_processing([message.id])
            job = store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "first\nsecond"
            )
            first_claim = store.claim_outbox(job.id, ready_at=0)
            store.mark_outbox_part_sent(job.id, 0)
            retry_scheduled = store.schedule_outbox_retry(
                job.id,
                "send unknown",
                max_attempts=2,
                next_attempt_at=100.0,
            )
            queued = store.list_outbox(status="queued")[0]
            early_claim = store.claim_outbox(job.id, ready_at=99.0)
            second_claim = store.claim_outbox(job.id, ready_at=100.0)
            retry_exhausted = store.schedule_outbox_retry(
                job.id,
                "still unknown",
                max_attempts=2,
                next_attempt_at=200.0,
            )
            uncertain = store.list_outbox(status="uncertain")[0]

        self.assertIsNotNone(first_claim)
        self.assertTrue(retry_scheduled)
        self.assertEqual(queued.sent_parts, 1)
        self.assertEqual(queued.next_attempt_at, 100.0)
        self.assertIsNone(early_claim)
        self.assertEqual(second_claim.attempts, 2)
        self.assertEqual(second_claim.sent_parts, 1)
        self.assertFalse(retry_exhausted)
        self.assertEqual(uncertain.attempts, 2)

    def test_interrupted_outbox_can_be_requeued_for_bounded_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-recover-retry", "chat", content="question")
            store.mark_messages_processing([message.id])
            job = store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            store.claim_outbox(job.id)

            recovered = store.recover_interrupted_outbox(
                retry_enabled=True,
                max_attempts=3,
                next_attempt_at=123.0,
            )
            queued = store.list_outbox(status="queued")[0]
            source = store.get_message(message.id)

        self.assertEqual(recovered, 1)
        self.assertEqual(queued.next_attempt_at, 123.0)
        self.assertEqual(source.status, "generated")

    def test_enqueue_rolls_back_when_any_source_message_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-rollback", "chat", "alice", "text", "question")

            with self.assertRaises(RuntimeError):
                store.enqueue_outbox(
                    [message.id, 9999], "chat", "alice", "text", "auto", "question", "answer"
                )

            unchanged = store.get_message(message.id)
            queued = store.list_outbox()

        self.assertEqual(unchanged.status, "received")
        self.assertEqual(queued, [])

    def test_mark_messages_failed_persists_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-failed", "chat", "alice", "text", "question")

            store.mark_messages_failed([message.id], "llm unavailable")
            failed = store.get_message(message.id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "llm unavailable")

    def test_state_machine_rejects_terminal_message_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-terminal", "chat", "alice", "text", "question")
            store.mark_messages_processing([message.id])
            job = store.enqueue_outbox(
                [message.id], "chat", "alice", "text", "auto", "question", "answer"
            )
            store.claim_outbox(job.id)
            store.mark_outbox_sent(job.id)

            with self.assertRaisesRegex(RuntimeError, "state transitions are invalid"):
                store.mark_messages_processing([message.id])

            sent = store.get_message(message.id)

        self.assertEqual(sent.status, "sent")

    def test_outbox_requires_processing_source_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-received", "chat", "alice", "text", "question")

            with self.assertRaisesRegex(RuntimeError, "must be processing or generated"):
                store.enqueue_outbox(
                    [message.id], "chat", "alice", "text", "auto", "question", "answer"
                )

            unchanged = store.get_message(message.id)
            queued = store.list_outbox()

        self.assertEqual(unchanged.status, "received")
        self.assertEqual(queued, [])

    def test_list_messages_returns_only_requested_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            received, _ = store.record_message("fp-received-list", "chat", content="one")
            processing, _ = store.record_message("fp-processing-list", "chat", content="two")
            store.mark_messages_processing([processing.id])

            received_rows = store.list_messages("received")
            processing_rows = store.list_messages("processing")

        self.assertEqual([message.id for message in received_rows], [received.id])
        self.assertEqual([message.id for message in processing_rows], [processing.id])

    def test_batch_messages_keep_order_and_resolved_content_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteStore(db_path)
            first, _ = store.record_message("fp-batch-1", "chat", content="voice")
            second, _ = store.record_message("fp-batch-2", "chat", content="text")

            batch = store.append_message_to_batch(first.id, "chat", 10.0)
            reused = store.append_message_to_batch(second.id, "chat", 20.0)
            store.set_message_resolved_content(first.id, "voice transcript")

            restarted_store = SQLiteStore(db_path)
            open_batches = restarted_store.list_open_batches()
            messages = restarted_store.get_batch_messages(batch.id)

        self.assertIsInstance(batch, MessageBatch)
        self.assertEqual(reused.id, batch.id)
        self.assertEqual(open_batches[0].deadline_at, 20.0)
        self.assertEqual([message.id for message in messages], [first.id, second.id])
        self.assertEqual([message.batch_position for message in messages], [1, 2])
        self.assertEqual(messages[0].resolved_content, "voice transcript")

    def test_record_buffered_message_is_atomic_and_duplicate_does_not_extend_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")

            first, batch, created = store.record_buffered_message(
                "fp-buffered", "chat", "alice", "text", "hello", 10.0
            )
            duplicate, duplicate_batch, duplicate_created = store.record_buffered_message(
                "fp-buffered", "chat", "alice", "text", "hello", 99.0
            )
            persisted_batch = store.list_open_batches()[0]
            messages = store.get_batch_messages(batch.id)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, first)
        self.assertEqual(duplicate_batch.id, batch.id)
        self.assertEqual(persisted_batch.deadline_at, 10.0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].batch_position, 1)

    def test_concurrent_record_buffered_message_creates_one_message_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            stores = (SQLiteStore(db_path), SQLiteStore(db_path))
            barrier = Barrier(2)

            def record(item: tuple[SQLiteStore, float]) -> tuple[StoredMessage, MessageBatch, bool]:
                store, deadline = item
                barrier.wait()
                return store.record_buffered_message(
                    "fp-buffered-concurrent", "chat", "alice", "text", "hello", deadline
                )

            deadlines = (10.0, 20.0)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(record, zip(stores, deadlines)))

            created_indexes = [index for index, result in enumerate(results) if result[2]]
            persisted_store = SQLiteStore(db_path)
            batches = persisted_store.list_open_batches()
            messages = persisted_store.get_batch_messages(batches[0].id)

        self.assertEqual(len(created_indexes), 1)
        self.assertEqual(results[0][0].id, results[1][0].id)
        self.assertEqual(results[0][1].id, results[1][1].id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(batches[0].deadline_at, deadlines[created_indexes[0]])

    def test_concurrent_append_reuses_one_collecting_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            setup_store = SQLiteStore(db_path)
            first, _ = setup_store.record_message("fp-batch-concurrent-1", "chat", content="one")
            second, _ = setup_store.record_message("fp-batch-concurrent-2", "chat", content="two")
            stores = (SQLiteStore(db_path), SQLiteStore(db_path))
            barrier = Barrier(2)

            def append(item: tuple[SQLiteStore, int]) -> MessageBatch:
                store, message_id = item
                barrier.wait()
                return store.append_message_to_batch(message_id, "chat", 30.0)

            with ThreadPoolExecutor(max_workers=2) as executor:
                batches = list(executor.map(append, zip(stores, (first.id, second.id))))

            persisted_batches = setup_store.list_open_batches()
            messages = setup_store.get_batch_messages(batches[0].id)

        self.assertEqual(batches[0].id, batches[1].id)
        self.assertEqual(len(persisted_batches), 1)
        self.assertEqual({message.id for message in messages}, {first.id, second.id})
        self.assertEqual([message.batch_position for message in messages], [1, 2])

    def test_append_and_processing_claim_race_never_half_assigns_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            setup_store = SQLiteStore(db_path)
            first, old_batch, _ = setup_store.record_buffered_message(
                "fp-race-first", "chat", "alice", "text", "one", 10.0
            )
            second, _ = setup_store.record_message(
                "fp-race-second", "chat", "alice", "text", "two"
            )
            append_store = SQLiteStore(db_path)
            claim_store = SQLiteStore(db_path)
            barrier = Barrier(2)

            def append() -> MessageBatch:
                barrier.wait()
                return append_store.append_message_to_batch(second.id, "chat", 20.0)

            def claim() -> bool:
                barrier.wait()
                return claim_store.mark_batch_processing(old_batch.id)

            with ThreadPoolExecutor(max_workers=2) as executor:
                append_future = executor.submit(append)
                claim_future = executor.submit(claim)
                appended_batch = append_future.result()
                claimed = claim_future.result()

            persisted_second = setup_store.get_message(second.id)
            old_messages = setup_store.get_batch_messages(old_batch.id)
            open_batches = setup_store.list_open_batches()

        self.assertTrue(claimed)
        self.assertIsNotNone(persisted_second.batch_id)
        self.assertIsNotNone(persisted_second.batch_position)
        if appended_batch.id == old_batch.id:
            self.assertEqual([message.id for message in old_messages], [first.id, second.id])
            self.assertEqual([batch.id for batch in open_batches], [old_batch.id])
        else:
            self.assertEqual([message.id for message in old_messages], [first.id])
            self.assertEqual(
                [batch.id for batch in open_batches],
                [old_batch.id, appended_batch.id],
            )

    def test_batch_state_machine_prevents_terminal_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-batch-state", "chat", content="one")
            batch = store.append_message_to_batch(message.id, "chat", 10.0)

            store.update_batch_deadline(batch.id, 15.0)
            first_claim = store.mark_batch_processing(batch.id)
            duplicate_claim = store.mark_batch_processing(batch.id)
            processing = store.list_open_batches()[0]
            store.mark_batch_completed(batch.id)
            store.mark_batch_completed(batch.id)

            with self.assertRaisesRegex(RuntimeError, "state transition is invalid"):
                store.mark_batch_processing(batch.id)
            with self.assertRaisesRegex(RuntimeError, "state transition is invalid"):
                store.mark_batch_failed(batch.id, "late failure")
            with self.assertRaisesRegex(RuntimeError, "collecting batch deadline"):
                store.update_batch_deadline(batch.id, 20.0)

            other, _ = store.record_message("fp-batch-failed", "other", content="two")
            failed_batch = store.append_message_to_batch(other.id, "other", 10.0)
            store.mark_batch_failed(failed_batch.id, "media unavailable")
            with self.assertRaisesRegex(RuntimeError, "state transition is invalid"):
                store.mark_batch_processing(failed_batch.id)

            open_batches = store.list_open_batches()

        self.assertEqual(processing.status, "processing")
        self.assertEqual(processing.deadline_at, 15.0)
        self.assertTrue(first_claim)
        self.assertFalse(duplicate_claim)
        self.assertEqual(open_batches, [])

    def test_batch_claim_honors_current_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            _, batch, _ = store.record_buffered_message(
                "fp-deadline-claim", "chat", "alice", "text", "one", 20.0
            )

            early_claim = store.mark_batch_processing(batch.id, ready_at=19.0)
            still_collecting = store.get_message_batch(batch.id)
            due_claim = store.mark_batch_processing(batch.id, ready_at=20.0)

        self.assertFalse(early_claim)
        self.assertEqual(still_collecting.status, "collecting")
        self.assertTrue(due_claim)

    def test_list_open_batches_can_return_more_than_default_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            for index in range(105):
                store.record_buffered_message(
                    f"fp-open-{index}",
                    f"chat-{index}",
                    "alice",
                    "text",
                    "one",
                    10.0,
                )

            default_page = store.list_open_batches()
            all_batches = store.list_open_batches(limit=None)

        self.assertEqual(len(default_page), 100)
        self.assertEqual(len(all_batches), 105)

    def test_blocked_batches_are_separate_from_open_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, batch, _ = store.record_buffered_message(
                "fp-blocked", "chat", "alice", "voice", "voice", 10.0
            )

            store.mark_batch_blocked(batch.id, "managed media missing")
            store.mark_batch_blocked(batch.id, "managed media still missing")
            blocked = store.list_blocked_batches()
            open_batches = store.list_open_batches()
            batch_messages = store.get_batch_messages(batch.id)

            with self.assertRaisesRegex(RuntimeError, "state transition is invalid"):
                store.mark_batch_processing(batch.id)

        self.assertEqual(open_batches, [])
        self.assertEqual(blocked[0].id, batch.id)
        self.assertEqual(blocked[0].status, "blocked")
        self.assertEqual(blocked[0].error, "managed media still missing")
        self.assertEqual(batch_messages[0].id, message.id)

    def test_message_media_metadata_and_extracted_text_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteStore(db_path)
            message, _ = store.record_message("fp-media", "chat", msg_type="voice")

            pending = store.upsert_message_media(
                message.id,
                "voice",
                source_path="wx/cache/voice.silk",
            )
            ready = store.upsert_message_media(
                message.id,
                "voice",
                source_path="wx/cache/voice.silk",
                managed_path="data/media/voice.silk",
                checksum="sha256-value",
                size_bytes=123,
                status="ready",
            )
            store.mark_message_media_processed(message.id, "transcribed text")
            store.set_message_resolved_content(message.id, "transcribed text")

            restarted_store = SQLiteStore(db_path)
            processed = restarted_store.get_message_media(message.id)
            resolved = restarted_store.get_message(message.id)

        self.assertIsInstance(pending, MessageMedia)
        self.assertEqual(ready.managed_path, "data/media/voice.silk")
        self.assertEqual(processed.status, "processed")
        self.assertEqual(processed.checksum, "sha256-value")
        self.assertEqual(processed.size_bytes, 123)
        self.assertEqual(processed.extracted_text, "transcribed text")
        self.assertEqual(processed.error, "")
        self.assertEqual(resolved.resolved_content, "transcribed text")

    def test_message_media_failure_and_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")
            message, _ = store.record_message("fp-media-failed", "chat", msg_type="image")
            store.upsert_message_media(message.id, "image")
            store.mark_message_media_failed(message.id, "copy failed")

            failed = store.get_message_media(message.id)
            with store._connect() as conn:
                foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
                conn.execute("DELETE FROM messages WHERE id = ?", (message.id,))
            deleted = store.get_message_media(message.id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "copy failed")
        self.assertEqual(foreign_keys, 1)
        self.assertIsNone(deleted)

    def test_existing_messages_table_is_migrated_with_batch_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL UNIQUE,
                        chat_name TEXT NOT NULL,
                        sender TEXT NOT NULL DEFAULT '',
                        msg_type TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'received',
                        reply TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        received_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO messages (
                        fingerprint, chat_name, content, received_at, updated_at
                    )
                    VALUES ('legacy-fp', 'legacy-chat', 'hello', 'old', 'old')
                    """
                )

            store = SQLiteStore(db_path)
            legacy = store.get_message_by_fingerprint("legacy-fp")
            batch = store.append_message_to_batch(legacy.id, "legacy-chat", 50.0)
            migrated = store.get_batch_messages(batch.id)[0]
            with store._connect() as conn:
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(messages)").fetchall()
                }

        self.assertTrue({"batch_id", "batch_position", "resolved_content"} <= columns)
        self.assertEqual(migrated.batch_id, batch.id)
        self.assertEqual(migrated.batch_position, 1)
        self.assertEqual(migrated.resolved_content, "")

    def test_existing_batch_schema_is_migrated_for_blocked_and_media_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE message_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'collecting'
                            CHECK(status IN ('collecting', 'processing', 'completed', 'failed')),
                        deadline_at REAL NOT NULL,
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO message_batches (
                        chat_name, status, deadline_at, error, created_at, updated_at
                    )
                    VALUES ('legacy-chat', 'collecting', 10, '', 'old', 'old')
                    """
                )

            store = SQLiteStore(db_path)
            store.mark_batch_blocked(1, "legacy media missing")
            blocked = store.list_blocked_batches()
            with store._connect() as conn:
                batch_schema = str(
                    conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name = 'message_batches'"
                    ).fetchone()["sql"]
                )
                media_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(message_media)").fetchall()
                }

        self.assertIn("'blocked'", batch_schema)
        self.assertIn("size_bytes", media_columns)
        self.assertEqual(blocked[0].status, "blocked")
        self.assertEqual(blocked[0].error, "legacy media missing")

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

    def test_failed_reply_queue_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteStore(Path(temp_dir) / "state.sqlite3")

            reply_id = store.enqueue_failed_reply(
                chat_name="chat",
                sender="alice",
                msg_type="text",
                policy="auto",
                content="question",
                reply="answer",
                error="send failed",
            )
            pending = store.list_failed_replies()
            store.mark_failed_reply_attempt(reply_id, "still failed", max_attempts=2)
            pending_after_attempt = store.list_failed_replies()
            store.mark_failed_reply_attempt(reply_id, "failed again", max_attempts=2)
            failed = store.list_failed_replies(status="failed")

        self.assertEqual(pending[0].id, reply_id)
        self.assertEqual(pending[0].reply, "answer")
        self.assertEqual(pending_after_attempt[0].attempts, 1)
        self.assertEqual(failed[0].status, "failed")
        self.assertEqual(failed[0].attempts, 2)


if __name__ == "__main__":
    unittest.main()
