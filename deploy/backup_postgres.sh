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

[[ $# -eq 1 ]] || die "usage: $0 /absolute/backup/directory"
[[ "$1" = /* ]] || die "backup directory must be an absolute path"

require_command docker
require_command realpath
require_command sha256sum

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(realpath -- "${SCRIPT_DIR}/..")"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.production.yml}"

[[ -f "$ENV_FILE" ]] || die "environment file not found: $ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"
export CENTER_ENV_FILE="$ENV_FILE"

mkdir -p -- "$1"
BACKUP_DIR="$(realpath -- "$1")"
[[ "$BACKUP_DIR" != "/" ]] || die "refusing to write backups to the filesystem root"
[[ -d "$BACKUP_DIR" && -w "$BACKUP_DIR" ]] || die "backup directory is not writable: $BACKUP_DIR"

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
"${COMPOSE[@]}" config --quiet

TIMESTAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
FINAL_NAME="wechat-ai-postgres-${TIMESTAMP}.dump"
FINAL_DUMP="${BACKUP_DIR}/${FINAL_NAME}"
FINAL_CHECKSUM="${FINAL_DUMP}.sha256"
TMP_DUMP="$(mktemp --tmpdir="$BACKUP_DIR" ".${FINAL_NAME}.tmp.XXXXXX")"
TMP_CHECKSUM="${TMP_DUMP}.sha256"

cleanup() {
  rm -f -- "$TMP_DUMP" "$TMP_CHECKSUM"
}
trap cleanup EXIT HUP INT TERM

[[ ! -e "$FINAL_DUMP" && ! -e "$FINAL_CHECKSUM" ]] || die "backup already exists: $FINAL_DUMP"

"${COMPOSE[@]}" exec -T postgres sh -eu -c '
  exec pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-acl
' >"$TMP_DUMP"

[[ -s "$TMP_DUMP" ]] || die "pg_dump produced an empty file"
"${COMPOSE[@]}" exec -T postgres pg_restore --list <"$TMP_DUMP" >/dev/null

DUMP_HASH="$(sha256sum -- "$TMP_DUMP" | awk '{print $1}')"
[[ "$DUMP_HASH" =~ ^[0-9a-f]{64}$ ]] || die "could not calculate SHA-256 checksum"
printf '%s  %s\n' "$DUMP_HASH" "$FINAL_NAME" >"$TMP_CHECKSUM"

mv -- "$TMP_DUMP" "$FINAL_DUMP"
mv -- "$TMP_CHECKSUM" "$FINAL_CHECKSUM"
trap - EXIT HUP INT TERM

printf 'Backup verified: %s\n' "$FINAL_DUMP"
printf 'Checksum: %s\n' "$FINAL_CHECKSUM"
