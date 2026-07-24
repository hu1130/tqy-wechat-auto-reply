from __future__ import annotations

import math
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar


T = TypeVar("T")
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+){1,3}")


class OperationGateError(RuntimeError):
    """Base error for guarded WeChat UI operations."""


class OperationTimeoutError(OperationGateError):
    """Raised when an operation cannot finish inside its time budget."""


class OperationRejectedError(OperationGateError):
    """Raised while a previously timed-out operation is still running."""


@dataclass(frozen=True)
class OperationGateSnapshot:
    active: bool
    operation_name: str = ""
    timed_out: bool = False
    started_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "operation_name": self.operation_name,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
        }


@dataclass
class _ActiveOperation:
    name: str
    started_at: float
    done: threading.Event
    timed_out: bool = False
    result: Any = None
    error: BaseException | None = None


class OperationGate:
    """Serialize UI calls and fail closed when a timed-out call is still alive.

    Python cannot safely terminate a thread that is blocked inside wxauto. Each
    operation therefore runs in a daemon thread. If it times out, the gate stays
    blocked until that exact operation returns; no later UI operation is started.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._active: _ActiveOperation | None = None

    def run(
        self,
        operation_name: str,
        operation: Callable[[], T],
        timeout_seconds: float,
    ) -> T:
        name = str(operation_name).strip()
        if not name:
            raise ValueError("operation_name must not be empty")
        if not callable(operation):
            raise TypeError("operation must be callable")
        timeout = self._validate_timeout(timeout_seconds)
        deadline = self._clock() + timeout

        with self._condition:
            while self._active is not None:
                if self._active.timed_out:
                    raise OperationRejectedError(
                        f"WeChat UI operation rejected: timed-out operation "
                        f"{self._active.name!r} is still running."
                    )
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise OperationTimeoutError(
                        f"WeChat UI operation {name!r} timed out waiting for the gate."
                    )
                self._condition.wait(remaining)

            active = _ActiveOperation(
                name=name,
                started_at=self._clock(),
                done=threading.Event(),
            )
            self._active = active

        worker = threading.Thread(
            target=self._invoke,
            args=(active, operation),
            name=f"wechat-ui-{name}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            with self._condition:
                if self._active is active:
                    self._active = None
                    self._condition.notify_all()
            raise

        remaining = max(0.0, deadline - self._clock())
        active.done.wait(remaining)
        if not active.done.is_set():
            with self._condition:
                if not active.done.is_set():
                    active.timed_out = True
                    self._condition.notify_all()
                    raise OperationTimeoutError(
                        f"WeChat UI operation {name!r} timed out after {timeout:g} seconds; "
                        "the gate will reject later operations until it finishes."
                    )

        if active.error is not None:
            raise active.error
        return active.result

    def execute(
        self,
        operation_name: str,
        operation: Callable[[], T],
        timeout_seconds: float,
    ) -> T:
        return self.run(operation_name, operation, timeout_seconds)

    def snapshot(self) -> OperationGateSnapshot:
        with self._condition:
            if self._active is None:
                return OperationGateSnapshot(active=False)
            return OperationGateSnapshot(
                active=True,
                operation_name=self._active.name,
                timed_out=self._active.timed_out,
                started_at=self._active.started_at,
            )

    @property
    def is_blocked(self) -> bool:
        snapshot = self.snapshot()
        return snapshot.active and snapshot.timed_out

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        timeout = self._validate_timeout(timeout_seconds, allow_zero=True)
        deadline = self._clock() + timeout
        with self._condition:
            while self._active is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _invoke(self, active: _ActiveOperation, operation: Callable[[], T]) -> None:
        try:
            active.result = operation()
        except BaseException as exc:
            active.error = exc
        finally:
            active.done.set()
            with self._condition:
                if self._active is active:
                    self._active = None
                    self._condition.notify_all()

    @staticmethod
    def _validate_timeout(value: float, *, allow_zero: bool = False) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be a finite number") from exc
        if not math.isfinite(timeout) or timeout < 0 or (timeout == 0 and not allow_zero):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ValueError(f"timeout_seconds must be a finite {qualifier} number")
        return timeout


DEFAULT_MEDIA_EXTENSIONS: dict[str, frozenset[str]] = {
    "image": frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}),
    "voice": frozenset({".aac", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".silk", ".wav"}),
}


@dataclass(frozen=True)
class MediaValidationResult:
    valid: bool
    kind: str
    source_path: str
    resolved_path: str = ""
    size_bytes: int = 0
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.valid

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "kind": self.kind,
            "source_path": self.source_path,
            "resolved_path": self.resolved_path,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


class MediaSourceValidator:
    """Validate an untrusted media path without guessing or leaving its roots."""

    def __init__(
        self,
        allowed_roots: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...],
        *,
        sample_interval_seconds: float = 0.05,
        extensions: Mapping[str, set[str] | frozenset[str] | tuple[str, ...]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not allowed_roots:
            raise ValueError("at least one allowed media root is required")
        interval = float(sample_interval_seconds)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("sample_interval_seconds must be finite and non-negative")

        roots: list[Path] = []
        for raw_root in allowed_roots:
            root = Path(raw_root).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError(f"allowed media root is not a directory: {root}")
            if root not in roots:
                roots.append(root)

        raw_extensions = extensions or DEFAULT_MEDIA_EXTENSIONS
        normalized_extensions: dict[str, frozenset[str]] = {}
        for raw_kind, values in raw_extensions.items():
            kind = str(raw_kind).strip().lower()
            if not kind or not values:
                raise ValueError("media extension rules must have non-empty kinds and values")
            normalized = frozenset(
                text if text.startswith(".") else f".{text}"
                for value in values
                if (text := str(value).strip().lower())
            )
            if not normalized:
                raise ValueError(f"media kind {kind!r} has no extensions")
            normalized_extensions[kind] = normalized

        self._allowed_roots = tuple(roots)
        self._sample_interval_seconds = interval
        self._extensions = normalized_extensions
        self._sleep = sleep

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._allowed_roots

    def validate(self, source_path: str | os.PathLike[str], kind: str) -> MediaValidationResult:
        normalized_kind = str(kind).strip().lower()
        try:
            source_text = os.fspath(source_path) if source_path is not None else ""
        except TypeError:
            return self._reject(normalized_kind, repr(source_path), "invalid_path")
        if normalized_kind not in self._extensions:
            return self._reject(normalized_kind, source_text, "unsupported_kind")
        if not str(source_text).strip():
            return self._reject(normalized_kind, source_text, "empty_path")

        try:
            first_path = Path(source_text).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return self._reject(normalized_kind, source_text, "path_unavailable")
        if not self._is_allowed(first_path):
            return self._reject(normalized_kind, source_text, "outside_allowed_root", first_path)
        if first_path.suffix.lower() not in self._extensions[normalized_kind]:
            return self._reject(normalized_kind, source_text, "extension_mismatch", first_path)

        try:
            first_stat = first_path.stat()
        except OSError:
            return self._reject(normalized_kind, source_text, "file_unavailable", first_path)
        if not stat.S_ISREG(first_stat.st_mode):
            return self._reject(normalized_kind, source_text, "not_regular_file", first_path)
        if first_stat.st_size <= 0:
            return self._reject(normalized_kind, source_text, "empty_file", first_path)

        if self._sample_interval_seconds:
            self._sleep(self._sample_interval_seconds)

        try:
            second_path = Path(source_text).expanduser().resolve(strict=True)
            second_stat = second_path.stat()
        except (OSError, RuntimeError):
            return self._reject(normalized_kind, source_text, "file_unavailable", first_path)
        if second_path != first_path:
            return self._reject(normalized_kind, source_text, "path_changed", second_path)
        if not self._is_allowed(second_path):
            return self._reject(normalized_kind, source_text, "outside_allowed_root", second_path)
        if not stat.S_ISREG(second_stat.st_mode):
            return self._reject(normalized_kind, source_text, "not_regular_file", second_path)
        if second_stat.st_size <= 0:
            return self._reject(normalized_kind, source_text, "empty_file", second_path)
        if second_stat.st_size != first_stat.st_size:
            return self._reject(normalized_kind, source_text, "size_changed", second_path)

        return MediaValidationResult(
            valid=True,
            kind=normalized_kind,
            source_path=str(source_text),
            resolved_path=str(second_path),
            size_bytes=second_stat.st_size,
        )

    def require_valid(self, source_path: str | os.PathLike[str], kind: str) -> Path:
        result = self.validate(source_path, kind)
        if not result.valid:
            raise ValueError(f"media source rejected: {result.reason}")
        return Path(result.resolved_path)

    def _is_allowed(self, path: Path) -> bool:
        for root in self._allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _reject(
        kind: str,
        source_path: str,
        reason: str,
        resolved_path: Path | None = None,
    ) -> MediaValidationResult:
        return MediaValidationResult(
            valid=False,
            kind=kind,
            source_path=str(source_path),
            resolved_path=str(resolved_path or ""),
            reason=reason,
        )


@dataclass(frozen=True)
class WeChatVersionProbeResult:
    client_version: str
    adapter_name: str
    supported: bool
    detail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "client_version": self.client_version,
            "adapter_name": self.adapter_name,
            "supported": self.supported,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass(frozen=True)
class CapabilityProbeResult:
    name: str
    supported: bool
    detail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "supported": self.supported,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeSafetyDecision:
    safe_to_run: bool
    reasons: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.safe_to_run

    def to_dict(self) -> dict[str, object]:
        return {
            "safe_to_run": self.safe_to_run,
            "reasons": list(self.reasons),
            "missing_capabilities": list(self.missing_capabilities),
        }


DEFAULT_REQUIRED_CAPABILITIES = (
    "session_scan",
    "receive_text",
    "send_text",
    "polling_api",
)


@dataclass(frozen=True)
class WeChatRuntimeProbeResult:
    online: bool
    version: WeChatVersionProbeResult
    capabilities: tuple[CapabilityProbeResult, ...]
    errors: tuple[str, ...] = ()

    def evaluate(
        self,
        required_capabilities: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_CAPABILITIES,
    ) -> RuntimeSafetyDecision:
        return evaluate_wechat_runtime(self, required_capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "online": self.online,
            "version": self.version.to_dict(),
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "errors": list(self.errors),
        }


def evaluate_wechat_runtime(
    result: WeChatRuntimeProbeResult,
    required_capabilities: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_CAPABILITIES,
) -> RuntimeSafetyDecision:
    """Return a fail-closed decision: uncertainty always prevents UI activity."""

    reasons: list[str] = []
    required = tuple(dict.fromkeys(str(name).strip() for name in required_capabilities if str(name).strip()))
    version = result.version

    if not result.online:
        reasons.append("wechat_offline")
    if not version.client_version.strip():
        reasons.append("client_version_unknown")
    if not version.adapter_name.strip():
        reasons.append("adapter_unknown")
    if not version.supported:
        reasons.append("client_version_unsupported")
    if version.error.strip():
        reasons.append("version_probe_error")
    if any(str(error).strip() for error in result.errors):
        reasons.append("runtime_probe_error")

    by_name: dict[str, CapabilityProbeResult] = {}
    duplicate_names: set[str] = set()
    for capability in result.capabilities:
        name = capability.name.strip()
        if not name:
            reasons.append("capability_name_invalid")
            continue
        if name in by_name:
            duplicate_names.add(name)
        else:
            by_name[name] = capability
        if capability.error.strip():
            reasons.append(f"capability_probe_error:{name}")
    for name in sorted(duplicate_names):
        reasons.append(f"capability_probe_duplicate:{name}")

    missing = tuple(
        name
        for name in required
        if name not in by_name or not by_name[name].supported or bool(by_name[name].error.strip())
    )
    if missing:
        reasons.append("required_capabilities_unavailable")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return RuntimeSafetyDecision(
        safe_to_run=not unique_reasons,
        reasons=unique_reasons,
        missing_capabilities=missing,
    )


def detect_wechat_client_version(client: Any, explicit_version: str = "") -> str:
    """Read a client version without treating the wxauto package version as WeChat's version."""

    explicit = _normalized_version(explicit_version)
    if explicit:
        return explicit
    for attribute in ("client_version", "wechat_version", "weixin_version", "version"):
        value = getattr(client, attribute, "")
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        detected = _normalized_version(value)
        if detected:
            return detected
    if os.name != "nt":
        return ""
    for executable in _client_executable_candidates(client):
        detected = _windows_file_version(executable)
        if detected:
            return detected
    return ""


