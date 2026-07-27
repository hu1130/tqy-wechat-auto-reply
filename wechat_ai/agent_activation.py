from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wechat_ai.agent_profile import AgentProfile, AgentProfileStore


BUILTIN_AGENT_CENTER_URL = ""
AGENT_VERSION = "1.0.0"
MAX_ACTIVATION_RESPONSE_BYTES = 64 * 1024
MAX_BUILD_CONFIG_BYTES = 16 * 1024
BUILD_CONFIG_NAME = "agent-build.json"


class ActivationError(RuntimeError):
    pass


class HTTPResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class HTTPOpener(Protocol):
    def open(self, request: Request, timeout: float) -> HTTPResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def resolve_center_url(explicit_url: str | None, environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    candidate = (
        (explicit_url or "").strip()
        or values.get("WECHAT_AGENT_CENTER_URL", "").strip()
        or _bundled_center_url()
        or BUILTIN_AGENT_CENTER_URL.strip()
    )
    if not candidate:
        raise ActivationError(
            "Agent center URL is not configured. Use --center-url or configure the installer build."
        )
    return validate_center_url(candidate)


def _bundle_root() -> Path | None:
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        return Path(pyinstaller_root)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _bundled_center_url() -> str:
    root = _bundle_root()
    if root is None:
        return ""
    path = root / BUILD_CONFIG_NAME
    if not path.is_file():
        return ""
    try:
        if path.stat().st_size > MAX_BUILD_CONFIG_BYTES:
            raise ActivationError("Bundled Agent center configuration is invalid.")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except ActivationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ActivationError("Bundled Agent center configuration is invalid.") from None
    if not isinstance(payload, dict):
        raise ActivationError("Bundled Agent center configuration is invalid.")
    center_url = payload.get("center_url")
    if type(center_url) is not str or not center_url.strip():
        raise ActivationError("Bundled Agent center configuration is invalid.")
    return center_url.strip()


def validate_center_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ActivationError("Agent center URL must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ActivationError("Agent center URL must not contain credentials, a query, or a fragment.")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ActivationError("HTTPS is required unless the Agent center is localhost.")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _is_loopback_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _required_request_text(value: str, field_name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ActivationError(f"{field_name} is invalid.")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ActivationError(f"{field_name} is invalid.")
    return normalized


def _response_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if type(value) is not str or not value.strip() or len(value) > 4096:
        raise ActivationError("Agent center returned an invalid activation response.")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ActivationError("Agent center returned an invalid activation response.")
    return value.strip()


@dataclass(frozen=True)
class ActivationResult:
    tenant_id: str
    agent_instance_id: str
    channel_account_id: str
    agent_token: str


class AgentActivationClient:
    def __init__(
        self,
        center_url: str,
        *,
        timeout_seconds: float = 20.0,
        opener: HTTPOpener | None = None,
    ) -> None:
        self.center_url = validate_center_url(center_url)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(_NoRedirectHandler())

    def activate(
        self,
        *,
        activation_code: str,
        device_id: str,
        device_name: str,
        agent_version: str,
    ) -> ActivationResult:
        payload = {
            "activation_code": _required_request_text(activation_code, "Activation code", 512),
            "device_id": _required_request_text(device_id, "Device ID", 128),
            "device_name": _required_request_text(device_name, "Device name", 255),
            "agent_version": _required_request_text(agent_version, "Agent version", 64),
        }
        request = Request(
            f"{self.center_url}/v1/agent/activate",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"DeepAwake-WeChat-Agent/{payload['agent_version']}",
            },
            method="POST",
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            raise ActivationError(f"Agent activation was rejected (HTTP {exc.code}).") from None
        except (URLError, OSError, TimeoutError):
            raise ActivationError("Cannot connect to the Agent center.") from None
        try:
            if response.status < 200 or response.status >= 300:
                raise ActivationError(f"Agent activation was rejected (HTTP {response.status}).")
            try:
                raw = response.read(MAX_ACTIVATION_RESPONSE_BYTES + 1)
            except (URLError, OSError, TimeoutError):
                raise ActivationError("Cannot read the Agent center response.") from None
            if len(raw) > MAX_ACTIVATION_RESPONSE_BYTES:
                raise ActivationError("Agent center returned an invalid activation response.")
        finally:
            response.close()
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ActivationError("Agent center returned an invalid activation response.") from None
        if not isinstance(decoded, dict):
            raise ActivationError("Agent center returned an invalid activation response.")
        return ActivationResult(
            tenant_id=_response_text(decoded, "tenant_id"),
            agent_instance_id=_response_text(decoded, "agent_instance_id"),
            channel_account_id=_response_text(decoded, "channel_account_id"),
            agent_token=_response_text(decoded, "agent_token"),
        )


def activate_and_save(
    *,
    activation_code: str,
    center_url: str,
    store: AgentProfileStore,
    client: AgentActivationClient | None = None,
    agent_version: str = AGENT_VERSION,
) -> AgentProfile:
    try:
        device_id = store.get_or_create_device_id()
        device_name = store.device_name()
    except (OSError, RuntimeError, ValueError):
        raise ActivationError("Cannot prepare the local Agent identity.") from None
    activation_client = client or AgentActivationClient(center_url)
    result = activation_client.activate(
        activation_code=activation_code,
        device_id=device_id,
        device_name=device_name,
        agent_version=agent_version,
    )
    profile = AgentProfile(
        center_url=validate_center_url(center_url),
        tenant_id=result.tenant_id,
        agent_instance_id=result.agent_instance_id,
        channel_account_id=result.channel_account_id,
        agent_token=result.agent_token,
        device_id=device_id,
        device_name=device_name,
        agent_version=agent_version,
        activated_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        store.save(profile)
    except (OSError, RuntimeError, ValueError):
        raise ActivationError(
            "Agent activation succeeded, but the secure profile could not be stored."
        ) from None
    return profile
