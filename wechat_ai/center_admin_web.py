from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit


CENTER_ADMIN_PREFIX = "/admin"
_STATIC_ROOT = Path(__file__).resolve().parent / "static" / "center_admin"
_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def read_center_admin_asset(request_target: str) -> tuple[str, bytes]:
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
    return content_type, asset_path.read_bytes()


__all__ = ["CENTER_ADMIN_PREFIX", "read_center_admin_asset"]
