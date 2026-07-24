from __future__ import annotations

import sys
import unittest
import uuid
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

import main
from wechat_ai.single_instance import (
    MUTEX_PREFIX,
    SingleInstanceAlreadyRunningError,
    SingleInstanceLock,
    SingleInstanceUnsupportedError,
    WECHAT_DESKTOP_MUTEX_NAME,
    build_mutex_name,
)


def make_config(account_id: str = "", state_db_file: str = "") -> SimpleNamespace:
    return SimpleNamespace(account_id=account_id, state_db_file=state_db_file)


class MutexNameTests(unittest.TestCase):
    def test_all_configurations_share_one_desktop_lock_per_windows_session(self) -> None:
        first = build_mutex_name(
            make_config(account_id="tenant-a", state_db_file="one.sqlite3"),
            "one.env",
        )
        second = build_mutex_name(
            make_config(account_id="tenant-a", state_db_file="two.sqlite3"),
            "two.env",
        )

        self.assertEqual(first, second)
        self.assertEqual(first, second)
        self.assertEqual(first, WECHAT_DESKTOP_MUTEX_NAME)
        self.assertTrue(first.startswith("Local\\"))


@unittest.skipUnless(sys.platform == "win32", "Windows named mutex test")
class WindowsMutexTests(unittest.TestCase):
    def unique_name(self) -> str:
        return f"{MUTEX_PREFIX}test-{uuid.uuid4().hex}"

    def test_duplicate_acquire_is_rejected(self) -> None:
        first = SingleInstanceLock(self.unique_name())
        second = SingleInstanceLock(first.mutex_name)
        first.acquire()
        try:
            with self.assertRaisesRegex(
                SingleInstanceAlreadyRunningError,
                "already running",
            ):
                second.acquire()
        finally:
            first.release()

    def test_release_allows_the_same_name_to_be_acquired_again(self) -> None:
        mutex_name = self.unique_name()
        first = SingleInstanceLock(mutex_name)
        first.acquire()
        first.release()

        second = SingleInstanceLock(mutex_name)
        second.acquire()
        second.release()

    def test_context_manager_releases_mutex_after_an_exception(self) -> None:
        mutex_name = self.unique_name()

        with self.assertRaisesRegex(RuntimeError, "bot failed"):
            with SingleInstanceLock(mutex_name):
                raise RuntimeError("bot failed")

        with SingleInstanceLock(mutex_name):
            pass


class PlatformBehaviorTests(unittest.TestCase):
    def test_non_windows_does_not_fall_back_to_another_lock(self) -> None:
        lock = SingleInstanceLock("test", platform_name="linux")

        with self.assertRaisesRegex(SingleInstanceUnsupportedError, "requires Windows"):
            lock.acquire()


class RunWechatBotLockTests(unittest.TestCase):
    def test_lock_is_held_before_clients_and_bot_are_created(self) -> None:
        events: list[str] = []
        config = make_config(account_id="tenant-a", state_db_file="state.sqlite3")

        class RecordingLock:
            def __enter__(self) -> "RecordingLock":
                events.append("lock-acquired")
                return self

            def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
                events.append("lock-released")

        class RecordingBot:
            def start(self) -> None:
                events.append("bot-started")

        def create_llm(loaded_config: object) -> object:
            self.assertIn("lock-acquired", events)
            events.append("llm-created")
            return object()

        def create_bot(*, config: object, llm_client: object) -> RecordingBot:
            self.assertIn("lock-acquired", events)
            events.append("bot-created")
            return RecordingBot()

        with patch("main.load_config", return_value=config):
            with patch("main.SingleInstanceLock.from_config", return_value=RecordingLock()) as lock_factory:
                with patch("main.LLMClient", side_effect=create_llm):
                    with patch("main.WeChatAutoReplyBot", side_effect=create_bot):
                        result = main.run_wechat_bot("tenant.env")

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            ["lock-acquired", "llm-created", "bot-created", "bot-started", "lock-released"],
        )
        lock_factory.assert_called_once_with(config, "tenant.env")

    def test_duplicate_lock_failure_does_not_create_a_bot(self) -> None:
        config = make_config(account_id="tenant-a", state_db_file="state.sqlite3")

        class DuplicateLock:
            def __enter__(self) -> "DuplicateLock":
                raise SingleInstanceAlreadyRunningError(
                    "Another WeChat automation instance is already running in this Windows session."
                )

            def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
                pass

        with patch("main.load_config", return_value=config):
            with patch(
                "main.SingleInstanceLock.from_config",
                return_value=DuplicateLock(),
            ):
                with patch("main.LLMClient") as llm_class:
                    with patch("main.WeChatAutoReplyBot") as bot_class:
                        with self.assertRaisesRegex(
                            SingleInstanceAlreadyRunningError,
                            "already running in this Windows session",
                        ):
                            main.run_wechat_bot("tenant.env")

        llm_class.assert_not_called()
        bot_class.assert_not_called()


class RunConnectorAgentLockTests(unittest.TestCase):
    def test_local_connector_uses_the_same_desktop_lock(self) -> None:
        events: list[str] = []
        config = make_config(account_id="tenant-a", state_db_file="state.sqlite3")
        args = Namespace(
            connector_command="run-local",
            id="local-wechat-desktop",
            channel="wechat_desktop",
            interval=1.0,
            echo=True,
        )

        class RecordingLock:
            def __enter__(self) -> "RecordingLock":
                events.append("lock-acquired")
                return self

            def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
                events.append("lock-released")

        def run_agent(*args: object, **kwargs: object) -> None:
            self.assertIn("lock-acquired", events)
            events.append("connector-started")

        with patch("main.load_config", return_value=config):
            with patch("main.SingleInstanceLock.from_config", return_value=RecordingLock()) as lock_factory:
                with patch("main.run_local_connector_agent", side_effect=run_agent):
                    result = main.run_connector_command("tenant.env", args)

        self.assertEqual(result, 0)
        self.assertEqual(events, ["lock-acquired", "connector-started", "lock-released"])
        lock_factory.assert_called_once_with(config, "tenant.env")


if __name__ == "__main__":
    unittest.main()
