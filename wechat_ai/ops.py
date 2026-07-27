from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import re


PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{8,15}(\d{4})(?!\d)")
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
CODE_RE = re.compile(r"((?:验证码|校验码|动态码|code|CODE)[^\d]{0,10})(\d{4,8})")


def redact_sensitive_text(text: str) -> str:
    redacted = PHONE_RE.sub(r"\1****\2", text)
    redacted = ID_CARD_RE.sub(r"\1********\2", redacted)
    redacted = BANK_CARD_RE.sub(r"\1********\2", redacted)
    redacted = EMAIL_RE.sub(r"\1***\2", redacted)
    redacted = CODE_RE.sub(lambda match: f"{match.group(1)}{'*' * len(match.group(2))}", redacted)
    return redacted


def append_rotating_line(
    path: str | Path,
    line: str,
    max_bytes: int = 0,
    backup_count: int = 3,
) -> None:
    file_path = Path(path)
    if not str(file_path):
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_file(file_path, max_bytes=max_bytes, backup_count=backup_count)
    with file_path.open("a", encoding="utf-8") as file:
        file.write(f"{line}\n")


def rotate_file(path: str | Path, max_bytes: int = 0, backup_count: int = 3) -> None:
    file_path = Path(path)
    if max_bytes <= 0 or backup_count <= 0 or not file_path.exists():
        return
    if file_path.stat().st_size < max_bytes:
        return

    oldest = file_path.with_name(f"{file_path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        src = file_path.with_name(f"{file_path.name}.{index}")
        dst = file_path.with_name(f"{file_path.name}.{index + 1}")
        if src.exists():
            src.replace(dst)
    file_path.replace(file_path.with_name(f"{file_path.name}.1"))


def append_alert(
    alert_file: str | Path,
    message: str,
    max_bytes: int = 0,
    backup_count: int = 3,
) -> None:
    if not str(alert_file):
        return
    now = datetime.now().isoformat(timespec="seconds")
    append_rotating_line(
        alert_file,
        f"[{now}] {redact_sensitive_text(message)}",
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def write_health_file(path: str | Path, payload: dict[str, object]) -> None:
    if not str(path):
        return
    health_path = Path(path)
    health_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"ts": datetime.now().isoformat(timespec="seconds"), **payload}
    health_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_report(reports_dir: str | Path, report_text: str, report_date: str) -> Path:
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report_date}.txt"
    path.write_text(report_text, encoding="utf-8")
    return path
