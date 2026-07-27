from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import platform
import time
from typing import Any


SCALAR_TYPES = (str, int, float, bool, type(None))
PATH_ATTR_HINTS = (
    "path",
    "filepath",
    "file_path",
    "file",
    "image_path",
    "img_path",
    "thumb",
    "thumbnail",
    "download",
    "save",
)
IMAGE_TYPE_HINTS = ("image", "img", "pic", "picture", "photo", "图片", "表情")


def run_image_probe(
    chat_name: str,
    seconds: int = 60,
    interval_seconds: float = 2.0,
    output_dir: str | Path = "logs",
) -> Path:
    wx = _create_wechat_client()
    wx.ChatWith(chat_name)
    deadline = time.time() + max(seconds, 1)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    print(f"Image probe is watching {chat_name!r} for {seconds}s.")
    print("Send one image to this chat now. Press Ctrl+C to stop early.")
    while time.time() < deadline:
        for msg in list(wx.GetAllMessage() or []):
            key = message_key(msg)
            if key in seen:
                continue
            seen.add(key)
            record = inspect_message(msg)
            if record["is_image_like"]:
                record["chat_name"] = chat_name
                record["message_key"] = key
                records.append(record)
                print(json.dumps(record, ensure_ascii=False, indent=2))
        time.sleep(max(interval_seconds, 0.2))

    output_path = _write_probe_result(chat_name, records, output_dir)
    print(f"Saved image probe result to {output_path}")
    if not records:
        print("No image-like message was detected. Try again after sending a fresh image while this probe is running.")
    return output_path


def inspect_message(msg: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    callables: list[str] = []
    errors: dict[str, str] = {}

    for name in sorted(set(dir(msg))):
        if name.startswith("__"):
            continue
        try:
            value = getattr(msg, name)
        except Exception as exc:
            errors[name] = str(exc)
            continue
        if callable(value):
            callables.append(name)
            continue
        if isinstance(value, SCALAR_TYPES):
            attrs[name] = value
        elif isinstance(value, (list, tuple)):
            attrs[name] = [str(item) for item in value[:10]]
        elif isinstance(value, dict):
            attrs[name] = {str(key): _safe_value(val) for key, val in list(value.items())[:30]}
        else:
            attrs[name] = repr(value)

    candidates = _candidate_paths(attrs)
    msg_type = str(attrs.get("type", "") or attrs.get("msg_type", ""))
    content = str(attrs.get("content", "") or "")
    class_name = type(msg).__name__
    is_image_like = _is_image_like(class_name, msg_type, content, attrs, callables)

    return {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "class": class_name,
        "module": type(msg).__module__,
        "is_image_like": is_image_like,
        "type": msg_type,
        "sender": str(attrs.get("sender", "")),
        "content": content,
        "attrs": attrs,
        "callables": callables,
        "candidate_paths": candidates,
        "attr_errors": errors,
    }


def message_key(msg: Any) -> str:
    msg_id = getattr(msg, "id", None)
    if msg_id:
        return f"id:{msg_id}"
    return "|".join(
        [
            type(msg).__name__,
            str(getattr(msg, "attr", "")),
            str(getattr(msg, "sender", "")),
            str(getattr(msg, "type", "")),
            str(getattr(msg, "content", "")),
            str(getattr(msg, "time", "")),
        ]
    )


def _create_wechat_client() -> Any:
    if platform.system() != "Windows":
        raise RuntimeError("Image probe requires Windows desktop WeChat/Weixin with wxauto4.")
    try:
        from wxauto4 import WeChat
    except ImportError as exc:
        raise RuntimeError("wxauto4 is not installed. Run `pip install -r requirements.txt`.") from exc
    try:
        wx = WeChat(ads=False, resize=False)
    except Exception as exc:
        raise RuntimeError(f"Failed to attach to WeChat/Weixin with wxauto4: {exc}") from exc
    if not all(callable(getattr(wx, name, None)) for name in ("ChatWith", "GetAllMessage")):
        raise RuntimeError("wxauto4 client does not expose ChatWith/GetAllMessage.")
    return wx


def _write_probe_result(chat_name: str, records: list[dict[str, Any]], output_dir: str | Path) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe_chat = "".join(ch if ch.isalnum() else "_" for ch in chat_name).strip("_") or "chat"
    path = directory / f"image-probe-{safe_chat}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "chat_name": chat_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _safe_value(value: Any) -> Any:
    if isinstance(value, SCALAR_TYPES):
        return value
    return repr(value)


def _candidate_paths(attrs: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key, value in attrs.items():
        key_l = key.lower()
        if not any(hint in key_l for hint in PATH_ATTR_HINTS):
            continue
        if isinstance(value, str) and value:
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(str(item) for item in value if item)
    return candidates


def _is_image_like(
    class_name: str,
    msg_type: str,
    content: str,
    attrs: dict[str, Any],
    callables: list[str],
) -> bool:
    haystack = " ".join(
        [
            class_name,
            msg_type,
            content,
            " ".join(attrs.keys()),
            " ".join(callables),
        ]
    ).casefold()
    return any(hint.casefold() in haystack for hint in IMAGE_TYPE_HINTS)
