from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


CENTER_ADMIN_PREFIX = "/admin"
_STATIC_ROOT = Path(__file__).resolve().parent / "static" / "center_admin"
_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_SAFE_PUBLIC_PREFIX = re.compile(r"(?:/[A-Za-z0-9._~-]+)*")


def read_center_admin_asset(
    request_target: str,
    *,
    public_prefix: str = "",
) -> tuple[str, bytes]:
    """Return a whitelisted admin asset as ``(content_type, body)``.

    ``request_target`` may be a full request target such as ``/admin/app.css?v=1``
    or a bare asset name. Only the three bundled files are addressable.
    """

    if not isinstance(request_target, str):
        raise TypeError("request_target must be a string")

    path = unquote(urlsplit(request_target).path)
    if "\x00" in path or "\\" in path:
        raise FileNotFoundError("admin asset not found")

    if path in {"", "/", CENTER_ADMIN_PREFIX, f"{CENTER_ADMIN_PREFIX}/"}:
        asset_name = "index.html"
    else:
        normalized = path.lstrip("/")
        prefix = CENTER_ADMIN_PREFIX.lstrip("/") + "/"
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise FileNotFoundError("admin asset not found")
        if len(parts) != 1:
            raise FileNotFoundError("admin asset not found")
        asset_name = parts[0]

    content_type = _ASSETS.get(asset_name)
    if content_type is None:
        raise FileNotFoundError("admin asset not found")

    asset_path = (_STATIC_ROOT / asset_name).resolve()
    if asset_path.parent != _STATIC_ROOT.resolve():
        raise FileNotFoundError("admin asset not found")
    body = asset_path.read_bytes()
    if asset_name == "index.html":
        prefix = _normalized_public_prefix(public_prefix)
        admin_base = f"{prefix}{CENTER_ADMIN_PREFIX}"
        body = body.replace(
            b'href="/admin/app.css"',
            f'href="{admin_base}/app.css"'.encode("utf-8"),
        ).replace(
            b'src="/admin/app.js"',
            f'src="{admin_base}/app.js"'.encode("utf-8"),
        )
    return content_type, body


def _normalized_public_prefix(value: str) -> str:
    prefix = str(value or "").strip().rstrip("/")
    if not prefix:
        return ""
    if not _SAFE_PUBLIC_PREFIX.fullmatch(prefix):
        return ""
    if any(part in {".", ".."} for part in prefix.split("/")):
        return ""
    return prefix


__all__ = ["CENTER_ADMIN_PREFIX", "read_center_admin_asset"]
