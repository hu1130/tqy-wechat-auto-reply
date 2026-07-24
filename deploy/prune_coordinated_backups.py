from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


BACKUP_NAME = re.compile(
    r"^coordinated-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-(?P<nonce>[0-9a-f]{12})$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prune old, verified coordinated backup sets."
    )
    parser.add_argument("backup_root", type=Path)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--keep-latest", type=int, default=7)
    parser.add_argument(
        "--verify-script",
        type=Path,
        default=Path(__file__).with_name("verify_backup_set.sh"),
    )
    parser.add_argument("--bash-bin", default="bash")
    parser.add_argument("--confirm-delete-old-backups", action="store_true")
    return parser.parse_args()


def load_created_at(path: Path) -> datetime:
    manifest_path = path / "manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        raise ValueError("manifest is not a completed schema version 1 backup")
    match = BACKUP_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError("backup directory name is invalid")
    expected_batch_id = path.name.removeprefix("coordinated-")
    if manifest.get("batch_id") != expected_batch_id:
        raise ValueError("manifest batch_id does not match the directory name")
    created_at = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        raise ValueError("manifest created_at must include a timezone")
    directory_time = datetime.strptime(
        match.group("timestamp"), "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=timezone.utc)
    if created_at.astimezone(timezone.utc).replace(microsecond=0) != directory_time:
        raise ValueError("manifest created_at does not match the directory name")
    return created_at.astimezone(timezone.utc)


def discover_backups(root: Path) -> list[tuple[datetime, Path]]:
    backups: list[tuple[datetime, Path]] = []
    for path in root.iterdir():
        if not BACKUP_NAME.fullmatch(path.name):
            continue
        if path.is_symlink():
            raise ValueError(f"backup set must not be a symbolic link: {path}")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_dir():
            raise ValueError(f"backup set is not a directory: {path}")
        backups.append((load_created_at(resolved), resolved))
    backups.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return backups


def verify_backup(bash_bin: str, verify_script: Path, backup: Path) -> None:
    result = subprocess.run(
        [bash_bin, str(verify_script), str(backup)],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"backup verification failed; refusing to prune: {backup}")


def main() -> int:
    args = parse_args()
    if args.retention_days < 1:
        raise SystemExit("--retention-days must be at least 1")
    if args.keep_latest < 2:
        raise SystemExit("--keep-latest must be at least 2")
    if not args.backup_root.is_absolute():
        raise SystemExit("backup root must be absolute")
    if args.backup_root.is_symlink():
        raise SystemExit("backup root must not be a symbolic link")

    root = args.backup_root.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise SystemExit("backup root must be a non-root directory")
    verify_script = args.verify_script.resolve(strict=True)
    if not verify_script.is_file() or verify_script.is_symlink():
        raise SystemExit("verify script must be a regular non-symlink file")

    backups = discover_backups(root)
    protected = {path for _, path in backups[: args.keep_latest]}
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
    candidates = [
        path
        for created_at, path in backups
        if path not in protected and created_at < cutoff
    ]

    if not candidates:
        print(
            f"Backup retention complete: kept {len(backups)}, pruned 0 "
            f"(minimum latest sets: {args.keep_latest})."
        )
        return 0

    for path in candidates:
        verify_backup(args.bash_bin, verify_script, path)

    if not args.confirm_delete_old_backups:
        for path in candidates:
            print(f"Would prune verified backup: {path}")
        print("No backups deleted without --confirm-delete-old-backups.")
        return 0

    for path in candidates:
        path.relative_to(root)
        shutil.rmtree(path)
        print(f"Pruned verified backup: {path}")
    print(
        f"Backup retention complete: kept {len(backups) - len(candidates)}, "
        f"pruned {len(candidates)}."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
