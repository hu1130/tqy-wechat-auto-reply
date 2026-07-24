from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _private_key(path: Path) -> Ed25519PrivateKey:
    password_text = os.environ.get("AGENT_ED25519_PRIVATE_KEY_PASSWORD", "")
    password = password_text.encode("utf-8") if password_text else None
    value = serialization.load_pem_private_key(path.read_bytes(), password=password)
    if not isinstance(value, Ed25519PrivateKey):
        raise RuntimeError("Release signing key must be an Ed25519 private key")
    return value


def _public_key_base64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def export_public_key(private_key: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_public_key_base64(_private_key(private_key)) + "\n", encoding="ascii")


def create_manifest(
    private_key: Path,
    archive: Path,
    version: str,
    package_url: str,
    output: Path,
) -> None:
    parsed = urlsplit(package_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Release package URL must be an absolute HTTPS URL")
    content = archive.read_bytes()
    payload: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "package_url": package_url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["signature"] = base64.b64encode(_private_key(private_key).sign(canonical)).decode(
        "ascii"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    public = subparsers.add_parser("export-public")
    public.add_argument("--private-key", required=True, type=Path)
    public.add_argument("--output", required=True, type=Path)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--private-key", required=True, type=Path)
    manifest.add_argument("--archive", required=True, type=Path)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--package-url", required=True)
    manifest.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "export-public":
        export_public_key(args.private_key, args.output)
    else:
        create_manifest(
            args.private_key,
            args.archive,
            args.version,
            args.package_url,
            args.output,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
