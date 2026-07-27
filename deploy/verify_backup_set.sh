#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    die "required command not found: python3 or python"
  fi
}

ALLOW_STAGING=0
if [[ $# -eq 2 && "$2" == "--allow-staging" ]]; then
  ALLOW_STAGING=1
elif [[ $# -ne 1 ]]; then
  die "usage: $0 /absolute/coordinated-backup-set [--allow-staging]"
fi

[[ "$1" = /* ]] || die "backup set path must be absolute"
[[ ! -L "$1" ]] || die "backup set path must not be a symbolic link"

require_command awk
require_command docker
require_command gzip
require_command realpath
require_command sed
require_command sha256sum
require_command tar

PYTHON_BIN="$(find_python)"
SET_DIR="$(realpath -e -- "$1")"
[[ -d "$SET_DIR" && -r "$SET_DIR" ]] || die "backup set is not a readable directory: $SET_DIR"
[[ ! -L "${SET_DIR}/manifest.json" && -f "${SET_DIR}/manifest.json" ]] ||
  die "manifest.json is missing or is not a regular file"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(realpath -- "${SCRIPT_DIR}/..")"
CENTER_ENV_FILE="${CENTER_ENV_FILE:-${PROJECT_DIR}/.env.production}"
CENTER_COMPOSE_FILE="${CENTER_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.production.yml}"
[[ -f "$CENTER_ENV_FILE" && -r "$CENTER_ENV_FILE" && ! -L "$CENTER_ENV_FILE" ]] ||
  die "center environment file is missing or unsafe: $CENTER_ENV_FILE"
[[ -f "$CENTER_COMPOSE_FILE" && -r "$CENTER_COMPOSE_FILE" && ! -L "$CENTER_COMPOSE_FILE" ]] ||
  die "center Compose file is missing or unsafe: $CENTER_COMPOSE_FILE"

mapfile -t MANIFEST_VALUES < <(
  "$PYTHON_BIN" - "$SET_DIR" "$ALLOW_STAGING" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(newline="\n")

class ManifestError(ValueError):
    pass


def fail(message: str) -> None:
    raise ManifestError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def exact_keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        fail(f"{name} has unexpected or missing fields")


def parse_time(value, name):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        fail(f"{name} is not a UTC timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def safe_relative_path(root: Path, value: object, pattern: str, name: str) -> Path:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        fail(f"{name} has an invalid path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        fail(f"{name} must be a normalized relative path")

    current = root
    for part in pure.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            fail(f"{name} does not exist")
        if stat.S_ISLNK(mode):
            fail(f"{name} contains a symbolic link")

    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        fail(f"{name} escapes the backup set")
    return resolved


root = Path(sys.argv[1]).resolve(strict=True)
allow_staging = sys.argv[2] == "1"
manifest_path = root / "manifest.json"
if manifest_path.stat().st_size > 65536:
    fail("manifest.json is too large")

with manifest_path.open("r", encoding="utf-8") as handle:
    manifest = json.load(handle, object_pairs_hook=unique_object)

exact_keys(
    manifest,
    ("schema_version", "batch_id", "status", "created_at", "completed_at", "consistency", "services"),
    "manifest",
)
if manifest["schema_version"] != 1 or manifest["status"] != "complete":
    fail("unsupported or incomplete manifest")

batch_id = manifest["batch_id"]
if not isinstance(batch_id, str) or not re.fullmatch(
    r"\d{8}T\d{6}Z-[0-9a-f]{12}", batch_id
):
    fail("invalid batch_id")
normal_name = f"coordinated-{batch_id}"
staging_pattern = rf"\.coordinated-{re.escape(batch_id)}\.partial\.[A-Za-z0-9]+"
if root.name != normal_name and not (
    allow_staging and re.fullmatch(staging_pattern, root.name)
):
    fail("backup directory name and batch_id do not match")

created_at = parse_time(manifest["created_at"], "created_at")
completed_at = parse_time(manifest["completed_at"], "completed_at")
if completed_at < created_at:
    fail("completed_at precedes created_at")
if created_at.strftime("%Y%m%dT%H%M%SZ") != batch_id.split("-", 1)[0]:
    fail("batch_id timestamp and created_at do not match")

consistency = manifest["consistency"]
exact_keys(
    consistency,
    (
        "strategy",
        "write_services",
        "write_stop_started_at",
        "snapshot_completed_at",
        "services_restored_at",
        "original_running_state",
    ),
    "consistency",
)
if consistency["strategy"] != "coordinated_write_stop":
    fail("unsupported consistency strategy")
expected_write_services = [
    "ai_cs:nginx",
    "ai_cs:api",
    "ai_cs:knowledge-worker",
    "wechat_center:caddy",
    "wechat_center:center",
]
if consistency["write_services"] != expected_write_services:
    fail("write service set is incomplete")
exact_keys(
    consistency["original_running_state"],
    (
        "ai_cs:api",
        "ai_cs:nginx",
        "ai_cs:knowledge-worker",
        "wechat_center:center",
        "wechat_center:caddy",
    ),
    "original_running_state",
)
if not all(isinstance(value, bool) for value in consistency["original_running_state"].values()):
    fail("original_running_state values must be booleans")

write_stop = parse_time(consistency["write_stop_started_at"], "write_stop_started_at")
snapshot_done = parse_time(consistency["snapshot_completed_at"], "snapshot_completed_at")
services_restored = parse_time(consistency["services_restored_at"], "services_restored_at")
if not (created_at <= write_stop <= snapshot_done <= services_restored == completed_at):
    fail("consistency timestamps are out of order")

services = manifest["services"]
exact_keys(services, ("ai_cs", "wechat_center"), "services")
expected = {
    "ai_cs": {
        "engine": "mysql",
        "type": "directory",
        "scheme": "sha256_of_SHA256SUMS",
        "path_pattern": r"ai-cs/ai-cs-\d{8}T\d{6}Z",
    },
    "wechat_center": {
        "engine": "postgresql",
        "type": "file",
        "scheme": "sha256",
        "path_pattern": r"center/wechat-ai-postgres-\d{8}T\d{6}Z\.dump",
    },
}

paths = {}
hashes = {}
for service_name, contract in expected.items():
    service = services[service_name]
    exact_keys(service, ("batch_id", "service_version", "database", "backup"), service_name)
    if service["batch_id"] != batch_id:
        fail(f"{service_name} belongs to a different batch")

    service_version = service["service_version"]
    exact_keys(service_version, ("image_id", "compose_file_sha256"), f"{service_name}.service_version")
    if not isinstance(service_version["image_id"], str) or not re.fullmatch(
        r"(?:sha256:)?[0-9a-f]{12,64}", service_version["image_id"]
    ):
        fail(f"{service_name} has an invalid image ID")
    if not isinstance(service_version["compose_file_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", service_version["compose_file_sha256"]
    ):
        fail(f"{service_name} has an invalid Compose hash")

    database = service["database"]
    exact_keys(database, ("engine", "migration_revision"), f"{service_name}.database")
    if database["engine"] != contract["engine"]:
        fail(f"{service_name} database engine mismatch")
    if not isinstance(database["migration_revision"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", database["migration_revision"]
    ):
        fail(f"{service_name} has an invalid migration revision")

    backup = service["backup"]
    exact_keys(backup, ("path", "type", "sha256", "sha256_scheme"), f"{service_name}.backup")
    if backup["type"] != contract["type"] or backup["sha256_scheme"] != contract["scheme"]:
        fail(f"{service_name} backup contract mismatch")
    if not isinstance(backup["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", backup["sha256"]):
        fail(f"{service_name} has an invalid backup SHA-256")
    paths[service_name] = safe_relative_path(
        root, backup["path"], contract["path_pattern"], f"{service_name}.backup.path"
    )
    hashes[service_name] = backup["sha256"]

expected_top_level = {"manifest.json", "ai-cs", "center"}
if {entry.name for entry in root.iterdir()} != expected_top_level:
    fail("backup set contains unexpected or missing top-level entries")
if any(entry.is_symlink() for entry in root.rglob("*")):
    fail("backup set contains a symbolic link")

ai_path = paths["ai_cs"]
if not ai_path.is_dir():
    fail("AI-cs backup path is not a directory")
if {entry.name for entry in ai_path.iterdir()} != {
    "ai-cs-mysql.sql.gz",
    "ai-cs-data.tar.gz",
    "SHA256SUMS",
}:
    fail("AI-cs backup directory has an invalid structure")
if not all(entry.is_file() for entry in ai_path.iterdir()):
    fail("AI-cs backup contains a non-file entry")

center_path = paths["wechat_center"]
if not center_path.is_file():
    fail("center backup path is not a regular file")
checksum_path = Path(f"{center_path}.sha256")
if not checksum_path.is_file() or checksum_path.is_symlink():
    fail("center checksum companion is missing or unsafe")
if {entry.name for entry in center_path.parent.iterdir()} != {
    center_path.name,
    checksum_path.name,
}:
    fail("center backup directory has an invalid structure")

print(services["ai_cs"]["backup"]["path"])
print(services["wechat_center"]["backup"]["path"])
print(hashes["ai_cs"])
print(hashes["wechat_center"])
PY
)
[[ "${#MANIFEST_VALUES[@]}" -eq 4 ]] || die "manifest validation did not return the expected values"
AI_BACKUP_PATH="${SET_DIR}/${MANIFEST_VALUES[0]}"
CENTER_BACKUP_PATH="${SET_DIR}/${MANIFEST_VALUES[1]}"
EXPECTED_AI_SHA256="${MANIFEST_VALUES[2]}"
EXPECTED_CENTER_SHA256="${MANIFEST_VALUES[3]}"

awk '
  NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/ { exit 1 }
  $2 != "ai-cs-mysql.sql.gz" && $2 != "ai-cs-data.tar.gz" { exit 1 }
  { seen[$2] += 1; count += 1 }
  END {
    if (count != 2 || seen["ai-cs-mysql.sql.gz"] != 1 || seen["ai-cs-data.tar.gz"] != 1) {
      exit 1
    }
  }
' "${AI_BACKUP_PATH}/SHA256SUMS" || die "AI-cs checksum manifest is invalid"
ACTUAL_AI_SHA256="$(sha256sum -- "${AI_BACKUP_PATH}/SHA256SUMS" | awk '{print $1}')"
[[ "$ACTUAL_AI_SHA256" == "$EXPECTED_AI_SHA256" ]] ||
  die "AI-cs child backup checksum failed (expected $EXPECTED_AI_SHA256, got $ACTUAL_AI_SHA256)"
(
  cd "$AI_BACKUP_PATH"
  sha256sum --check --strict SHA256SUMS
)
gzip -t "${AI_BACKUP_PATH}/ai-cs-mysql.sql.gz"
tar -tzf "${AI_BACKUP_PATH}/ai-cs-data.tar.gz" | awk '
  $0 ~ /(^|\/)\.\.(\/|$)/ || $0 ~ /^\// || ($0 !~ /^data(\/|$)/) { bad=1 }
  END { exit bad ? 1 : 0 }
' || die "AI-cs data archive contains an unsafe path"
tar -tvzf "${AI_BACKUP_PATH}/ai-cs-data.tar.gz" | awk '
  substr($1, 1, 1) == "l" || substr($1, 1, 1) == "h" { bad=1 }
  END { exit bad ? 1 : 0 }
' || die "AI-cs data archive must not contain links"

ACTUAL_CENTER_SHA256="$(sha256sum -- "$CENTER_BACKUP_PATH" | awk '{print $1}')"
[[ "$ACTUAL_CENTER_SHA256" == "$EXPECTED_CENTER_SHA256" ]] ||
  die "center child backup checksum failed (expected $EXPECTED_CENTER_SHA256, got $ACTUAL_CENTER_SHA256)"
awk -v expected_hash="$EXPECTED_CENTER_SHA256" -v expected_name="$(basename -- "$CENTER_BACKUP_PATH")" '
  NF != 2 || $1 != expected_hash || $2 != expected_name { exit 1 }
  { count += 1 }
  END { exit count == 1 ? 0 : 1 }
' "${CENTER_BACKUP_PATH}.sha256" || die "center checksum companion is invalid"

CENTER_COMPOSE=(docker compose --env-file "$CENTER_ENV_FILE" -f "$CENTER_COMPOSE_FILE")
"${CENTER_COMPOSE[@]}" config --quiet
"${CENTER_COMPOSE[@]}" exec -T postgres pg_restore --list <"$CENTER_BACKUP_PATH" >/dev/null

printf 'Backup set verified: %s\n' "$SET_DIR"
printf 'Batch ID: %s\n' "$(basename -- "$SET_DIR" | sed -E 's/^\\.?coordinated-//; s/\\.partial\\..*$//')"
printf 'Both child backups are structurally readable by their restore tools.\n'
