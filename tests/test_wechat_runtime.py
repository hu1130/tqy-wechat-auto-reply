from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wechat_ai.wechat_runtime import (
    CapabilityProbeResult,
    MediaSourceValidator,
    OperationGate,
    OperationRejectedError,
    OperationTimeoutError,
    ReconnectBackoff,
    WeChatRuntimeProbeResult,
    WeChatVersionProbeResult,
    detect_wechat_client_version,
    evaluate_wechat_runtime,
)


class OperationGateTests(unittest.TestCase):
    def test_operations_are_serialized(self) -> None:
        gate = OperationGate()
        first_started = threading.Event()
        release_first = threading.Event()
        order: list[str] = []
        results: list[str] = []

        def first() -> str:
            order.append("first-start")
            first_started.set()
            release_first.wait(1)
            order.append("first-end")
            return "one"

        def second() -> str:
            order.append("second-start")
            return "two"

        first_thread = threading.Thread(target=lambda: results.append(gate.run("first", first, 1)))
        second_thread = threading.Thread(target=lambda: results.append(gate.run("second", second, 1)))
        first_thread.start()
        self.assertTrue(first_started.wait(1))
        second_thread.start()
        time.sleep(0.02)
        self.assertNotIn("second-start", order)
        release_first.set()
        first_thread.join(1)
        second_thread.join(1)

        self.assertEqual(order, ["first-start", "first-end", "second-start"])
        self.assertCountEqual(results, ["one", "two"])

    def test_timeout_blocks_later_operations_until_old_operation_finishes(self) -> None:
        gate = OperationGate()
        release = threading.Event()
        old_finished = threading.Event()

        def stuck() -> None:
            release.wait(1)
            old_finished.set()

        with self.assertRaises(OperationTimeoutError):
            gate.run("stuck", stuck, 0.02)

        self.assertTrue(gate.is_blocked)
        with self.assertRaisesRegex(OperationRejectedError, "stuck"):
            gate.run("send", lambda: None, 0.5)
        release.set()
        self.assertTrue(old_finished.wait(1))
        self.assertTrue(gate.wait_until_idle(1))
        self.assertEqual(gate.run("send", lambda: "sent", 0.5), "sent")

    def test_operation_error_is_propagated_and_gate_is_released(self) -> None:
        gate = OperationGate()

        with self.assertRaisesRegex(RuntimeError, "wx failed"):
            gate.execute("scan", lambda: (_ for _ in ()).throw(RuntimeError("wx failed")), 1)

        self.assertFalse(gate.snapshot().active)
        self.assertEqual(gate.run("scan-again", lambda: 3, 1), 3)

    def test_waiting_caller_can_time_out_without_starting(self) -> None:
        gate = OperationGate()
        started = threading.Event()
        release = threading.Event()
        later_called = threading.Event()

        def first() -> None:
            started.set()
            release.wait(1)

        thread = threading.Thread(target=lambda: gate.run("first", first, 1))
        thread.start()
        self.assertTrue(started.wait(1))
        with self.assertRaisesRegex(OperationTimeoutError, "waiting for the gate"):
            gate.run("later", later_called.set, 0.02)
        self.assertFalse(later_called.is_set())
        release.set()
        thread.join(1)

    def test_waiting_caller_is_woken_when_active_operation_times_out(self) -> None:
        gate = OperationGate()
        operation_started = threading.Event()
        release = threading.Event()
        follower_finished = threading.Event()
        follower_errors: list[BaseException] = []

        def stuck() -> None:
            operation_started.set()
            release.wait(1)

        def run_stuck() -> None:
            with self.assertRaises(OperationTimeoutError):
                gate.run("stuck", stuck, 0.05)

        def run_follower() -> None:
            try:
                gate.run("follower", lambda: None, 1)
            except BaseException as exc:
                follower_errors.append(exc)
            finally:
                follower_finished.set()

        first = threading.Thread(target=run_stuck)
        follower = threading.Thread(target=run_follower)
        first.start()
        self.assertTrue(operation_started.wait(1))
        follower.start()

        self.assertTrue(follower_finished.wait(0.3))
        self.assertEqual(len(follower_errors), 1)
        self.assertIsInstance(follower_errors[0], OperationRejectedError)
        release.set()
        first.join(1)
        follower.join(1)

    def test_invalid_operation_arguments_are_rejected(self) -> None:
        gate = OperationGate()
        with self.assertRaises(ValueError):
            gate.run("", lambda: None, 1)
        with self.assertRaises(TypeError):
            gate.run("scan", None, 1)  # type: ignore[arg-type]
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                gate.run("scan", lambda: None, timeout)


