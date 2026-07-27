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

CONFIRMATION="--confirm-replace-database"
[[ $# -eq 2 && "$2" == "$CONFIRMATION" ]] || die "usage: $0 /absolute/backup.dump $CONFIRMATION"
[[ "$1" = /* ]] || die "backup file must be an absolute path"
[[ ! -L "$1" ]] || die "backup file must not be a symbolic link"

require_command docker
require_command realpath
require_command sha256sum

BACKUP_FILE="$(realpath -e -- "$1")"
CHECKSUM_FILE="${BACKUP_FILE}.sha256"
[[ -f "$BACKUP_FILE" && -r "$BACKUP_FILE" ]] || die "backup file is not readable: $BACKUP_FILE"
[[ -f "$CHECKSUM_FILE" && -r "$CHECKSUM_FILE" ]] || die "checksum file is not readable: $CHECKSUM_FILE"
[[ ! -L "$CHECKSUM_FILE" ]] || die "checksum file must not be a symbolic link"

EXPECTED_HASH="$(awk 'NR == 1 {print $1}' "$CHECKSUM_FILE")"
[[ "$EXPECTED_HASH" =~ ^[0-9a-f]{64}$ ]] || die "checksum file does not contain a valid SHA-256 hash"
ACTUAL_HASH="$(sha256sum -- "$BACKUP_FILE" | awk '{print $1}')"
[[ "$ACTUAL_HASH" == "$EXPECTED_HASH" ]] || die "backup checksum verification failed"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(realpath -- "${SCRIPT_DIR}/..")"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.production.yml}"

[[ -f "$ENV_FILE" ]] || die "environment file not found: $ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"
export CENTER_ENV_FILE="$ENV_FILE"

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
RESTORE_START_INGRESS="${RESTORE_START_INGRESS:-1}"
[[ "$RESTORE_START_INGRESS" == "0" || "$RESTORE_START_INGRESS" == "1" ]] ||
  die "RESTORE_START_INGRESS must be 0 or 1"
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" exec -T postgres sh -eu -c '
  case "$POSTGRES_DB" in
    ""|postgres|template0|template1)
      printf "unsafe POSTGRES_DB: %s\n" "$POSTGRES_DB" >&2
      exit 64
      ;;
  esac
'
"${COMPOSE[@]}" exec -T postgres pg_restore --list <"$BACKUP_FILE" >/dev/null

CENTER_STOPPED=0
on_error() {
  if [[ "$CENTER_STOPPED" -eq 1 ]]; then
    printf 'ERROR: restore failed; center remains stopped for operator inspection.\n' >&2
  fi
}
trap on_error ERR

"${COMPOSE[@]}" stop center
CENTER_STOPPED=1

"${COMPOSE[@]}" exec -T postgres sh -eu -c '
  dropdb \
    --username "$POSTGRES_USER" \
    --maintenance-db postgres \
    --if-exists \
    --force \
    "$POSTGRES_DB"
  PGDATABASE=postgres createdb \
    --username "$POSTGRES_USER" \
    "$POSTGRES_DB"
'

"${COMPOSE[@]}" exec -T postgres sh -eu -c '
  exec pg_restore \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --exit-on-error \
    --no-owner \
    --no-acl
' <"$BACKUP_FILE"

SCHEMA_OK="$("${COMPOSE[@]}" exec -T postgres sh -eu -c '
  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --command "SELECT to_regclass('"'"'public.tenants'"'"') IS NOT NULL AND to_regclass('"'"'public.messages'"'"') IS NOT NULL"
')"
[[ "$SCHEMA_OK" == "t" ]] || die "restored database is missing required center tables"

if [[ "$RESTORE_START_INGRESS" == "1" ]]; then
  "${COMPOSE[@]}" --profile standalone-edge up -d --wait center caddy
else
  "${COMPOSE[@]}" up -d --wait center
fi
"${COMPOSE[@]}" exec -T center python main.py --env .env.production center doctor

CENTER_STOPPED=0
trap - ERR
printf 'Restore verified and center restarted from: %s\n' "$BACKUP_FILE"
