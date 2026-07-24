from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeVar


T = TypeVar("T")
RetryPredicate = Callable[[BaseException], bool]
Jitter = Callable[[float], float]
DelayOverride = Callable[[BaseException, int, float], float | None]


def _retry_all(_error: BaseException) -> bool:
    return True


def _validate_delay(value: float, name: str) -> float:
    delay = float(value)
    if not math.isfinite(delay) or delay < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return delay


@dataclass(frozen=True)
class RetryPolicy:
    """Stateless exponential-backoff retry policy.

    ``max_attempts`` includes the initial call. ``delay_override`` can be a
    fixed delay or a callback returning a per-failure delay, for example a
    parsed Retry-After value. Returning None keeps the exponential delay.
    """

    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    should_retry: RetryPredicate = _retry_all
    jitter: Jitter | None = None
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        _validate_delay(self.base_delay, "base_delay")
        _validate_delay(self.max_delay, "max_delay")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be greater than or equal to base_delay")
        if not callable(self.should_retry):
            raise TypeError("should_retry must be callable")
        if self.jitter is not None and not callable(self.jitter):
            raise TypeError("jitter must be callable or None")
        if not callable(self.sleep):
            raise TypeError("sleep must be callable")

    def delay_for(self, retry_number: int) -> float:
        """Return the delay before a one-based retry number."""
        if retry_number < 1:
            raise ValueError("retry_number must be at least 1")
        if self.base_delay == 0:
            delay = 0.0
        else:
            try:
                delay = min(self.max_delay, math.ldexp(self.base_delay, retry_number - 1))
            except OverflowError:
                delay = self.max_delay
        if self.jitter is not None:
            delay = self.jitter(delay)
        return _validate_delay(delay, "retry delay")

    def call(
        self,
        operation: Callable[[], T],
        *,
        delay_override: float | DelayOverride | None = None,
    ) -> T:
        """Call ``operation``, re-raising its last exception when exhausted."""
        if not callable(operation):
            raise TypeError("operation must be callable")
        fixed_delay = None
        if delay_override is not None and not callable(delay_override):
            fixed_delay = _validate_delay(delay_override, "delay override")

        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if attempt >= self.max_attempts or not self.should_retry(exc):
                    raise

                retry_number = attempt
                delay = self.delay_for(retry_number)
                if fixed_delay is not None:
                    delay = fixed_delay
                elif delay_override is not None:
                    override = delay_override(exc, retry_number, delay)
                    if override is not None:
                        delay = _validate_delay(override, "delay override")
                self.sleep(delay)

        raise AssertionError("retry loop exited unexpectedly")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit breaker rejects a call."""


@dataclass(frozen=True)
class _CircuitPermit:
    generation: int
    is_probe: bool


class CircuitBreaker:
    """Thread-safe circuit breaker with one half-open probe."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        _validate_delay(recovery_timeout, "recovery_timeout")
        if recovery_timeout == 0:
            raise ValueError("recovery_timeout must be greater than 0")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._failure_threshold = failure_threshold
        self._recovery_timeout = float(recovery_timeout)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._generation = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._refresh_state_locked()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def call(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if not callable(operation):
            raise TypeError("operation must be callable")
        permit = self._acquire_permit()
        try:
            result = operation(*args, **kwargs)
        except Exception:
            self._record_failure(permit)
            raise
        except BaseException:
            self._cancel_permit(permit)
            raise
        self._record_success(permit)
        return result

    def reset(self) -> None:
        with self._lock:
            self._generation += 1
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False

    def _refresh_state_locked(self) -> None:
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self._recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False

    def _acquire_permit(self) -> _CircuitPermit:
        with self._lock:
            self._refresh_state_locked()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError("circuit breaker is open")
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError("circuit breaker half-open probe is in progress")
                self._probe_in_flight = True
                return _CircuitPermit(self._generation, True)
            return _CircuitPermit(self._generation, False)

    def _record_success(self, permit: _CircuitPermit) -> None:
        with self._lock:
            if permit.generation != self._generation:
                return
            if permit.is_probe:
                if self._state is not CircuitState.HALF_OPEN:
                    return
                self._generation += 1
                self._state = CircuitState.CLOSED
                self._opened_at = None
                self._probe_in_flight = False
            if self._state is CircuitState.CLOSED:
                self._failure_count = 0

    def _record_failure(self, permit: _CircuitPermit) -> None:
        with self._lock:
            if permit.generation != self._generation:
                return
            if permit.is_probe:
                if self._state is CircuitState.HALF_OPEN:
                    self._open_locked()
                return
            if self._state is not CircuitState.CLOSED:
                return
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_locked()

    def _cancel_permit(self, permit: _CircuitPermit) -> None:
        with self._lock:
            if (
                permit.generation == self._generation
                and permit.is_probe
                and self._state is CircuitState.HALF_OPEN
            ):
                self._probe_in_flight = False

    def _open_locked(self) -> None:
        self._generation += 1
        self._state = CircuitState.OPEN
        self._failure_count = self._failure_threshold
        self._opened_at = self._clock()
        self._probe_in_flight = False


class TokenBucketRateLimiter:
    """Thread-safe token bucket supporting blocking and non-blocking acquire."""

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        initial_tokens: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be a finite number greater than 0")
        if not math.isfinite(capacity) or capacity <= 0:
            raise ValueError("capacity must be a finite number greater than 0")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(sleep):
            raise TypeError("sleep must be callable")

        tokens = capacity if initial_tokens is None else float(initial_tokens)
        if not math.isfinite(tokens) or tokens < 0 or tokens > capacity:
            raise ValueError("initial_tokens must be between 0 and capacity")

        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = tokens
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._updated_at = self._clock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        amount = self._validate_amount(tokens)
        with self._lock:
            self._refill_locked()
            if self._tokens < amount:
                return False
            self._tokens -= amount
            return True

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> bool:
        amount = self._validate_amount(tokens)
        if timeout is not None:
            timeout = _validate_delay(timeout, "timeout")
        deadline = None if timeout is None else self._clock() + timeout

        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return True
                wait = (amount - self._tokens) / self._rate
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return False
                    wait = min(wait, remaining)
            self._sleep(wait)

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    def _validate_amount(self, tokens: float) -> float:
        amount = float(tokens)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("tokens must be a finite number greater than 0")
        if amount > self._capacity:
            raise ValueError("tokens cannot exceed bucket capacity")
        return amount

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated_at = now