class MediaSourceValidatorTests(unittest.TestCase):
    def test_valid_image_is_sampled_and_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "PHOTO.JPG"
            image.write_bytes(b"image-data")
            sleeps: list[float] = []
            validator = MediaSourceValidator([root], sample_interval_seconds=0.01, sleep=sleeps.append)

            result = validator.validate(image, "IMAGE")

        self.assertTrue(result.valid)
        self.assertEqual(result.size_bytes, 10)
        self.assertEqual(result.resolved_path, str(image.resolve()))
        self.assertEqual(sleeps, [0.01])
        json.dumps(result.to_dict())

    def test_outside_root_and_lexical_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "allowed"
            root.mkdir()
            outside = Path(parent) / "outside.jpg"
            outside.write_bytes(b"x")
            validator = MediaSourceValidator([root], sample_interval_seconds=0)

            direct = validator.validate(outside, "image")
            traversed = validator.validate(root / ".." / "outside.jpg", "image")

        self.assertEqual(direct.reason, "outside_allowed_root")
        self.assertEqual(traversed.reason, "outside_allowed_root")

    def test_extension_must_match_media_kind(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            voice = Path(root) / "voice.mp3"
            voice.write_bytes(b"voice")
            validator = MediaSourceValidator([root], sample_interval_seconds=0)

            wrong_kind = validator.validate(voice, "image")
            right_kind = validator.validate(voice, "voice")
            unknown_kind = validator.validate(voice, "video")

        self.assertEqual(wrong_kind.reason, "extension_mismatch")
        self.assertTrue(right_kind.valid)
        self.assertEqual(unknown_kind.reason, "unsupported_kind")

    def test_empty_missing_and_directory_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            empty = Path(root) / "empty.png"
            empty.touch()
            directory = Path(root) / "folder.png"
            directory.mkdir()
            validator = MediaSourceValidator([root], sample_interval_seconds=0)

            empty_result = validator.validate(empty, "image")
            missing_result = validator.validate(Path(root) / "missing.png", "image")
            directory_result = validator.validate(directory, "image")

        self.assertEqual(empty_result.reason, "empty_file")
        self.assertEqual(missing_result.reason, "path_unavailable")
        self.assertEqual(directory_result.reason, "not_regular_file")

    def test_non_path_source_is_rejected_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            validator = MediaSourceValidator([root], sample_interval_seconds=0)

            result = validator.validate(object(), "image")  # type: ignore[arg-type]

        self.assertEqual(result.reason, "invalid_path")

    def test_size_must_be_stable_across_two_samples(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            image = Path(root) / "changing.png"
            image.write_bytes(b"first")

            def mutate(_: float) -> None:
                with image.open("ab") as handle:
                    handle.write(b"-second")

            validator = MediaSourceValidator([root], sample_interval_seconds=0.01, sleep=mutate)
            result = validator.validate(image, "image")

        self.assertEqual(result.reason, "size_changed")

    def test_require_valid_raises_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            validator = MediaSourceValidator([root], sample_interval_seconds=0)
            with self.assertRaisesRegex(ValueError, "path_unavailable"):
                validator.require_valid(Path(root) / "missing.jpg", "image")

    def test_invalid_validator_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MediaSourceValidator([])
        with tempfile.NamedTemporaryFile() as not_a_directory:
            with self.assertRaises(ValueError):
                MediaSourceValidator([not_a_directory.name])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                MediaSourceValidator([root], sample_interval_seconds=-1)


def safe_probe() -> WeChatRuntimeProbeResult:
    return WeChatRuntimeProbeResult(
        online=True,
        version=WeChatVersionProbeResult(
            client_version="4.1.11.17",
            adapter_name="wxauto4_4_1_11",
            supported=True,
        ),
        capabilities=(
            CapabilityProbeResult("session_scan", True),
            CapabilityProbeResult("receive_text", True),
            CapabilityProbeResult("send_text", True),
            CapabilityProbeResult("polling_api", True),
        ),
    )


class RuntimeProbeTests(unittest.TestCase):
    def test_client_version_prefers_explicit_then_runtime_attribute(self) -> None:
        class Client:
            version = "Weixin 4.1.11.17 release"

        self.assertEqual(detect_wechat_client_version(Client()), "4.1.11.17")
        self.assertEqual(detect_wechat_client_version(Client(), "4.1.8.107"), "4.1.8.107")

    def test_complete_probe_is_safe_and_serializable(self) -> None:
        probe = safe_probe()

        decision = probe.evaluate()

        self.assertTrue(decision.safe_to_run)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())
        json.dumps(probe.to_dict())
        json.dumps(decision.to_dict())

    def test_unknown_or_unsupported_version_fails_closed(self) -> None:
        for version in (
            WeChatVersionProbeResult("", "adapter", True),
            WeChatVersionProbeResult("4.2", "adapter", False),
            WeChatVersionProbeResult("4.1", "", True),
            WeChatVersionProbeResult("4.1", "adapter", True, error="registry unavailable"),
        ):
            with self.subTest(version=version):
                probe = WeChatRuntimeProbeResult(True, version, safe_probe().capabilities)
                self.assertFalse(probe.evaluate().safe_to_run)

    def test_offline_and_runtime_probe_errors_fail_closed(self) -> None:
        good = safe_probe()
        probe = WeChatRuntimeProbeResult(False, good.version, good.capabilities, errors=("window probe failed",))

        decision = evaluate_wechat_runtime(probe)

        self.assertFalse(decision.safe_to_run)
        self.assertIn("wechat_offline", decision.reasons)
        self.assertIn("runtime_probe_error", decision.reasons)

    def test_missing_failed_and_duplicate_capabilities_fail_closed(self) -> None:
        good = safe_probe()
        capabilities = (
            CapabilityProbeResult("session_scan", True),
            CapabilityProbeResult("receive_text", True),
            CapabilityProbeResult("send_text", False),
            CapabilityProbeResult("send_text", True),
            CapabilityProbeResult("polling_api", True, error="probe exception"),
        )
        probe = WeChatRuntimeProbeResult(True, good.version, capabilities)

        decision = probe.evaluate()

        self.assertFalse(decision.safe_to_run)
        self.assertEqual(decision.missing_capabilities, ("send_text", "polling_api"))
        self.assertIn("capability_probe_duplicate:send_text", decision.reasons)
        self.assertIn("capability_probe_error:polling_api", decision.reasons)

    def test_required_capabilities_are_caller_selectable(self) -> None:
        good = safe_probe()
        receive_only = WeChatRuntimeProbeResult(
            True,
            good.version,
            (CapabilityProbeResult("receive_text", True),),
        )

        self.assertTrue(receive_only.evaluate(["receive_text"]).safe_to_run)
        self.assertFalse(receive_only.evaluate().safe_to_run)


class ReconnectBackoffTests(unittest.TestCase):
    def test_failures_back_off_exponentially_and_cap(self) -> None:
        backoff = ReconnectBackoff(2, 5)

        states = [backoff.record_failure(now=10), backoff.record_failure(now=12), backoff.record_failure(now=16)]

        self.assertEqual([state.delay_seconds for state in states], [2, 4, 5])
        self.assertEqual([state.next_attempt_at for state in states], [12, 16, 21])
        self.assertFalse(backoff.can_attempt(now=20.99))
        self.assertTrue(backoff.can_attempt(now=21))

    def test_try_begin_reserves_one_attempt(self) -> None:
        backoff = ReconnectBackoff(1, 8)

        self.assertTrue(backoff.try_begin(now=0))
        self.assertFalse(backoff.try_begin(now=0))
        self.assertFalse(backoff.can_attempt(now=100))
        state = backoff.cancel_attempt(now=0)

        self.assertFalse(state.attempt_in_progress)
        self.assertTrue(backoff.try_begin(now=0))

    def test_success_resets_failure_state(self) -> None:
        backoff = ReconnectBackoff(1, 8)
        backoff.record_failure(now=5)
        self.assertFalse(backoff.can_attempt(now=5.5))

        state = backoff.record_success(now=5.5)

        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.delay_seconds, 0)
        self.assertTrue(state.ready)
        self.assertTrue(backoff.can_attempt(now=5.5))

    def test_seconds_until_retry_never_becomes_negative(self) -> None:
        backoff = ReconnectBackoff(3, 30)
        backoff.record_failure(now=10)

        self.assertEqual(backoff.seconds_until_retry(now=11), 2)
        self.assertEqual(backoff.seconds_until_retry(now=20), 0)
        json.dumps(backoff.snapshot(now=20).to_dict())

    def test_zero_base_delay_is_immediately_retryable(self) -> None:
        backoff = ReconnectBackoff(0, 0)

        state = backoff.record_failure(now=10)

        self.assertEqual(state.delay_seconds, 0)
        self.assertTrue(state.ready)

    def test_invalid_backoff_configuration_is_rejected(self) -> None:
        for args in ((-1, 1), (2, 1), (1, float("inf"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ReconnectBackoff(*args)
        with self.assertRaises(ValueError):
            ReconnectBackoff(1, 2, multiplier=1)


if __name__ == "__main__":
    unittest.main()
