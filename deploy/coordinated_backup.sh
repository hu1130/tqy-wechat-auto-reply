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

compose_service_is_running() {
  local service="$1"
  shift
  "$@" ps --status running --services | grep -Fxq -- "$service"
}

compose_image_id() {
  local service="$1"
  shift
  local image_id
  image_id="$("$@" images -q "$service" | awk 'NF { print; exit }')"
  [[ "$image_id" =~ ^(sha256:)?[0-9a-f]{12,64}$ ]] ||
    die "could not determine an immutable image ID for service: $service"
  printf '%s\n' "$image_id"
}

single_revision() {
  local value
  value="$(awk '
    NF {
      count += 1
      revision = $1
      if (NF != 1) {
        bad = 1
      }
    }
    END {
      if (bad || count != 1) {
        exit 1
      }
      print revision
    }
  ')"
  [[ "$value" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid database migration revision"
  printf '%s\n' "$value"
}

[[ $# -eq 1 ]] || die "usage: $0 /absolute/coordinated/backup/root"
[[ "$1" = /* ]] || die "backup root must be an absolute path"
[[ ! -L "$1" ]] || die "backup root must not be a symbolic link"

require_command bash
require_command date
require_command docker
require_command find
require_command flock
require_command grep
require_command realpath
require_command sha256sum

PYTHON_BIN="$(find_python)"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(realpath -- "${SCRIPT_DIR}/..")"
AI_CS_PROJECT_DIR="$(realpath -e -- "${AI_CS_PROJECT_DIR:-${PROJECT_DIR}/../AI-cs}")"

CENTER_ENV_FILE="${CENTER_ENV_FILE:-${PROJECT_DIR}/.env.production}"
CENTER_COMPOSE_FILE="${CENTER_COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.production.yml}"
CENTER_BACKUP_SCRIPT="${CENTER_BACKUP_SCRIPT:-${PROJECT_DIR}/deploy/backup_postgres.sh}"
AI_CS_ENV_FILE="${AI_CS_ENV_FILE:-${AI_CS_PROJECT_DIR}/.env}"
AI_CS_COMPOSE_FILE="${AI_CS_COMPOSE_FILE:-${AI_CS_PROJECT_DIR}/docker-compose.yml}"
AI_CS_BACKUP_SCRIPT="${AI_CS_BACKUP_SCRIPT:-${AI_CS_PROJECT_DIR}/deploy/backup_ai_cs.sh}"
VERIFY_SCRIPT="${VERIFY_SCRIPT:-${PROJECT_DIR}/deploy/verify_backup_set.sh}"
PRUNE_SCRIPT="${PRUNE_SCRIPT:-${PROJECT_DIR}/deploy/prune_coordinated_backups.py}"
RETENTION_DAYS="${COORDINATED_BACKUP_RETENTION_DAYS:-30}"
KEEP_LATEST="${COORDINATED_BACKUP_KEEP_LATEST:-7}"

for path in \
  "$CENTER_ENV_FILE" \
  "$CENTER_COMPOSE_FILE" \
  "$CENTER_BACKUP_SCRIPT" \
  "$AI_CS_ENV_FILE" \
  "$AI_CS_COMPOSE_FILE" \
  "$AI_CS_BACKUP_SCRIPT" \
  "$VERIFY_SCRIPT" \
  "$PRUNE_SCRIPT"; do
  [[ -f "$path" && -r "$path" && ! -L "$path" ]] || die "required regular file is missing or unsafe: $path"
done

mkdir -p -- "$1"
BACKUP_ROOT="$(realpath -- "$1")"
[[ "$BACKUP_ROOT" != "/" ]] || die "refusing to write backups to the filesystem root"
[[ -d "$BACKUP_ROOT" && -w "$BACKUP_ROOT" ]] || die "backup root is not writable: $BACKUP_ROOT"

LOCK_FILE="${COORDINATED_BACKUP_LOCK_FILE:-${BACKUP_ROOT}/.coordinated-backup.lock}"
[[ "$LOCK_FILE" = /* && ! -L "$LOCK_FILE" ]] ||
  die "backup lock path must be absolute and must not be a symbolic link"

CENTER_COMPOSE=(docker compose --env-file "$CENTER_ENV_FILE" -f "$CENTER_COMPOSE_FILE")
AI_CS_COMPOSE=(docker compose --env-file "$AI_CS_ENV_FILE" -f "$AI_CS_COMPOSE_FILE")

BATCH_TIMESTAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
CREATED_AT="${BATCH_TIMESTAMP:0:4}-${BATCH_TIMESTAMP:4:2}-${BATCH_TIMESTAMP:6:2}T${BATCH_TIMESTAMP:9:2}:${BATCH_TIMESTAMP:11:2}:${BATCH_TIMESTAMP:13:2}Z"
BATCH_ID="${COORDINATED_BATCH_ID:-${BATCH_TIMESTAMP}-$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(6))')}"
[[ "$BATCH_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] || die "invalid coordinated batch ID"
[[ "${BATCH_ID%%-*}" == "$BATCH_TIMESTAMP" ]] || die "batch ID timestamp must match the current UTC second"

FINAL_DIR="${BACKUP_ROOT}/coordinated-${BATCH_ID}"
[[ ! -e "$FINAL_DIR" ]] || die "coordinated backup already exists: $FINAL_DIR"
STAGING_DIR=""

AI_API_WAS_RUNNING=0
AI_NGINX_WAS_RUNNING=0
AI_KNOWLEDGE_WORKER_WAS_RUNNING=0
CENTER_API_WAS_RUNNING=0
CENTER_CADDY_WAS_RUNNING=0
RESTORE_REQUIRED=0

restore_original_services() {
  local failed=0

  if [[ "$AI_API_WAS_RUNNING" -eq 1 ]]; then
    "${AI_CS_COMPOSE[@]}" up -d api || failed=1
  fi
  if [[ "$AI_KNOWLEDGE_WORKER_WAS_RUNNING" -eq 1 ]]; then
    "${AI_CS_COMPOSE[@]}" up -d knowledge-worker || failed=1
  fi
  if [[ "$CENTER_API_WAS_RUNNING" -eq 1 ]]; then
    "${CENTER_COMPOSE[@]}" up -d center || failed=1
  fi
  if [[ "$AI_NGINX_WAS_RUNNING" -eq 1 ]]; then
    "${AI_CS_COMPOSE[@]}" up -d nginx || failed=1
  fi
  if [[ "$CENTER_CADDY_WAS_RUNNING" -eq 1 ]]; then
    "${CENTER_COMPOSE[@]}" up -d caddy || failed=1
  fi

  return "$failed"
}

remove_staging() {
  [[ -n "${STAGING_DIR:-}" && -e "$STAGING_DIR" ]] || return 0
  [[ "$STAGING_DIR" == "$BACKUP_ROOT"/.coordinated-"$BATCH_ID".partial.* ]] ||
    die "refusing to remove unsafe staging directory: $STAGING_DIR"
  rm -rf -- "$STAGING_DIR"
}

cleanup() {
  local status=$?
  local restore_status=0
  trap - EXIT HUP INT TERM

  if [[ "$RESTORE_REQUIRED" -eq 1 ]]; then
    restore_original_services || restore_status=$?
  fi
  remove_staging
  if [[ "$status" -eq 0 && "$restore_status" -ne 0 ]]; then
    status="$restore_status"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

exec {LOCK_FD}>"$LOCK_FILE"
flock -n "$LOCK_FD" ||
  die "another coordinated backup is already running"

"${CENTER_COMPOSE[@]}" config --quiet
"${AI_CS_COMPOSE[@]}" config --quiet

STAGING_DIR="$(mktemp -d --tmpdir="$BACKUP_ROOT" ".coordinated-${BATCH_ID}.partial.XXXXXX")"
STAGING_DIR="$(realpath -- "$STAGING_DIR")"
[[ "$STAGING_DIR" == "$BACKUP_ROOT"/.coordinated-"$BATCH_ID".partial.* ]] ||
  die "unsafe staging directory: $STAGING_DIR"

if compose_service_is_running api "${AI_CS_COMPOSE[@]}"; then
  AI_API_WAS_RUNNING=1
fi
if compose_service_is_running nginx "${AI_CS_COMPOSE[@]}"; then
  AI_NGINX_WAS_RUNNING=1
fi
if compose_service_is_running knowledge-worker "${AI_CS_COMPOSE[@]}"; then
  AI_KNOWLEDGE_WORKER_WAS_RUNNING=1
fi
if compose_service_is_running center "${CENTER_COMPOSE[@]}"; then
  CENTER_API_WAS_RUNNING=1
fi
if compose_service_is_running caddy "${CENTER_COMPOSE[@]}"; then
  CENTER_CADDY_WAS_RUNNING=1
fi

[[ "$AI_NGINX_WAS_RUNNING" -eq 0 || "$AI_API_WAS_RUNNING" -eq 1 ]] ||
  die "AI-cs nginx is running while api is stopped"
[[ "$CENTER_CADDY_WAS_RUNNING" -eq 0 || "$CENTER_API_WAS_RUNNING" -eq 1 ]] ||
  die "center caddy is running while center is stopped"

RESTORE_REQUIRED=1
WRITE_STOP_STARTED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
if [[ "$AI_NGINX_WAS_RUNNING" -eq 1 ]]; then
  "${AI_CS_COMPOSE[@]}" stop nginx
fi
if [[ "$CENTER_CADDY_WAS_RUNNING" -eq 1 ]]; then
  "${CENTER_COMPOSE[@]}" stop caddy
fi
if [[ "$AI_API_WAS_RUNNING" -eq 1 ]]; then
  "${AI_CS_COMPOSE[@]}" stop api
fi
if [[ "$AI_KNOWLEDGE_WORKER_WAS_RUNNING" -eq 1 ]]; then
  "${AI_CS_COMPOSE[@]}" stop knowledge-worker
fi
if [[ "$CENTER_API_WAS_RUNNING" -eq 1 ]]; then
  "${CENTER_COMPOSE[@]}" stop center
fi

for service in nginx api knowledge-worker; do
  if compose_service_is_running "$service" "${AI_CS_COMPOSE[@]}"; then
    die "AI-cs write service did not stop: $service"
  fi
done
for service in caddy center; do
  if compose_service_is_running "$service" "${CENTER_COMPOSE[@]}"; then
    die "center write service did not stop: $service"
  fi
done

AI_IMAGE_ID="$(compose_image_id api "${AI_CS_COMPOSE[@]}")"
CENTER_IMAGE_ID="$(compose_image_id center "${CENTER_COMPOSE[@]}")"
AI_COMPOSE_SHA256="$(sha256sum -- "$AI_CS_COMPOSE_FILE" | awk '{print $1}')"
CENTER_COMPOSE_SHA256="$(sha256sum -- "$CENTER_COMPOSE_FILE" | awk '{print $1}')"
AI_MIGRATION_REVISION="$(
  "${AI_CS_COMPOSE[@]}" exec -T mysql sh -euc '
    exec env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql \
      --user=root \
      --batch \
      --skip-column-names \
      "$MYSQL_DATABASE" \
      --execute="SELECT version_num FROM alembic_version"
  ' | single_revision
)"
CENTER_MIGRATION_REVISION="$(
  "${CENTER_COMPOSE[@]}" exec -T postgres sh -euc '
    exec psql \
      --username "$POSTGRES_USER" \
      --dbname "$POSTGRES_DB" \
      --tuples-only \
      --no-align \
      --command="SELECT version_num FROM alembic_version"
  ' | single_revision
)"

mkdir -p -- "${STAGING_DIR}/ai-cs" "${STAGING_DIR}/center"
ENV_FILE="$AI_CS_ENV_FILE" COMPOSE_FILE="$AI_CS_COMPOSE_FILE" \
  bash "$AI_CS_BACKUP_SCRIPT" "${STAGING_DIR}/ai-cs"
ENV_FILE="$CENTER_ENV_FILE" COMPOSE_FILE="$CENTER_COMPOSE_FILE" \
  bash "$CENTER_BACKUP_SCRIPT" "${STAGING_DIR}/center"
SNAPSHOT_COMPLETED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

shopt -s nullglob
AI_CHILDREN=("${STAGING_DIR}/ai-cs"/*)
[[ "${#AI_CHILDREN[@]}" -eq 1 ]] || die "AI-cs backup script did not create exactly one child backup"
AI_BACKUP_PATH="${AI_CHILDREN[0]}"
[[ -d "$AI_BACKUP_PATH" && ! -L "$AI_BACKUP_PATH" ]] || die "AI-cs child backup is not a regular directory"

CENTER_DUMPS=("${STAGING_DIR}/center"/wechat-ai-postgres-*.dump)
[[ "${#CENTER_DUMPS[@]}" -eq 1 ]] || die "center backup script did not create exactly one dump"
CENTER_BACKUP_PATH="${CENTER_DUMPS[0]}"
[[ -f "$CENTER_BACKUP_PATH" && ! -L "$CENTER_BACKUP_PATH" ]] ||
  die "center child backup must be a regular file"

AI_RELATIVE_PATH="$(realpath --relative-to="$STAGING_DIR" -- "$AI_BACKUP_PATH")"
CENTER_RELATIVE_PATH="$(realpath --relative-to="$STAGING_DIR" -- "$CENTER_BACKUP_PATH")"
AI_BACKUP_SHA256="$(sha256sum -- "${AI_BACKUP_PATH}/SHA256SUMS" | awk '{print $1}')"
CENTER_BACKUP_SHA256="$(sha256sum -- "$CENTER_BACKUP_PATH" | awk '{print $1}')"
[[ "$AI_BACKUP_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid AI-cs child backup checksum"
[[ "$CENTER_BACKUP_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid center child backup checksum"

if ! restore_original_services; then
  die "one or more write services could not be restored after backup"
fi
RESTORE_REQUIRED=0
SERVICES_RESTORED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

"$PYTHON_BIN" - \
  "${STAGING_DIR}/manifest.json" \
  "$BATCH_ID" \
  "$CREATED_AT" \
  "$WRITE_STOP_STARTED_AT" \
  "$SNAPSHOT_COMPLETED_AT" \
  "$SERVICES_RESTORED_AT" \
  "$AI_RELATIVE_PATH" \
  "$AI_BACKUP_SHA256" \
  "$AI_IMAGE_ID" \
  "$AI_COMPOSE_SHA256" \
  "$AI_MIGRATION_REVISION" \
  "$CENTER_RELATIVE_PATH" \
  "$CENTER_BACKUP_SHA256" \
  "$CENTER_IMAGE_ID" \
  "$CENTER_COMPOSE_SHA256" \
  "$CENTER_MIGRATION_REVISION" \
  "$AI_API_WAS_RUNNING" \
  "$AI_NGINX_WAS_RUNNING" \
  "$AI_KNOWLEDGE_WORKER_WAS_RUNNING" \
  "$CENTER_API_WAS_RUNNING" \
  "$CENTER_CADDY_WAS_RUNNING" <<'PY'
import json
import sys

(
    manifest_path,
    batch_id,
    created_at,
    write_stop_started_at,
    snapshot_completed_at,
    services_restored_at,
    ai_path,
    ai_sha256,
    ai_image_id,
    ai_compose_sha256,
    ai_migration_revision,
    center_path,
    center_sha256,
    center_image_id,
    center_compose_sha256,
    center_migration_revision,
    ai_api_was_running,
    ai_nginx_was_running,
    ai_knowledge_worker_was_running,
    center_api_was_running,
    center_caddy_was_running,
) = sys.argv[1:]

manifest = {
    "schema_version": 1,
    "batch_id": batch_id,
    "status": "complete",
    "created_at": created_at,
    "completed_at": services_restored_at,
    "consistency": {
        "strategy": "coordinated_write_stop",
        "write_services": [
            "ai_cs:nginx",
            "ai_cs:api",
            "ai_cs:knowledge-worker",
            "wechat_center:caddy",
            "wechat_center:center",
        ],
        "write_stop_started_at": write_stop_started_at,
        "snapshot_completed_at": snapshot_completed_at,
        "services_restored_at": services_restored_at,
        "original_running_state": {
            "ai_cs:api": ai_api_was_running == "1",
            "ai_cs:nginx": ai_nginx_was_running == "1",
            "ai_cs:knowledge-worker": ai_knowledge_worker_was_running == "1",
            "wechat_center:center": center_api_was_running == "1",
            "wechat_center:caddy": center_caddy_was_running == "1",
        },
    },
    "services": {
        "ai_cs": {
            "batch_id": batch_id,
            "service_version": {
                "image_id": ai_image_id,
                "compose_file_sha256": ai_compose_sha256,
            },
            "database": {
                "engine": "mysql",
                "migration_revision": ai_migration_revision,
            },
            "backup": {
                "path": ai_path,
                "type": "directory",
                "sha256": ai_sha256,
                "sha256_scheme": "sha256_of_SHA256SUMS",
            },
        },
        "wechat_center": {
            "batch_id": batch_id,
            "service_version": {
                "image_id": center_image_id,
                "compose_file_sha256": center_compose_sha256,
            },
            "database": {
                "engine": "postgresql",
                "migration_revision": center_migration_revision,
            },
            "backup": {
                "path": center_path,
                "type": "file",
                "sha256": center_sha256,
                "sha256_scheme": "sha256",
            },
        },
    },
}

with open(manifest_path, "x", encoding="utf-8", newline="\n") as handle:
    json.dump(manifest, handle, ensure_ascii=True, indent=2, sort_keys=True)
    handle.write("\n")
PY

bash "$VERIFY_SCRIPT" "$STAGING_DIR" --allow-staging
mv -- "$STAGING_DIR" "$FINAL_DIR"
STAGING_DIR=""

"$PYTHON_BIN" "$PRUNE_SCRIPT" \
  "$BACKUP_ROOT" \
  --retention-days "$RETENTION_DAYS" \
  --keep-latest "$KEEP_LATEST" \
  --verify-script "$VERIFY_SCRIPT" \
  --bash-bin "$(command -v bash)" \
  --confirm-delete-old-backups

printf 'Coordinated backup verified: %s\n' "$FINAL_DIR"
printf 'Batch ID: %s\n' "$BATCH_ID"
printf 'Do not restore either child backup without the other child from this batch.\n'
