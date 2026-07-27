from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from typing import Protocol
import uuid


PROFILE_SCHEMA_VERSION = 1
APP_DIRECTORY_PARTS = ("DeepAwake", "WeChatAgent")
MAX_PROFILE_BYTES = 64 * 1024


class TokenCipher(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class WindowsDPAPICipher:
    """Protect Agent credentials for the current interactive Windows user."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, *, platform_name: str | None = None) -> None:
        self._platform_name = platform_name or sys.platform
        if self._platform_name != "win32":
            raise RuntimeError(
                "Windows DPAPI is required for Agent credentials; "
                "tests on other platforms must inject a TokenCipher."
            )

    @staticmethod
    def _libraries() -> tuple[object, object]:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("Windows DPAPI is unavailable.")
        crypt32 = loader("crypt32", use_last_error=True)
        kernel32 = loader("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return crypt32, kernel32

    @staticmethod
    def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buffer

    def encrypt(self, plaintext: bytes) -> bytes:
        if not plaintext:
            raise ValueError("Agent token must not be empty.")
        crypt32, kernel32 = self._libraries()
        source, source_buffer = self._input_blob(plaintext)
        protected = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(source),
            "DeepAwake WeChat Agent",
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(protected),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        del source_buffer
        try:
            return ctypes.string_at(protected.pbData, protected.cbData)
        finally:
            kernel32.LocalFree(protected.pbData)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if not ciphertext:
            raise ValueError("Protected Agent token must not be empty.")
        crypt32, kernel32 = self._libraries()
        source, source_buffer = self._input_blob(ciphertext)
        plaintext = _DataBlob()
        description = wintypes.LPWSTR()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(plaintext),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        del source_buffer
        try:
            return ctypes.string_at(plaintext.pbData, plaintext.cbData)
        finally:
            if description:
                kernel32.LocalFree(description)
            kernel32.LocalFree(plaintext.pbData)


def default_agent_data_dir(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is required for the Windows Agent profile.")
    return Path(local_app_data).joinpath(*APP_DIRECTORY_PARTS)


def _required_text(value: object, field_name: str, *, max_length: int = 2048) -> str:
    if type(value) is not str:
        raise RuntimeError(f"Agent profile field {field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise RuntimeError(f"Agent profile field {field_name} is invalid.")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise RuntimeError(f"Agent profile field {field_name} is invalid.")
    return normalized


def _read_json(path: Path) -> dict[str, object]:
    try:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise RuntimeError(f"Agent profile file is too large: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read Agent profile file: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Agent profile file must contain a JSON object: {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@dataclass(frozen=True)
class AgentProfile:
    center_url: str
    tenant_id: str
    agent_instance_id: str
    channel_account_id: str
    agent_token: str
    device_id: str
    device_name: str
    agent_version: str
    activated_at: str


@dataclass(frozen=True)
class AgentDataPaths:
    root: Path

    @property
    def profile(self) -> Path:
        return self.root / "profile.json"

    @property
    def device(self) -> Path:
        return self.root / "device.json"

    @property
    def runtime_marker(self) -> Path:
        return self.root / "agent.runtime"

    @property
    def spool(self) -> Path:
        return self.root / "data" / "agent-spool.sqlite3"

    @property
    def state_database(self) -> Path:
        return self.root / "data" / "wechat-agent.sqlite3"

    @property
    def log(self) -> Path:
        return self.root / "logs" / "agent.log"

    @property
    def audit_log(self) -> Path:
        return self.root / "logs" / "audit.jsonl"

    @property
    def alert_log(self) -> Path:
        return self.root / "logs" / "alerts.log"

    @property
    def health(self) -> Path:
        return self.root / "logs" / "health.json"

    @property
    def pending_replies(self) -> Path:
        return self.root / "logs" / "pending-replies.jsonl"

    @property
    def reports(self) -> Path:
        return self.root / "reports"


class AgentProfileStore:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        cipher: TokenCipher | None = None,
    ) -> None:
        self.paths = AgentDataPaths(
            Path(root).expanduser().resolve() if root is not None else default_agent_data_dir().resolve()
        )
        self._cipher = cipher

    def _token_cipher(self) -> TokenCipher:
        if self._cipher is None:
            self._cipher = WindowsDPAPICipher()
        return self._cipher

    def is_activated(self) -> bool:
        return self.paths.profile.is_file()

    def get_or_create_device_id(self) -> str:
        if self.paths.device.exists():
            payload = _read_json(self.paths.device)
            if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
                raise RuntimeError("Agent device identity schema is unsupported.")
            return _required_text(payload.get("device_id"), "device_id", max_length=128)
        device_id = str(uuid.uuid4())
        _atomic_write_json(
            self.paths.device,
            {"schema_version": PROFILE_SCHEMA_VERSION, "device_id": device_id},
        )
        return device_id

    def device_name(self) -> str:
        return _required_text(socket.gethostname(), "device_name", max_length=255)

    def save(self, profile: AgentProfile) -> None:
        if profile.device_id != self.get_or_create_device_id():
            raise RuntimeError("Activation response does not match this device.")
        protected_token = self._token_cipher().encrypt(profile.agent_token.encode("utf-8"))
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "center_url": _required_text(profile.center_url, "center_url"),
            "tenant_id": _required_text(profile.tenant_id, "tenant_id", max_length=256),
            "agent_instance_id": _required_text(
                profile.agent_instance_id, "agent_instance_id", max_length=256
            ),
            "channel_account_id": _required_text(
                profile.channel_account_id, "channel_account_id", max_length=256
            ),
            "agent_token_protected": base64.b64encode(protected_token).decode("ascii"),
            "device_id": _required_text(profile.device_id, "device_id", max_length=128),
            "device_name": _required_text(profile.device_name, "device_name", max_length=255),
            "agent_version": _required_text(profile.agent_version, "agent_version", max_length=64),
            "activated_at": _required_text(profile.activated_at, "activated_at", max_length=64),
        }
        _atomic_write_json(self.paths.profile, payload)
        self.ensure_runtime_marker()

    def load(self) -> AgentProfile:
        if not self.paths.profile.is_file():
            raise RuntimeError("Agent is not activated. Run `agent activate` first.")
        payload = _read_json(self.paths.profile)
        if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise RuntimeError("Agent profile schema is unsupported.")
        encoded_token = _required_text(
            payload.get("agent_token_protected"),
            "agent_token_protected",
            max_length=MAX_PROFILE_BYTES,
        )
        try:
            protected_token = base64.b64decode(encoded_token, validate=True)
            token = self._token_cipher().decrypt(protected_token).decode("utf-8")
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            raise RuntimeError("Cannot decrypt the Agent credential.") from exc
        token = _required_text(token, "agent_token", max_length=4096)
        device_id = _required_text(payload.get("device_id"), "device_id", max_length=128)
        if device_id != self.get_or_create_device_id():
            raise RuntimeError("Agent profile belongs to a different device.")
        return AgentProfile(
            center_url=_required_text(payload.get("center_url"), "center_url"),
            tenant_id=_required_text(payload.get("tenant_id"), "tenant_id", max_length=256),
            agent_instance_id=_required_text(
                payload.get("agent_instance_id"), "agent_instance_id", max_length=256
            ),
            channel_account_id=_required_text(
                payload.get("channel_account_id"), "channel_account_id", max_length=256
            ),
            agent_token=token,
            device_id=device_id,
            device_name=_required_text(payload.get("device_name"), "device_name", max_length=255),
            agent_version=_required_text(payload.get("agent_version"), "agent_version", max_length=64),
            activated_at=_required_text(payload.get("activated_at"), "activated_at", max_length=64),
        )

    def config_overrides(self, profile: AgentProfile) -> dict[str, object]:
        return {
            "account_id": profile.channel_account_id,
            "agent_center_url": profile.center_url,
            "agent_tenant_id": profile.tenant_id,
            "agent_instance_id": profile.agent_instance_id,
            "agent_channel_account_id": profile.channel_account_id,
            "agent_token": profile.agent_token,
            "agent_spool_file": str(self.paths.spool),
            "state_db_file": str(self.paths.state_database),
            "log_file": str(self.paths.log),
            "audit_log_file": str(self.paths.audit_log),
            "alert_file": str(self.paths.alert_log),
            "health_file": str(self.paths.health),
            "pending_replies_file": str(self.paths.pending_replies),
            "reports_dir": str(self.paths.reports),
        }

    def ensure_runtime_marker(self) -> Path:
        if not self.paths.runtime_marker.is_file():
            _atomic_write_json(
                self.paths.runtime_marker,
                {"secure_profile": self.paths.profile.name},
            )
        return self.paths.runtime_marker

    def reset_binding(self) -> bool:
        removed = False
        for path in (self.paths.profile, self.paths.runtime_marker):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


def mask_identifier(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def public_profile_status(store: AgentProfileStore) -> dict[str, object]:
    if not store.is_activated():
        return {"activated": False, "profile_path": str(store.paths.profile)}
    profile = store.load()
    return {
        "activated": True,
        "center_url": profile.center_url,
        "tenant_id": mask_identifier(profile.tenant_id),
        "agent_instance_id": mask_identifier(profile.agent_instance_id),
        "channel_account_id": mask_identifier(profile.channel_account_id),
        "device_id": mask_identifier(profile.device_id),
        "device_name": profile.device_name,
        "agent_version": profile.agent_version,
        "activated_at": profile.activated_at,
        "agent_token": "protected",
        "profile_path": str(store.paths.profile),
    }
