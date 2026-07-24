from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from wechat_ai.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryPolicy,
    TokenBucketRateLimiter,
)


class FakeClock:
    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds


class RetryPolicyTests(unittest.TestCase):
    def test_success_returns_without_sleeping(self) -> None:
        sleeps: list[float] = []
        policy = RetryPolicy(sleep=sleeps.append)

        self.assertEqual(policy.call(lambda: "ok"), "ok")
        self.assertEqual(sleeps, [])

    def test_exponential_delay_is_capped(self) -> None:
        sleeps: list[float] = []
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 5:
                raise OSError("temporary")
            return "ok"

        policy = RetryPolicy(
            max_attempts=5,
            base_delay=1,
            max_delay=3,
            sleep=sleeps.append,
        )

        self.assertEqual(policy.call(operation), "ok")
        self.assertEqual(sleeps, [1, 2, 3, 3])

    def test_last_exception_is_reraised_after_max_attempts(self) -> None:
        calls = 0

        def operation() -> None:
            nonlocal calls
            calls += 1
            raise LookupError("still failing")

        policy = RetryPolicy(max_attempts=3, base_delay=0, max_delay=0, sleep=lambda _: None)

        with self.assertRaisesRegex(LookupError, "still failing"):
            policy.call(operation)
        self.assertEqual(calls, 3)

    def test_exception_predicate_can_stop_retry(self) -> None:
        calls = 0

        def operation() -> None:
            nonlocal calls
            calls += 1
            raise ValueError("permanent")

        policy = RetryPolicy(
            max_attempts=4,
            should_retry=lambda exc: isinstance(exc, TimeoutError),
            sleep=lambda _: self.fail("must not sleep"),
        )

        with self.assertRaises(ValueError):
            policy.call(operation)
        self.assertEqual(calls, 1)

    def test_jitter_receives_backoff_and_can_be_disabled(self) -> None:
        jitter_inputs: list[float] = []
        sleeps: list[float] = []

        def jitter(delay: float) -> float:
            jitter_inputs.append(delay)
            return delay / 2

        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimeoutError
            return "ok"

        policy = RetryPolicy(
            max_attempts=3,
            base_delay=2,
            max_delay=10,
            jitter=jitter,
            sleep=sleeps.append,
        )

        self.assertEqual(policy.call(operation), "ok")
        self.assertEqual(jitter_inputs, [2, 4])
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(RetryPolicy(base_delay=2, max_delay=10).delay_for(2), 4)

    def test_caller_can_override_delay_with_retry_after(self) -> None:
        sleeps: list[float] = []
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("rate limited")
            return "ok"

        policy = RetryPolicy(max_attempts=2, base_delay=1, max_delay=10, sleep=sleeps.append)
        result = policy.call(operation, delay_override=7.5)

        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [7.5])

    def test_delay_override_can_fall_back_to_policy_delay(self) -> None:
        sleeps: list[float] = []
        calls = 0

        def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError
            return "ok"

        policy = RetryPolicy(max_attempts=2, base_delay=3, max_delay=3, sleep=sleeps.append)
        policy.call(operation, delay_override=lambda exc, retry_number, default: None)

        self.assertEqual(sleeps, [3])

    def test_policy_is_safe_for_concurrent_calls(self) -> None:
        sleep_count = 0
        sleep_lock = threading.Lock()

        def sleep(_seconds: float) -> None:
            nonlocal sleep_count
            with sleep_lock:
                sleep_count += 1

        def invoke(_index: int) -> str:
            attempts = 0

            def operation() -> str:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TimeoutError
                return "ok"

            return policy.call(operation)

        policy = RetryPolicy(max_attempts=2, base_delay=0, max_delay=0, sleep=sleep)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(invoke, range(40)))

        self.assertEqual(results, ["ok"] * 40)
        self.assertEqual(sleep_count, 40)

    def test_invalid_retry_settings_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(base_delay=2, max_delay=1)
        with self.assertRaises(ValueError):
            RetryPolicy(jitter=lambda _: -1).delay_for(1)
        self.assertEqual(RetryPolicy(base_delay=1, max_delay=9).delay_for(100_000), 9)
        self.assertEqual(RetryPolicy(base_delay=0, max_delay=9).delay_for(100_000), 0)
        with self.assertRaises(ValueError):
            RetryPolicy().call(lambda: "ok", delay_override=-1)


