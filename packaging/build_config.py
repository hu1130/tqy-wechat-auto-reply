from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from wechat_ai.agent_activation import validate_center_url
from wechat_ai.agent_updater import UpdateSecurityError, parse_version


BUILD_CONFIG_NAME = "agent-build.json"
MAX_BUILD_CONFIG_BYTES = 16 * 1024


@dataclass(frozen=True)
class AgentPackageConfig:
    version: str
    center_url: str
    update_manifest_url: str
    update_public_key: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AgentPackageConfig":
        version = str(value.get("version", "")).strip()
        parse_version(version)
        center_url = validate_center_url(str(value.get("center_url", "")))
        if urlsplit(center_url).scheme != "https":
            raise UpdateSecurityError("Packaged center_url must use HTTPS")
        manifest_url = str(value.get("update_manifest_url", "")).strip()
        parsed_manifest = urlsplit(manifest_url)
        if (
            parsed_manifest.scheme != "https"
            or not parsed_manifest.hostname
            or parsed_manifest.username
            or parsed_manifest.password
            or parsed_manifest.fragment
        ):
            raise UpdateSecurityError("update_manifest_url must use an absolute HTTPS URL")
        public_key = str(value.get("update_public_key", "")).strip()
        try:
            public_key_bytes = base64.b64decode(public_key, validate=True)
        except ValueError as exc:
            raise UpdateSecurityError("update_public_key must be valid base64") from exc
        if len(public_key_bytes) != 32:
            raise UpdateSecurityError("update_public_key must be a 32-byte Ed25519 public key")
        return cls(version, center_url, manifest_url, public_key)


def load_package_config() -> AgentPackageConfig:
    roots: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    executable_root = Path(sys.executable).resolve().parent
    roots.extend((executable_root, executable_root / "_internal"))

    missing_error: OSError | None = None
    value: object | None = None
    for root in dict.fromkeys(roots):
        path = root / BUILD_CONFIG_NAME
        try:
            if path.stat().st_size > MAX_BUILD_CONFIG_BYTES:
                raise RuntimeError("Packaged Agent build configuration is too large")
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            break
        except FileNotFoundError as exc:
            missing_error = exc
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot read packaged Agent build configuration at {path}: {exc}"
            ) from exc
    if value is None:
        searched = ", ".join(str(root / BUILD_CONFIG_NAME) for root in dict.fromkeys(roots))
        raise RuntimeError(
            f"Cannot read packaged Agent build configuration; searched: {searched}"
        ) from missing_error
    if not isinstance(value, dict):
        raise RuntimeError("Packaged Agent build configuration must be a JSON object")
    return AgentPackageConfig.from_dict(value)