def _normalized_version(value: Any) -> str:
    match = VERSION_PATTERN.search(str(value or "").strip())
    return match.group(0) if match else ""


def _client_executable_candidates(client: Any) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for attribute in ("exe_path", "client_path", "path", "dir"):
        raw_value = getattr(client, attribute, "")
        if not isinstance(raw_value, (str, os.PathLike)) or not str(raw_value).strip():
            continue
        path = Path(raw_value).expanduser()
        if path.is_file():
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(path / name for name in ("Weixin.exe", "WeChat.exe") if (path / name).is_file())
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _windows_file_version(executable: Path) -> str:
    command = (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$p=$args[0]; (Get-Item -LiteralPath $p).VersionInfo.FileVersion",
        str(executable),
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return _normalized_version(completed.stdout)


@dataclass(frozen=True)
class ReconnectState:
    consecutive_failures: int
    delay_seconds: float
    next_attempt_at: float
    attempt_in_progress: bool
    ready: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "delay_seconds": self.delay_seconds,
            "next_attempt_at": self.next_attempt_at,
            "attempt_in_progress": self.attempt_in_progress,
            "ready": self.ready,
        }


class ReconnectBackoff:
    """Thread-safe deterministic exponential backoff state for reconnects."""

    def __init__(
        self,
        base_delay_seconds: float,
        max_delay_seconds: float,
        *,
        multiplier: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_delay_seconds = self._finite_non_negative(base_delay_seconds, "base_delay_seconds")
        self.max_delay_seconds = self._finite_non_negative(max_delay_seconds, "max_delay_seconds")
        self.multiplier = self._finite_non_negative(multiplier, "multiplier")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        if self.multiplier <= 1:
            raise ValueError("multiplier must be greater than 1")
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._delay_seconds = 0.0
        self._next_attempt_at = 0.0
        self._attempt_in_progress = False

    def can_attempt(self, now: float | None = None) -> bool:
        current = self._now(now)
        with self._lock:
            return not self._attempt_in_progress and current >= self._next_attempt_at

    def try_begin(self, now: float | None = None) -> bool:
        current = self._now(now)
        with self._lock:
            if self._attempt_in_progress or current < self._next_attempt_at:
                return False
            self._attempt_in_progress = True
            return True

    def record_failure(self, now: float | None = None) -> ReconnectState:
        current = self._now(now)
        with self._lock:
            self._consecutive_failures += 1
            exponent = self._consecutive_failures - 1
            try:
                calculated = self.base_delay_seconds * (self.multiplier**exponent)
            except OverflowError:
                calculated = math.inf
            self._delay_seconds = min(self.max_delay_seconds, calculated)
            self._next_attempt_at = current + self._delay_seconds
            self._attempt_in_progress = False
            return self._state_unlocked(current)

    def record_success(self, now: float | None = None) -> ReconnectState:
        current = self._now(now)
        with self._lock:
            self._consecutive_failures = 0
            self._delay_seconds = 0.0
            self._next_attempt_at = current
            self._attempt_in_progress = False
            return self._state_unlocked(current)

    def cancel_attempt(self, now: float | None = None) -> ReconnectState:
        current = self._now(now)
        with self._lock:
            self._attempt_in_progress = False
            return self._state_unlocked(current)

    def snapshot(self, now: float | None = None) -> ReconnectState:
        current = self._now(now)
        with self._lock:
            return self._state_unlocked(current)

    def seconds_until_retry(self, now: float | None = None) -> float:
        current = self._now(now)
        with self._lock:
            return max(0.0, self._next_attempt_at - current)

    def _state_unlocked(self, now: float) -> ReconnectState:
        return ReconnectState(
            consecutive_failures=self._consecutive_failures,
            delay_seconds=self._delay_seconds,
            next_attempt_at=self._next_attempt_at,
            attempt_in_progress=self._attempt_in_progress,
            ready=not self._attempt_in_progress and now >= self._next_attempt_at,
        )

    def _now(self, now: float | None) -> float:
        value = self._clock() if now is None else float(now)
        if not math.isfinite(value):
            raise ValueError("now must be finite")
        return value

    @staticmethod
    def _finite_non_negative(value: float, name: str) -> float:
        try:
            converted = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite non-negative number") from exc
        if not math.isfinite(converted) or converted < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
        return converted
