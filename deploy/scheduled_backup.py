from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


REQUIRED_CONFIG_KEYS = {
    "schema_version",
    "center_project_dir",
    "ai_cs_project_dir",
    "backup_root",
    "health_file",
    "bash_path",
    "retention_days",
    "keep_latest",
}
OPTIONAL_CONFIG_KEYS = {"failure_webhook_url", "max_backup_age_hours"}
BACKUP_NAME = re.compile(
    r"^coordinated-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one coordinated backup and persist scheduler health."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--check-health", action="store_true")
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("config path must be an absolute non-symlink file")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("config path must be a regular file")
    if os.name != "nt" and resolved.stat().st_mode & 0o077:
        raise ValueError("config file must not be accessible by group or other users")
    with resolved.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("backup config must be a JSON object")
    keys = set(value)
    missing = REQUIRED_CONFIG_KEYS - keys
    unknown = keys - REQUIRED_CONFIG_KEYS - OPTIONAL_CONFIG_KEYS
    if missing or unknown:
        raise ValueError(
            f"backup config keys are invalid; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    if value["schema_version"] != 1:
        raise ValueError("unsupported backup config schema_version")
    return value


def resolve_directory(value: Any, name: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise ValueError(f"{name} must be a non-root directory")
    return resolved


def resolve_file(value: Any, name: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    if path.is_symlink():
        raise ValueError(f"{name} must be a regular non-symlink file")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{name} must be a regular non-symlink file")
    return resolved


def bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def validate_webhook(value: Any) -> str:
    webhook = str(value or "").strip()
    if not webhook:
        return ""
    parsed = urlsplit(webhook)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "failure_webhook_url must be an HTTPS URL without credentials or fragment"
        )
    return webhook


def load_previous_health(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def discover_backup_sets(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in root.iterdir():
        if not BACKUP_NAME.fullmatch(path.name):
            continue
        if path.is_symlink():
            raise ValueError(f"backup set must not be a symbolic link: {path}")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_dir():
            raise ValueError(f"backup set must be a directory: {path}")
        result[path.name] = resolved
    return result


def verify_new_backup_set(path: Path) -> str:
    manifest_path = path / "manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "complete"
        or manifest.get("batch_id") != path.name.removeprefix("coordinated-")
    ):
        raise ValueError("new backup set has an invalid completion manifest")
    created_at = str(manifest.get("created_at", "")).strip()
    parse_utc(created_at, "backup manifest created_at")
    return created_at


def write_health(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("health_file must be an absolute non-symlink path")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("health_file parent must be a directory")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def send_failure_webhook(url: str, payload: dict[str, Any]) -> None:
    if not url:
        return
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if not 200 <= int(response.status) < 300:
                raise RuntimeError(f"HTTP {response.status}")
    except Exception as exc:
        print(
            f"ERROR: backup failed and the configured webhook was unavailable: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )


def parse_utc(value: Any, name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def check_health(config: dict[str, Any]) -> int:
    webhook = validate_webhook(config.get("failure_webhook_url", ""))
    max_age_hours = bounded_integer(
        config.get("max_backup_age_hours", 36),
        "max_backup_age_hours",
        1,
        24 * 30,
    )
    health_path = Path(str(config["health_file"]))
    if not health_path.is_absolute() or health_path.is_symlink():
        raise ValueError("health_file must be an absolute non-symlink path")
    health = load_previous_health(health_path)
    now = datetime.now(timezone.utc)
    status = str(health.get("status", "never_run"))
    reason = ""
    if status == "running":
        started_at = parse_utc(health.get("started_at"), "backup started_at")
        if now - started_at <= timedelta(hours=3):
            print(f"Backup is currently running; started at {started_at.isoformat()}.")
            return 0
        reason = "backup_run_stale"
    elif status != "success":
        reason = f"backup_status_{status}"
    else:
        last_success = parse_utc(
            health.get("last_success_at"), "backup last_success_at"
        )
        if now - last_success > timedelta(hours=max_age_hours):
            reason = "backup_success_stale"
        else:
            backup_set = Path(str(health.get("latest_backup_set", "")))
            if (
                not backup_set.is_absolute()
                or backup_set.is_symlink()
                or not backup_set.is_dir()
            ):
                reason = "latest_backup_set_missing"
            else:
                print(
                    f"Backup health is current: {backup_set.name}, "
                    f"last success {last_success.isoformat()}."
                )
                return 0

    payload = {
        "event": "wechat_platform_backup_unhealthy",
        "hostname": socket.gethostname(),
        "reason": reason,
        "status": status,
        "last_success_at": str(health.get("last_success_at", "")),
        "checked_at": utc_now(),
    }
    send_failure_webhook(webhook, payload)
    print(f"ERROR: backup health check failed: {reason}", file=sys.stderr)
    return 2


def run(config: dict[str, Any]) -> int:
    center_project = resolve_directory(
        config["center_project_dir"], "center_project_dir"
    )
    ai_cs_project = resolve_directory(config["ai_cs_project_dir"], "ai_cs_project_dir")
    backup_root = resolve_directory(config["backup_root"], "backup_root")
    bash_path = resolve_file(config["bash_path"], "bash_path")
    backup_script = resolve_file(
        center_project / "deploy" / "coordinated_backup.sh",
        "coordinated backup script",
    )
    health_path = Path(str(config["health_file"]))
    retention_days = bounded_integer(
        config["retention_days"], "retention_days", 1, 3650
    )
    keep_latest = bounded_integer(config["keep_latest"], "keep_latest", 2, 365)
    webhook = validate_webhook(config.get("failure_webhook_url", ""))
    before = discover_backup_sets(backup_root)

    previous = load_previous_health(health_path)
    started_at = utc_now()
    running_health = {
        "schema_version": 1,
        "status": "running",
        "hostname": socket.gethostname(),
        "started_at": started_at,
        "completed_at": "",
        "exit_code": None,
        "last_success_at": previous.get("last_success_at", ""),
        "last_failure_at": previous.get("last_failure_at", ""),
    }
    write_health(health_path, running_health)

    environment = os.environ.copy()
    environment.update(
        {
            "AI_CS_PROJECT_DIR": str(ai_cs_project),
            "COORDINATED_BACKUP_RETENTION_DAYS": str(retention_days),
            "COORDINATED_BACKUP_KEEP_LATEST": str(keep_latest),
        }
    )
    result = subprocess.run(
        [str(bash_path), str(backup_script), str(backup_root)],
        cwd=center_project,
        env=environment,
        check=False,
    )
    completed_at = utc_now()
    exit_code = int(result.returncode)
    latest_backup_set = ""
    latest_backup_created_at = ""
    if exit_code == 0:
        after = discover_backup_sets(backup_root)
        created = sorted(set(after) - set(before))
        if len(created) != 1:
            print(
                "ERROR: successful backup command must publish exactly one new "
                f"coordinated set; found {len(created)}",
                file=sys.stderr,
            )
            exit_code = 70
        else:
            latest_path = after[created[0]]
            latest_backup_created_at = verify_new_backup_set(latest_path)
            latest_backup_set = str(latest_path)
    succeeded = exit_code == 0
    final_health = {
        **running_health,
        "status": "success" if succeeded else "failed",
        "completed_at": completed_at,
        "exit_code": exit_code,
        "latest_backup_set": (
            latest_backup_set
            if succeeded
            else str(previous.get("latest_backup_set", ""))
        ),
        "latest_backup_created_at": (
            latest_backup_created_at
            if succeeded
            else str(previous.get("latest_backup_created_at", ""))
        ),
        "last_success_at": (
            completed_at if succeeded else running_health["last_success_at"]
        ),
        "last_failure_at": (
            running_health["last_failure_at"] if succeeded else completed_at
        ),
    }
    write_health(health_path, final_health)
    if not succeeded:
        send_failure_webhook(
            webhook,
            {
                "event": "wechat_platform_backup_failed",
                "hostname": final_health["hostname"],
                "started_at": started_at,
                "completed_at": completed_at,
                "exit_code": exit_code,
            },
        )
    return exit_code


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.check_health:
        return check_health(config)
    return run(config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
