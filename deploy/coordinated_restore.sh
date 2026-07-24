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

CONFIRMATION="--confirm-replace-both-systems"
[[ $# -eq 2 && "$2" == "$CONFIRMATION" ]] ||
  die "usage: $0 /absolute/coordinated-backup-set $CONFIRMATION"
[[ "$1" = /* ]] || die "backup set path must be absolute"
[[ ! -L "$1" ]] || die "backup set path must not be a symbolic link"

for command in awk bash comm docker grep mktemp realpath sort tr; do
  require_command "$command"
done
PYTHON_BIN="$(find_python)"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(realpath -- "${SCRIPT_DIR}/..")"
AI_CS_PROJECT_DIR="$(realpath -e -- "${AI_CS_PROJECT_DIR:-${PROJECT_DIR}/../AI-cs}")"
SET_DIR="$(realpath -e -- "$1")"

CENTER_ENV_FILE="${CENTER_ENV_FILE:-${PROJECT_DIR}/.env.production}"
CENTER_COMPOSE_FILE="${CENTER_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.production.yml}"
CENTER_RESTORE_SCRIPT="${CENTER_RESTORE_SCRIPT:-${PROJECT_DIR}/deploy/restore_postgres.sh}"
AI_CS_ENV_FILE="${AI_CS_ENV_FILE:-${AI_CS_PROJECT_DIR}/.env}"
AI_CS_COMPOSE_FILE="${AI_CS_COMPOSE_FILE:-${AI_CS_PROJECT_DIR}/docker-compose.yml}"
AI_CS_RESTORE_SCRIPT="${AI_CS_RESTORE_SCRIPT:-${AI_CS_PROJECT_DIR}/deploy/restore_ai_cs.sh}"
VERIFY_SCRIPT="${VERIFY_SCRIPT:-${PROJECT_DIR}/deploy/verify_backup_set.sh}"

for path in \
  "$CENTER_ENV_FILE" \
  "$CENTER_COMPOSE_FILE" \
  "$CENTER_RESTORE_SCRIPT" \
  "$AI_CS_ENV_FILE" \
  "$AI_CS_COMPOSE_FILE" \
  "$AI_CS_RESTORE_SCRIPT" \
  "$VERIFY_SCRIPT"; do
  [[ -f "$path" && -r "$path" && ! -L "$path" ]] ||
    die "required regular file is missing or unsafe: $path"
done

CENTER_COMPOSE=(docker compose --env-file "$CENTER_ENV_FILE" -f "$CENTER_COMPOSE_FILE")
AI_CS_COMPOSE=(docker compose --env-file "$AI_CS_ENV_FILE" -f "$AI_CS_COMPOSE_FILE")
"${CENTER_COMPOSE[@]}" config --quiet
"${AI_CS_COMPOSE[@]}" config --quiet
bash "$VERIFY_SCRIPT" "$SET_DIR"

mapfile -t CHILD_PATHS < <(
  "$PYTHON_BIN" - "$SET_DIR" <<'PY'
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import sys

root = Path(sys.argv[1]).resolve(strict=True)
with (root / "manifest.json").open("r", encoding="utf-8") as handle:
    manifest = json.load(handle)

for service_name, expected_kind in (
    ("ai_cs", "directory"),
    ("wechat_center", "file"),
):
    relative = manifest["services"][service_name]["backup"]["path"]
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise SystemExit("unsafe child backup path")
    path = root.joinpath(*pure.parts).resolve(strict=True)
    path.relative_to(root)
    if expected_kind == "directory" and not path.is_dir():
        raise SystemExit(f"{service_name} child backup is not a directory")
    if expected_kind == "file" and not path.is_file():
        raise SystemExit(f"{service_name} child backup is not a file")
    print(pure.as_posix())
PY
)
[[ "${#CHILD_PATHS[@]}" -eq 2 ]] ||
  die "manifest did not resolve both child backup paths"
AI_BACKUP_PATH="${SET_DIR}/${CHILD_PATHS[0]}"
CENTER_BACKUP_PATH="${SET_DIR}/${CHILD_PATHS[1]}"

LOCK_DIR="${RESTORE_LOCK_DIR:-${PROJECT_DIR}/.coordinated-restore.lock}"
[[ "$LOCK_DIR" = /* && ! -L "$LOCK_DIR" ]] ||
  die "restore lock path must be absolute and must not be a symbolic link"
mkdir -- "$LOCK_DIR" 2>/dev/null ||
  die "another restore may be running; inspect and remove only a stale lock: $LOCK_DIR"

TEMP_DIR="$(mktemp -d)"
RESTORE_STARTED=0
cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ "$RESTORE_STARTED" -eq 1 && "$status" -ne 0 ]]; then
    set +e
    "${AI_CS_COMPOSE[@]}" stop nginx >/dev/null 2>&1
    "${CENTER_COMPOSE[@]}" --profile standalone-edge stop caddy >/dev/null 2>&1
    set -e
    printf 'ERROR: coordinated restore failed; all public ingress remains stopped.\n' >&2
  fi
  rm -rf -- "$TEMP_DIR"
  rmdir -- "$LOCK_DIR" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

RESTORE_STARTED=1
"${AI_CS_COMPOSE[@]}" stop nginx
"${CENTER_COMPOSE[@]}" --profile standalone-edge stop caddy

RESTORE_START_INGRESS=0 \
  ENV_FILE="$CENTER_ENV_FILE" \
  COMPOSE_FILE="$CENTER_COMPOSE_FILE" \
  bash "$CENTER_RESTORE_SCRIPT" \
    "$CENTER_BACKUP_PATH" \
    --confirm-replace-database

RESTORE_START_INGRESS=0 \
  ENV_FILE="$AI_CS_ENV_FILE" \
  COMPOSE_FILE="$AI_CS_COMPOSE_FILE" \
  bash "$AI_CS_RESTORE_SCRIPT" \
    "$AI_BACKUP_PATH" \
    --confirm-replace-data

"${CENTER_COMPOSE[@]}" exec -T center \
  python main.py --env .env.production center db-status
"${AI_CS_COMPOSE[@]}" exec -T api \
  python -m app.db_migrations check
"${AI_CS_COMPOSE[@]}" exec -T api \
  python scripts/reconcile_knowledge_store.py

AI_BINDINGS="${TEMP_DIR}/ai-bindings"
CENTER_BINDINGS="${TEMP_DIR}/center-bindings"
MISSING_BINDINGS="${TEMP_DIR}/missing-bindings"

"${AI_CS_COMPOSE[@]}" exec -T mysql sh -euc '
  exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql \
    --user=root \
    --batch \
    --skip-column-names \
    "$MYSQL_DATABASE" \
    --execute="
      SELECT CONCAT(center_tenant_id, CHAR(9), channel_account_id)
      FROM personal_wechat_bindings
      WHERE status = '"'"'active'"'"'
      ORDER BY center_tenant_id, channel_account_id
    "
' | tr -d '\r' | sort -u >"$AI_BINDINGS"

"${CENTER_COMPOSE[@]}" exec -T postgres sh -euc '
  exec psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --command="
      SELECT tenant_id || E'"'"'\t'"'"' || id
      FROM channel_accounts
      ORDER BY tenant_id, id
    "
' | tr -d '\r' | awk 'NF { print }' | sort -u >"$CENTER_BINDINGS"

for path in "$AI_BINDINGS" "$CENTER_BINDINGS"; do
  awk '
    NF != 2 ||
    $1 !~ /^[A-Za-z0-9_.:-]+$/ ||
    $2 !~ /^[A-Za-z0-9_.:-]+$/ { exit 1 }
  ' "$path" || die "restored binding data has an invalid identifier"
done

comm -23 "$AI_BINDINGS" "$CENTER_BINDINGS" >"$MISSING_BINDINGS"
[[ ! -s "$MISSING_BINDINGS" ]] ||
  die "one or more active AI-cs personal WeChat bindings are missing in the center"

"${AI_CS_COMPOSE[@]}" up -d --wait nginx
RESTORE_STARTED=0

printf 'Coordinated restore completed from: %s\n' "$SET_DIR"
printf 'Cross-system bindings verified: %s\n' "$(awk 'END { print NR + 0 }' "$AI_BINDINGS")"
printf 'Public ingress restarted only after both systems passed readiness checks.\n'