class CircuitBreakerTests(unittest.TestCase):
    def test_failures_open_circuit_and_reject_calls(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(2, 10, clock=clock)

        for _ in range(2):
            with self.assertRaises(TimeoutError):
                breaker.call(lambda: (_ for _ in ()).throw(TimeoutError()))

        self.assertEqual(breaker.state, CircuitState.OPEN)
        self.assertEqual(breaker.failure_count, 2)
        with self.assertRaises(CircuitOpenError):
            breaker.call(lambda: "must not run")

    def test_success_resets_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(2, 10)

        with self.assertRaises(OSError):
            breaker.call(lambda: (_ for _ in ()).throw(OSError()))
        self.assertEqual(breaker.failure_count, 1)
        self.assertEqual(breaker.call(lambda: "ok"), "ok")
        self.assertEqual(breaker.failure_count, 0)

    def test_successful_half_open_probe_closes_circuit(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(1, 5, clock=clock)
        with self.assertRaises(OSError):
            breaker.call(lambda: (_ for _ in ()).throw(OSError()))

        clock.advance(5)
        self.assertEqual(breaker.state, CircuitState.HALF_OPEN)
        self.assertEqual(breaker.call(lambda: "recovered"), "recovered")
        self.assertEqual(breaker.state, CircuitState.CLOSED)
        self.assertEqual(breaker.failure_count, 0)

    def test_failed_half_open_probe_reopens_and_restarts_timeout(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(1, 5, clock=clock)
        with self.assertRaises(OSError):
            breaker.call(lambda: (_ for _ in ()).throw(OSError()))

        clock.advance(5)
        with self.assertRaises(TimeoutError):
            breaker.call(lambda: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(breaker.state, CircuitState.OPEN)

        clock.advance(4.9)
        self.assertEqual(breaker.state, CircuitState.OPEN)
        clock.advance(0.1)
        self.assertEqual(breaker.state, CircuitState.HALF_OPEN)

    def test_only_one_half_open_probe_can_run(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(1, 1, clock=clock)
        with self.assertRaises(OSError):
            breaker.call(lambda: (_ for _ in ()).throw(OSError()))
        clock.advance(1)

        probe_started = threading.Event()
        release_probe = threading.Event()

        def probe() -> str:
            probe_started.set()
            release_probe.wait(2)
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(breaker.call, probe)
            self.assertTrue(probe_started.wait(1))
            with self.assertRaises(CircuitOpenError):
                breaker.call(lambda: "second probe")
            release_probe.set()
            self.assertEqual(future.result(timeout=1), "ok")

        self.assertEqual(breaker.state, CircuitState.CLOSED)

    def test_stale_inflight_success_cannot_close_open_circuit(self) -> None:
        breaker = CircuitBreaker(1, 60)
        first_started = threading.Event()
        release_first = threading.Event()

        def slow_success() -> str:
            first_started.set()
            release_first.wait(2)
            return "late success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(breaker.call, slow_success)
            self.assertTrue(first_started.wait(1))
            with self.assertRaises(OSError):
                breaker.call(lambda: (_ for _ in ()).throw(OSError()))
            release_first.set()
            self.assertEqual(slow.result(timeout=1), "late success")

        self.assertEqual(breaker.state, CircuitState.OPEN)

    def test_reset_closes_circuit(self) -> None:
        breaker = CircuitBreaker(1, 60)
        with self.assertRaises(OSError):
            breaker.call(lambda: (_ for _ in ()).throw(OSError()))
        breaker.reset()

        self.assertEqual(breaker.state, CircuitState.CLOSED)
        self.assertEqual(breaker.call(lambda: "ok"), "ok")

    def test_control_flow_exception_does_not_count_as_failure(self) -> None:
        breaker = CircuitBreaker(1, 60)

        with self.assertRaises(KeyboardInterrupt):
            breaker.call(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        self.assertEqual(breaker.state, CircuitState.CLOSED)
        self.assertEqual(breaker.failure_count, 0)


class TokenBucketRateLimiterTests(unittest.TestCase):
    def test_try_acquire_consumes_and_refills_tokens(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(2, capacity=2, clock=clock, sleep=clock.sleep)

        self.assertTrue(limiter.try_acquire())
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())
        clock.advance(0.5)
        self.assertTrue(limiter.try_acquire())

    def test_blocking_acquire_sleeps_until_token_is_available(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(
            2,
            capacity=2,
            initial_tokens=0,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertTrue(limiter.acquire())
        self.assertEqual(clock.sleeps, [0.5])

    def test_acquire_returns_false_at_timeout(self) -> None:
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(
            1,
            capacity=1,
            initial_tokens=0,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertFalse(limiter.acquire(timeout=0.25))
        self.assertEqual(clock.sleeps, [0.25])

    def test_concurrent_try_acquire_does_not_overdraw_bucket(self) -> None:
        limiter = TokenBucketRateLimiter(1, capacity=1)
        barrier = threading.Barrier(24)

        def acquire_once(_index: int) -> bool:
            barrier.wait()
            return limiter.try_acquire()

        with ThreadPoolExecutor(max_workers=24) as executor:
            results = list(executor.map(acquire_once, range(24)))

        self.assertEqual(sum(results), 1)
        self.assertGreaterEqual(limiter.available_tokens, 0)

    def test_invalid_bucket_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(0)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(1, capacity=0)
        with self.assertRaises(ValueError):
            TokenBucketRateLimiter(1, capacity=1, initial_tokens=2)

        limiter = TokenBucketRateLimiter(1, capacity=1)
        with self.assertRaises(ValueError):
            limiter.try_acquire(2)
        with self.assertRaises(ValueError):
            limiter.acquire(timeout=-1)


if __name__ == "__main__":
    unittest.main()
