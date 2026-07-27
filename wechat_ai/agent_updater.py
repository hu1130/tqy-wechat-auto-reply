from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
STATE_FILENAME = "update-state.json"
STATE_BACKUP_FILENAME = "update-state.previous.json"
AGENT_EXECUTABLE = "WeChatAIAgent.exe"
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STAGING_PATTERN = re.compile(
    r"^\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-[0-9a-f]{32}\.staging$"
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:authorization|token|api[_ -]?key|secret|activation[_ -]?code)"
    r"\s*[:=]\s*[^\s,;]+"
)
_KNOWN_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:ak|wxact|agt|sk|bearer)_[a-z0-9._-]+\b"
)
_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s]+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:\\[^\r\n\t]*")
_STATE_STATUSES = {
    "healthy",
    "checking",
    "downloading",
    "downloaded",
    "staging",
    "switching",
    "pending_health",
    "confirming",
    "rolling_back",
    "rolled_back",
    "failed",
    "rollback_failed",
}


class UpdateError(RuntimeError):
    pass


class UpdateSecurityError(UpdateError):
    pass


class UpdateNotNewerError(UpdateError):
    pass


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise UpdateSecurityError("Agent versions must use numeric MAJOR.MINOR.PATCH format")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _validate_https_url(value: str, field_name: str) -> str:
    value = str(value).strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UpdateSecurityError(f"{field_name} must use an absolute https:// URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise UpdateSecurityError(f"{field_name} must not contain credentials or a fragment")
    return value


def canonical_manifest_payload(value: dict[str, Any]) -> bytes:
    signed = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        signed,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    package_url: str
    sha256: str
    size: int
    signature: str

    @classmethod
    def verify(
        cls,
        raw: bytes,
        public_key_base64: str,
        *,
        current_version: str,
    ) -> "ReleaseManifest":
        if not raw or len(raw) > MAX_MANIFEST_BYTES:
            raise UpdateSecurityError("Update manifest is empty or exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("Update manifest is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise UpdateSecurityError("Update manifest must be a JSON object")
        expected_keys = {
            "schema_version",
            "version",
            "package_url",
            "sha256",
            "size",
            "signature",
        }
        if set(value) != expected_keys or value.get("schema_version") != 1:
            raise UpdateSecurityError("Update manifest schema is not supported")
        try:
            public_key_raw = base64.b64decode(public_key_base64, validate=True)
            signature = base64.b64decode(str(value["signature"]), validate=True)
        except (ValueError, TypeError) as exc:
            raise UpdateSecurityError("Update signature or public key is not valid base64") from exc
        if len(public_key_raw) != 32 or len(signature) != 64:
            raise UpdateSecurityError("Update signature or Ed25519 public key has an invalid length")
        try:
            Ed25519PublicKey.from_public_bytes(public_key_raw).verify(
                signature,
                canonical_manifest_payload(value),
            )
        except (InvalidSignature, ValueError) as exc:
            raise UpdateSecurityError("Update manifest Ed25519 signature is invalid") from exc

        version = str(value["version"]).strip()
        if parse_version(version) <= parse_version(current_version):
            raise UpdateNotNewerError(
                f"Refusing update {version}; current version is {current_version}"
            )
        package_url = _validate_https_url(str(value["package_url"]), "package_url")
        checksum = str(value["sha256"]).strip().lower()
        if not _SHA256_PATTERN.fullmatch(checksum):
            raise UpdateSecurityError("Update manifest sha256 must be 64 lowercase hex characters")
        size = value["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_PACKAGE_BYTES:
            raise UpdateSecurityError("Update package size is outside the allowed range")
        return cls(version, package_url, checksum, size, str(value["signature"]))


UrlOpener = Callable[..., Any]


def fetch_release_manifest(
    manifest_url: str,
    public_key_base64: str,
    *,
    current_version: str,
    timeout_seconds: float = 20.0,
    opener: UrlOpener = urlopen,
) -> ReleaseManifest:
    url = _validate_https_url(manifest_url, "manifest_url")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with opener(request, timeout=timeout_seconds) as response:
            final_url = getattr(response, "geturl", lambda: url)()
            _validate_https_url(str(final_url), "manifest response URL")
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except HTTPError as exc:
        raise UpdateError(f"Update manifest request failed with HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise UpdateError("Update manifest request failed") from exc
    return ReleaseManifest.verify(raw, public_key_base64, current_version=current_version)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_release(
    manifest: ReleaseManifest,
    staging_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 60.0,
    opener: UrlOpener = urlopen,
) -> Path:
    _validate_https_url(manifest.package_url, "package_url")
    destination_dir = Path(staging_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"agent-{manifest.version}.zip"
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".agent-{manifest.version}-",
        suffix=".download",
        dir=destination_dir,
    )
    temp_path = Path(raw_temp)
    digest = hashlib.sha256()
    total = 0
    try:
        request = Request(
            manifest.package_url,
            headers={"Accept": "application/zip, application/octet-stream"},
            method="GET",
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                with opener(request, timeout=timeout_seconds) as response:
                    final_url = getattr(response, "geturl", lambda: manifest.package_url)()
                    _validate_https_url(str(final_url), "package response URL")
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > manifest.size or total > MAX_PACKAGE_BYTES:
                            raise UpdateSecurityError("Downloaded package exceeds its signed size")
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except HTTPError as exc:
            raise UpdateError(f"Update package request failed with HTTP {exc.code}") from exc
        except (URLError, OSError) as exc:
            raise UpdateError("Update package request failed") from exc
        if total != manifest.size:
            raise UpdateSecurityError("Downloaded package size does not match the signed manifest")
        if digest.hexdigest() != manifest.sha256:
            raise UpdateSecurityError("Downloaded package SHA-256 does not match the signed manifest")
        os.replace(temp_path, destination)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _safe_member_path(root: Path, member_name: str) -> Path:
    if not member_name or "\\" in member_name or "\x00" in member_name:
        raise UpdateSecurityError("Update archive contains an invalid path")
    pure = PurePosixPath(member_name)
    if (
        pure.is_absolute()
        or (pure.parts and ":" in pure.parts[0])
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise UpdateSecurityError("Update archive contains path traversal")
    candidate = root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise UpdateSecurityError("Update archive escapes the staging directory") from exc
    return candidate


def extract_release_archive(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                target = _safe_member_path(destination, item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UpdateSecurityError("Update archive must not contain symbolic links")
                total += item.file_size
                if total > MAX_EXTRACTED_BYTES:
                    raise UpdateSecurityError("Extracted update exceeds the size limit")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        executable = destination / AGENT_EXECUTABLE
        if not executable.is_file():
            raise UpdateSecurityError(f"Update archive is missing {AGENT_EXECUTABLE}")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_failure_details(reason: object) -> tuple[str, str]:
    raw = str(reason)
    summary = " ".join(raw.replace("\x00", " ").split())
    summary = _SENSITIVE_VALUE_PATTERN.sub("<redacted>", summary)
    summary = _KNOWN_SECRET_PATTERN.sub("<redacted>", summary)
    summary = _URL_PATTERN.sub("<url>", summary)
    summary = _WINDOWS_PATH_PATTERN.sub("<path>", summary)
    summary = summary[:240].strip() or "update operation failed"
    return summary, hashlib.sha256(summary.encode("utf-8")).hexdigest()


def _validate_timestamp(value: str, field_name: str) -> None:
    if not value:
        return
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UpdateSecurityError(f"{field_name} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise UpdateSecurityError(f"{field_name} must include a timezone")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _safe_release_directory(
    versions_dir: Path,
    version: str,
    *,
    require_exists: bool,
) -> Path:
    parse_version(version)
    versions_dir = versions_dir.expanduser()
    if _is_link_or_reparse(versions_dir):
        raise UpdateSecurityError("Versions directory must not be a symbolic link or reparse point")
    root = versions_dir.resolve()
    candidate = versions_dir / version
    if candidate.parent.resolve() != root:
        raise UpdateSecurityError("Release directory escapes the versions directory")
    if _is_link_or_reparse(candidate):
        raise UpdateSecurityError("Release directory must not be a symbolic link or reparse point")
    if not candidate.exists():
        if require_exists:
            raise UpdateError(f"Agent release is unavailable: {version}")
        return candidate
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise UpdateSecurityError("Release directory escapes the versions directory") from exc
    if not candidate.is_dir():
        raise UpdateSecurityError("Release path is not a directory")
    return candidate


def _safe_release_executable(versions_dir: Path, version: str) -> Path:
    release_dir = _safe_release_directory(versions_dir, version, require_exists=True)
    executable = release_dir / AGENT_EXECUTABLE
    if _is_link_or_reparse(executable) or not executable.is_file():
        raise UpdateError(f"Agent executable is unavailable for release {version}")
    try:
        executable.resolve().relative_to(release_dir.resolve())
    except ValueError as exc:
        raise UpdateSecurityError("Agent executable escapes its release directory") from exc
    return executable


def _find_previous_safe_release(
    versions_dir: Path,
    active_version: str,
    *,
    excluded: set[str] | None = None,
) -> str:
    if not versions_dir.is_dir() or _is_link_or_reparse(versions_dir):
        return ""
    active = parse_version(active_version)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for candidate in versions_dir.iterdir():
        if candidate.name in (excluded or set()):
            continue
        try:
            parsed = parse_version(candidate.name)
            if parsed >= active:
                continue
            _safe_release_executable(versions_dir, candidate.name)
        except UpdateError:
            continue
        candidates.append((parsed, candidate.name))
    return max(candidates, default=((0, 0, 0), ""))[1]


def _assert_removal_tree_is_safe(path: Path, versions_root: Path) -> None:
    if _is_link_or_reparse(path):
        raise UpdateSecurityError("Refusing to remove a symbolic link or reparse point")
    try:
        path.resolve().relative_to(versions_root.resolve())
    except ValueError as exc:
        raise UpdateSecurityError(
            "Refusing to remove a path outside the versions directory"
        ) from exc
    if path.is_dir():
        for child in path.iterdir():
            _assert_removal_tree_is_safe(child, versions_root)


@dataclass(frozen=True)
class UpdateState:
    active_version: str
    previous_version: str = ""
    pending_version: str = ""
    rollback_version: str = ""
    failed_version: str = ""
    target_version: str = ""
    status: str = "healthy"
    attempt_id: str = ""
    checked_at: str = ""
    downloaded_at: str = ""
    staged_at: str = ""
    switched_at: str = ""
    pending_since: str = ""
    confirmed_at: str = ""
    rolled_back_at: str = ""
    failed_at: str = ""
    cleanup_at: str = ""
    updated_at: str = ""
    failure_code: str = ""
    failure_summary: str = ""
    failure_digest: str = ""

    @property
    def failure_reason(self) -> str:
        return self.failure_summary

    def validate(self) -> "UpdateState":
        parse_version(self.active_version)
        for value in (
            self.previous_version,
            self.pending_version,
            self.rollback_version,
            self.failed_version,
            self.target_version,
        ):
            if value:
                parse_version(value)
        if self.status not in _STATE_STATUSES:
            raise UpdateSecurityError("Update state status is invalid")
        normalized = self
        if self.status in {
            "switching",
            "pending_health",
            "confirming",
            "rolling_back",
            "rollback_failed",
        }:
            target = self.target_version or self.pending_version
            rollback = self.rollback_version or self.previous_version
            if not target or not rollback:
                raise UpdateSecurityError("Pending update state is incomplete")
            normalized = replace(
                normalized,
                target_version=target,
                pending_version=self.pending_version or target,
                rollback_version=rollback,
            )
        if normalized.status in {"pending_health", "confirming", "rolling_back", "rollback_failed"}:
            if normalized.pending_version != normalized.active_version:
                raise UpdateSecurityError("Pending update must be the active release")
            _validate_timestamp(normalized.pending_since, "pending_since")
            if not normalized.pending_since:
                raise UpdateSecurityError("Pending update timestamp is missing")
        if (
            normalized.status in {"downloading", "downloaded", "staging"}
            and not normalized.target_version
        ):
            raise UpdateSecurityError("Update target version is missing")
        if normalized.failure_code and not _FAILURE_CODE_PATTERN.fullmatch(
            normalized.failure_code
        ):
            raise UpdateSecurityError("Update failure code is invalid")
        if len(normalized.failure_summary) > 240 or any(
            char in normalized.failure_summary for char in "\r\n\x00"
        ):
            raise UpdateSecurityError("Update failure summary is invalid")
        if normalized.failure_digest and not _SHA256_PATTERN.fullmatch(
            normalized.failure_digest
        ):
            raise UpdateSecurityError("Update failure digest is invalid")
        if normalized.status in {"failed", "rolled_back", "rollback_failed"} and not (
            normalized.failure_code
            and normalized.failure_summary
            and normalized.failure_digest
        ):
            raise UpdateSecurityError("Update failure state is incomplete")
        if normalized.attempt_id and not re.fullmatch(r"[0-9a-f]{32}", normalized.attempt_id):
            raise UpdateSecurityError("Update attempt identifier is invalid")
        for field_name in (
            "checked_at",
            "downloaded_at",
            "staged_at",
            "switched_at",
            "pending_since",
            "confirmed_at",
            "rolled_back_at",
            "failed_at",
            "cleanup_at",
            "updated_at",
        ):
            _validate_timestamp(str(getattr(normalized, field_name)), field_name)
        return normalized

    def to_dict(self) -> dict[str, Any]:
        state = self.validate()
        return {
            "schema_version": 2,
            "active_version": state.active_version,
            "previous_version": state.previous_version,
            "pending_version": state.pending_version,
            "rollback_version": state.rollback_version,
            "failed_version": state.failed_version,
            "target_version": state.target_version,
            "status": state.status,
            "attempt_id": state.attempt_id,
            "checked_at": state.checked_at,
            "downloaded_at": state.downloaded_at,
            "staged_at": state.staged_at,
            "switched_at": state.switched_at,
            "pending_since": state.pending_since,
            "confirmed_at": state.confirmed_at,
            "rolled_back_at": state.rolled_back_at,
            "failed_at": state.failed_at,
            "cleanup_at": state.cleanup_at,
            "updated_at": state.updated_at,
            "failure_code": state.failure_code,
            "failure_summary": state.failure_summary,
            "failure_digest": state.failure_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UpdateState":
        schema_version = value.get("schema_version")
        if schema_version not in {1, 2}:
            raise UpdateSecurityError("Update state schema is not supported")
        if schema_version == 1:
            reason = str(value.get("failure_reason", ""))
            summary, digest = _safe_failure_details(reason) if reason else ("", "")
            status = str(value.get("status", ""))
            pending = str(value.get("pending_version", ""))
            previous = str(value.get("previous_version", ""))
            return cls(
                active_version=str(value.get("active_version", "")),
                previous_version=previous,
                pending_version=pending,
                rollback_version=previous if status == "pending_health" else "",
                failed_version=str(value.get("failed_version", "")),
                target_version=pending or str(value.get("failed_version", "")),
                status=status,
                pending_since=str(value.get("pending_since", "")),
                updated_at=str(value.get("pending_since", "")),
                failure_code="legacy_failure" if reason else "",
                failure_summary=summary,
                failure_digest=digest,
            ).validate()
        return cls(
            active_version=str(value.get("active_version", "")),
            previous_version=str(value.get("previous_version", "")),
            pending_version=str(value.get("pending_version", "")),
            rollback_version=str(value.get("rollback_version", "")),
            failed_version=str(value.get("failed_version", "")),
            target_version=str(value.get("target_version", "")),
            status=str(value.get("status", "")),
            attempt_id=str(value.get("attempt_id", "")),
            checked_at=str(value.get("checked_at", "")),
            downloaded_at=str(value.get("downloaded_at", "")),
            staged_at=str(value.get("staged_at", "")),
            switched_at=str(value.get("switched_at", "")),
            pending_since=str(value.get("pending_since", "")),
            confirmed_at=str(value.get("confirmed_at", "")),
            rolled_back_at=str(value.get("rolled_back_at", "")),
            failed_at=str(value.get("failed_at", "")),
            cleanup_at=str(value.get("cleanup_at", "")),
            updated_at=str(value.get("updated_at", "")),
            failure_code=str(value.get("failure_code", "")),
            failure_summary=str(
                value.get("failure_summary", value.get("failure_reason", ""))
            ),
            failure_digest=str(value.get("failure_digest", "")),
        ).validate()


class UpdateStateStore:
    def __init__(self, install_root: str | os.PathLike[str]) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.state_dir = self.install_root / "state"
        self.path = self.state_dir / STATE_FILENAME
        self.backup_path = self.state_dir / STATE_BACKUP_FILENAME

    @staticmethod
    def _decode(raw: bytes) -> UpdateState:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("Update state is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise UpdateSecurityError("Update state must be a JSON object")
        return UpdateState.from_dict(payload)

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=self.state_dir,
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temp_path.unlink(missing_ok=True)

    def _save(self, state: UpdateState, *, backup_current: bool) -> UpdateState:
        state = replace(state.validate(), updated_at=_utc_now()).validate()
        payload = (
            json.dumps(
                state.to_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if backup_current and self.path.exists():
            try:
                current = self.path.read_bytes()
                self._decode(current)
            except (OSError, UpdateSecurityError):
                pass
            else:
                self._atomic_write(self.backup_path, current)
        self._atomic_write(self.path, payload)
        return state

    def save(self, state: UpdateState) -> UpdateState:
        return self._save(state, backup_current=True)

    def _failure_state(
        self,
        state: UpdateState,
        *,
        code: str,
        reason: object,
        status: str = "failed",
    ) -> UpdateState:
        summary, digest = _safe_failure_details(reason)
        target = state.target_version or state.pending_version
        failed_version = (
            target
            if target and target != state.active_version
            else state.failed_version
        )
        return replace(
            state,
            status=status,
            pending_version="" if status == "failed" else state.pending_version,
            rollback_version="" if status == "failed" else state.rollback_version,
            failed_version=failed_version,
            target_version=target,
            failed_at=_utc_now(),
            failure_code=code,
            failure_summary=summary,
            failure_digest=digest,
        ).validate()

    def initialize(self, version: str) -> UpdateState:
        parse_version(version)
        if self.path.exists() or self.backup_path.exists():
            return self.recover_interrupted()
        return self.save(UpdateState(active_version=version))

    def load(self) -> UpdateState:
        try:
            return self._decode(self.path.read_bytes())
        except (OSError, UpdateSecurityError) as primary_error:
            try:
                backup = self._decode(self.backup_path.read_bytes())
            except (OSError, UpdateSecurityError) as backup_error:
                raise UpdateSecurityError(
                    f"Cannot read update state or its atomic backup: {self.path}"
                ) from backup_error
            if backup.status in {"pending_health", "confirming"}:
                summary, digest = _safe_failure_details(primary_error)
                recovered = replace(
                    backup,
                    status="pending_health",
                    failure_code="state_file_recovered",
                    failure_summary=summary,
                    failure_digest=digest,
                    failed_at=_utc_now(),
                ).validate()
            elif backup.status == "rolling_back":
                recovered = self._recover_rolling_back(
                    backup,
                    "state_file_recovered",
                    primary_error,
                )
            elif backup.status == "rolled_back":
                recovered = backup
            else:
                recovered = self._failure_state(
                    backup,
                    code="state_file_recovered",
                    reason=primary_error,
                )
            return self._save(recovered, backup_current=False)

    def _recover_rolling_back(
        self,
        state: UpdateState,
        code: str,
        reason: object,
    ) -> UpdateState:
        rollback_version = state.rollback_version or state.previous_version
        try:
            _safe_release_executable(
                self.install_root / "versions",
                rollback_version,
            )
        except UpdateError as exc:
            return self._failure_state(
                state,
                code="rollback_unavailable",
                reason=exc,
                status="rollback_failed",
            )
        summary, digest = _safe_failure_details(reason)
        previous_version = _find_previous_safe_release(
            self.install_root / "versions",
            rollback_version,
            excluded={state.pending_version, state.target_version},
        )
        return replace(
            state,
            active_version=rollback_version,
            previous_version=previous_version,
            pending_version="",
            rollback_version="",
            failed_version=state.pending_version or state.target_version,
            status="rolled_back",
            rolled_back_at=_utc_now(),
            failed_at=_utc_now(),
            failure_code=code,
            failure_summary=summary,
            failure_digest=digest,
        ).validate()

    def _infer_single_installed_release(self) -> UpdateState:
        versions_dir = self.install_root / "versions"
        if _is_link_or_reparse(versions_dir) or not versions_dir.is_dir():
            raise UpdateSecurityError(
                "Update state cannot be reconstructed without a safe versions directory"
            )
        versions: list[str] = []
        for candidate in versions_dir.iterdir():
            try:
                parse_version(candidate.name)
            except UpdateSecurityError:
                continue
            _safe_release_executable(versions_dir, candidate.name)
            versions.append(candidate.name)
        if len(versions) != 1:
            raise UpdateSecurityError(
                "Update state cannot be reconstructed when multiple releases are installed"
            )
        base = UpdateState(active_version=versions[0])
        return self._failure_state(
            base,
            code="state_reconstructed",
            reason="update state was reconstructed from the only installed release",
        )

    def _cleanup_orphaned_staging(self) -> None:
        versions_dir = self.install_root / "versions"
        if not versions_dir.exists() or _is_link_or_reparse(versions_dir):
            return
        for candidate in versions_dir.iterdir():
            if not _STAGING_PATTERN.fullmatch(candidate.name):
                continue
            if _is_link_or_reparse(candidate):
                continue
            try:
                _assert_removal_tree_is_safe(candidate, versions_dir)
            except UpdateSecurityError:
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate)

    def recover_interrupted(self, state: UpdateState | None = None) -> UpdateState:
        if state is None:
            try:
                state = self.load()
            except UpdateSecurityError:
                state = self._infer_single_installed_release()
                return self._save(state, backup_current=False)
        if state.status in {
            "healthy",
            "pending_health",
            "rolled_back",
            "failed",
            "rollback_failed",
        }:
            return state
        if state.status == "confirming":
            summary, digest = _safe_failure_details(
                "health confirmation was interrupted and must be observed again"
            )
            recovered = replace(
                state,
                status="pending_health",
                failure_code="confirmation_interrupted",
                failure_summary=summary,
                failure_digest=digest,
                failed_at=_utc_now(),
            ).validate()
        elif state.status == "rolling_back":
            recovered = self._recover_rolling_back(
                state,
                "rollback_interrupted",
                "rollback was completed after an interrupted launcher transition",
            )
        else:
            recovered = self._failure_state(
                state,
                code=f"interrupted_{state.status}",
                reason=f"update process stopped while state was {state.status}",
            )
        saved = self.save(recovered)
        self._cleanup_orphaned_staging()
        return saved

    def active_executable(self) -> Path:
        state = self.load()
        return _safe_release_executable(
            self.install_root / "versions",
            state.active_version,
        )


class AgentUpdater:
    def __init__(self, install_root: str | os.PathLike[str], current_version: str) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.current_version = current_version
        parse_version(current_version)
        self.versions_dir = self.install_root / "versions"
        self.download_dir = self.install_root / "updates"
        self.state_store = UpdateStateStore(self.install_root)

    def _state_for_running_agent(self, *, recover: bool = True) -> UpdateState:
        if recover:
            state = self.state_store.initialize(self.current_version)
        elif self.state_store.path.exists() or self.state_store.backup_path.exists():
            state = self.state_store.load()
        else:
            state = self.state_store.initialize(self.current_version)
        if state.active_version != self.current_version:
            raise UpdateError("Running Agent version does not match the atomic update state")
        if state.status in {"pending_health", "confirming", "rolling_back", "rollback_failed"}:
            raise UpdateError("A pending Agent transition must be completed by the launcher")
        return state

    def begin_check(self) -> UpdateState:
        state = self._state_for_running_agent()
        now = _utc_now()
        return self.state_store.save(
            replace(
                state,
                status="checking",
                attempt_id=uuid.uuid4().hex,
                target_version="",
                pending_version="",
                rollback_version="",
                checked_at=now,
            )
        )

    def finish_check_without_update(self) -> UpdateState:
        state = self._state_for_running_agent(recover=False)
        if state.status != "checking":
            raise UpdateError("No active update check can be completed")
        healthy = self.state_store.save(
            replace(
                state,
                status="healthy",
                attempt_id="",
                target_version="",
                pending_version="",
                rollback_version="",
            )
        )
        if healthy.confirmed_at:
            try:
                self.cleanup_old_versions()
            except Exception as exc:
                return self.record_failure(
                    "cleanup_failed",
                    exc,
                    target_version=healthy.active_version,
                )
            return self.state_store.load()
        return healthy

    def begin_download(self, target_version: str) -> UpdateState:
        parse_version(target_version)
        state = self.state_store.load()
        if state.status != "checking":
            raise UpdateError("Update download cannot start from the current state")
        return self.state_store.save(
            replace(
                state,
                status="downloading",
                target_version=target_version,
            )
        )

    def mark_downloaded(self, target_version: str) -> UpdateState:
        state = self.state_store.load()
        if state.status != "downloading" or state.target_version != target_version:
            raise UpdateError("Downloaded update does not match the active update attempt")
        return self.state_store.save(
            replace(
                state,
                status="downloaded",
                downloaded_at=_utc_now(),
            )
        )

    def record_failure(
        self,
        code: str,
        reason: object,
        *,
        target_version: str = "",
    ) -> UpdateState:
        if not _FAILURE_CODE_PATTERN.fullmatch(code):
            raise ValueError("failure code must be a stable lowercase identifier")
        state = self.state_store.load()
        if target_version:
            parse_version(target_version)
            state = replace(state, target_version=target_version)
        failed = self.state_store._failure_state(
            state,
            code=code,
            reason=reason,
        )
        return self.state_store.save(failed)

    def _protected_versions(self, state: UpdateState) -> set[str]:
        return {
            value
            for value in (
                state.active_version,
                state.previous_version,
                state.pending_version,
                state.rollback_version,
                state.target_version
                if state.status
                in {
                    "downloading",
                    "downloaded",
                    "staging",
                    "switching",
                    "pending_health",
                    "confirming",
                    "rolling_back",
                    "rollback_failed",
                }
                else "",
            )
            if value
        }

    def _remove_release(self, version: str, state: UpdateState) -> bool:
        if version in self._protected_versions(state):
            return False
        candidate = _safe_release_directory(
            self.versions_dir,
            version,
            require_exists=False,
        )
        if not candidate.exists():
            return False
        _assert_removal_tree_is_safe(candidate, self.versions_dir)
        shutil.rmtree(candidate)
        return True

    def cleanup_old_versions(self) -> tuple[str, ...]:
        state = self.state_store.load()
        if state.status != "healthy" or not state.confirmed_at:
            raise UpdateError("Old releases may only be cleaned after health confirmation")
        if not self.versions_dir.exists():
            return ()
        _safe_release_directory(
            self.versions_dir,
            state.active_version,
            require_exists=True,
        )
        removed: list[str] = []
        for candidate in sorted(self.versions_dir.iterdir(), key=lambda item: item.name):
            try:
                candidate_version = parse_version(candidate.name)
            except UpdateSecurityError:
                continue
            if candidate_version >= parse_version(state.active_version):
                continue
            if self._remove_release(candidate.name, state):
                removed.append(candidate.name)
        current = self.state_store.load()
        if current != state:
            raise UpdateError("Update state changed while old releases were being cleaned")
        self.state_store.save(replace(state, cleanup_at=_utc_now()))
        return tuple(removed)

    def apply(self, manifest: ReleaseManifest, archive: Path) -> UpdateState:
        previous = self._state_for_running_agent(recover=False)
        if previous.status not in {"healthy", "failed", "rolled_back", "downloaded"}:
            raise UpdateError("Update package cannot be staged from the current state")
        try:
            if parse_version(manifest.version) <= parse_version(self.current_version):
                raise UpdateNotNewerError("Refusing to install an older or equal Agent version")
            if archive.stat().st_size != manifest.size or sha256_file(archive) != manifest.sha256:
                raise UpdateSecurityError(
                    "Staged update package failed its signed size or SHA-256 check"
                )
        except Exception as exc:
            self.record_failure(
                "package_validation_failed",
                exc,
                target_version=manifest.version,
            )
            raise

        self.versions_dir.mkdir(parents=True, exist_ok=True)
        target = _safe_release_directory(
            self.versions_dir,
            manifest.version,
            require_exists=False,
        )
        if target.exists():
            can_replace_failed_target = (
                previous.failed_version == manifest.version
                or (
                    previous.status == "failed"
                    and previous.target_version == manifest.version
                )
            )
            if not can_replace_failed_target or not self._remove_release(
                manifest.version,
                previous,
            ):
                error = UpdateSecurityError("Target update version directory already exists")
                self.record_failure(
                    "target_exists",
                    error,
                    target_version=manifest.version,
                )
                raise error

        attempt_id = previous.attempt_id or uuid.uuid4().hex
        staging = self.versions_dir / f".{manifest.version}-{uuid.uuid4().hex}.staging"
        staging_state = self.state_store.save(
            replace(
                previous,
                status="staging",
                attempt_id=attempt_id,
                target_version=manifest.version,
                pending_version="",
                rollback_version="",
                downloaded_at=previous.downloaded_at or _utc_now(),
                failure_code="",
                failure_summary="",
                failure_digest="",
                failed_at="",
                failed_version="",
            )
        )
        target_installed = False
        try:
            extract_release_archive(archive, staging)
            switching = self.state_store.save(
                replace(
                    staging_state,
                    status="switching",
                    pending_version=manifest.version,
                    rollback_version=previous.active_version,
                    staged_at=_utc_now(),
                )
            )
            os.replace(staging, target)
            target_installed = True
            now = _utc_now()
            return self.state_store.save(
                replace(
                    switching,
                    active_version=manifest.version,
                    previous_version=previous.active_version,
                    pending_version=manifest.version,
                    rollback_version=previous.active_version,
                    status="pending_health",
                    switched_at=now,
                    pending_since=now,
                )
            )
        except Exception as exc:
            if staging.exists() and not _is_link_or_reparse(staging):
                shutil.rmtree(staging, ignore_errors=True)
            try:
                failed = self.record_failure(
                    "install_failed",
                    exc,
                    target_version=manifest.version,
                )
            except Exception:
                raise
            if target_installed:
                self._remove_release(manifest.version, failed)
            raise

    def confirm_health(self, running_version: str) -> UpdateState:
        state = self.state_store.recover_interrupted()
        if state.status != "pending_health" or state.pending_version != running_version:
            raise UpdateError("No matching pending Agent update can be confirmed")
        confirming = self.state_store.save(
            replace(state, status="confirming")
        )
        healthy = self.state_store.save(
            replace(
                confirming,
                active_version=running_version,
                previous_version=confirming.rollback_version,
                pending_version="",
                rollback_version="",
                status="healthy",
                confirmed_at=_utc_now(),
                attempt_id="",
                failure_code="",
                failure_summary="",
                failure_digest="",
                failed_at="",
                failed_version="",
            )
        )
        try:
            self.cleanup_old_versions()
        except Exception as exc:
            return self.record_failure(
                "cleanup_failed",
                exc,
                target_version=running_version,
            )
        return self.state_store.load()

    def rollback(self, reason: str) -> UpdateState:
        state = self.state_store.load()
        if state.status != "pending_health":
            raise UpdateError("No pending Agent update can be rolled back")
        rollback_version = state.rollback_version or state.previous_version
        try:
            _safe_release_executable(self.versions_dir, rollback_version)
        except UpdateError as exc:
            failed = self.state_store._failure_state(
                state,
                code="rollback_unavailable",
                reason=exc,
                status="rollback_failed",
            )
            self.state_store.save(failed)
            raise UpdateError(
                "Previous Agent release is unavailable; refusing an unsafe rollback"
            ) from exc
        rolling_back = self.state_store.save(
            replace(state, status="rolling_back")
        )
        summary, digest = _safe_failure_details(reason)
        previous_version = _find_previous_safe_release(
            self.versions_dir,
            rollback_version,
            excluded={state.pending_version, state.target_version},
        )
        rolled_back = replace(
            rolling_back,
            active_version=rollback_version,
            previous_version=previous_version,
            pending_version="",
            rollback_version="",
            failed_version=state.pending_version,
            target_version=state.pending_version,
            status="rolled_back",
            attempt_id="",
            rolled_back_at=_utc_now(),
            failed_at=_utc_now(),
            failure_code="candidate_health_failed",
            failure_summary=summary,
            failure_digest=digest,
        )
        return self.state_store.save(rolled_back)

    def rollback_if_health_timed_out(
        self,
        *,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> UpdateState | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        state = self.state_store.load()
        if state.status != "pending_health":
            return None
        pending_since = datetime.fromisoformat(state.pending_since)
        observed = now or datetime.now(timezone.utc)
        if observed < pending_since + timedelta(seconds=timeout_seconds):
            return None
        return self.rollback("new Agent version did not confirm health before the deadline")


__all__ = [
    "AGENT_EXECUTABLE",
    "AgentUpdater",
    "ReleaseManifest",
    "UpdateError",
    "UpdateNotNewerError",
    "UpdateSecurityError",
    "UpdateState",
    "UpdateStateStore",
    "canonical_manifest_payload",
    "download_release",
    "extract_release_archive",
    "fetch_release_manifest",
    "parse_version",
    "sha256_file",
]
