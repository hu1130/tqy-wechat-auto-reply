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

[[ "$(id -u)" -eq 0 ]] || die "run this installer as root"
[[ $# -eq 3 ]] ||
  die "usage: $0 /absolute/center-project /absolute/AI-cs-project /absolute/backup-root"

for command in bash cat chmod docker flock id install mkdir mktemp python3 realpath rm systemctl systemd-analyze; do
  require_command "$command"
done

for value in "$@"; do
  [[ "$value" = /* ]] || die "all project and backup paths must be absolute"
  [[ "$value" != *[$'\r\n\t ']* ]] ||
    die "systemd-managed paths must not contain whitespace"
  [[ ! -L "$value" ]] || die "project and backup paths must not be symbolic links"
done

CENTER_PROJECT="$(realpath -e -- "$1")"
AI_CS_PROJECT="$(realpath -e -- "$2")"
mkdir -p -- "$3"
BACKUP_ROOT="$(realpath -e -- "$3")"
[[ "$BACKUP_ROOT" != "/" ]] || die "backup root must not be the filesystem root"

RUNNER="${CENTER_PROJECT}/deploy/scheduled_backup.py"
BACKUP_SCRIPT="${CENTER_PROJECT}/deploy/coordinated_backup.sh"
[[ -f "$RUNNER" && -r "$RUNNER" && ! -L "$RUNNER" ]] ||
  die "scheduled backup runner is missing or unsafe: $RUNNER"
[[ -f "$BACKUP_SCRIPT" && -r "$BACKUP_SCRIPT" && ! -L "$BACKUP_SCRIPT" ]] ||
  die "coordinated backup script is missing or unsafe: $BACKUP_SCRIPT"
[[ -f "${AI_CS_PROJECT}/docker-compose.yml" && -f "${AI_CS_PROJECT}/.env" ]] ||
  die "AI-cs production Compose or environment file is missing"
[[ -f "${CENTER_PROJECT}/docker-compose.production.yml" &&
   -f "${CENTER_PROJECT}/.env.production" ]] ||
  die "center production Compose or environment file is missing"

ON_CALENDAR="${BACKUP_ON_CALENDAR:-*-*-* 03:00:00 UTC}"
RANDOMIZED_DELAY="${BACKUP_RANDOMIZED_DELAY_SECONDS:-1800}"
RETENTION_DAYS="${COORDINATED_BACKUP_RETENTION_DAYS:-30}"
KEEP_LATEST="${COORDINATED_BACKUP_KEEP_LATEST:-7}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-36}"
[[ "$RANDOMIZED_DELAY" =~ ^[0-9]+$ && "$RANDOMIZED_DELAY" -le 86400 ]] ||
  die "BACKUP_RANDOMIZED_DELAY_SECONDS must be between 0 and 86400"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ &&
   "$RETENTION_DAYS" -ge 1 && "$RETENTION_DAYS" -le 3650 ]] ||
  die "COORDINATED_BACKUP_RETENTION_DAYS must be between 1 and 3650"
[[ "$KEEP_LATEST" =~ ^[0-9]+$ &&
   "$KEEP_LATEST" -ge 2 && "$KEEP_LATEST" -le 365 ]] ||
  die "COORDINATED_BACKUP_KEEP_LATEST must be between 2 and 365"
[[ "$MAX_BACKUP_AGE_HOURS" =~ ^[0-9]+$ &&
   "$MAX_BACKUP_AGE_HOURS" -ge 1 && "$MAX_BACKUP_AGE_HOURS" -le 720 ]] ||
  die "MAX_BACKUP_AGE_HOURS must be between 1 and 720"
systemd-analyze calendar "$ON_CALENDAR" >/dev/null

CONFIG_DIR="/etc/wechat-platform"
STATE_DIR="/var/lib/wechat-platform"
CONFIG_PATH="${CONFIG_DIR}/backup.json"
HEALTH_PATH="${STATE_DIR}/backup-health.json"
SERVICE_PATH="/etc/systemd/system/wechat-platform-backup.service"
TIMER_PATH="/etc/systemd/system/wechat-platform-backup.timer"
MONITOR_SERVICE_PATH="/etc/systemd/system/wechat-platform-backup-monitor.service"
MONITOR_TIMER_PATH="/etc/systemd/system/wechat-platform-backup-monitor.timer"
PYTHON_BIN="$(realpath -e -- "$(command -v python3)")"
BASH_BIN="$(realpath -e -- "$(command -v bash)")"

install -d -m 0700 "$CONFIG_DIR" "$STATE_DIR" "$BACKUP_ROOT"

python3 - \
  "$CONFIG_PATH" \
  "$CENTER_PROJECT" \
  "$AI_CS_PROJECT" \
  "$BACKUP_ROOT" \
  "$HEALTH_PATH" \
  "$BASH_BIN" \
  "$RETENTION_DAYS" \
  "$KEEP_LATEST" \
  "$MAX_BACKUP_AGE_HOURS" <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

(
    config_path,
    center_project,
    ai_cs_project,
    backup_root,
    health_path,
    bash_path,
    retention_days,
    keep_latest,
    max_backup_age_hours,
) = sys.argv[1:]
webhook = os.environ.get("BACKUP_FAILURE_WEBHOOK_URL", "").strip()
if webhook:
    parsed = urlsplit(webhook)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SystemExit(
            "BACKUP_FAILURE_WEBHOOK_URL must be HTTPS without credentials or fragment"
        )
payload = {
    "schema_version": 1,
    "center_project_dir": center_project,
    "ai_cs_project_dir": ai_cs_project,
    "backup_root": backup_root,
    "health_file": health_path,
    "bash_path": bash_path,
    "retention_days": int(retention_days),
    "keep_latest": int(keep_latest),
    "max_backup_age_hours": int(max_backup_age_hours),
    "failure_webhook_url": webhook,
}
target = Path(config_path)
with tempfile.NamedTemporaryFile(
    "w",
    encoding="utf-8",
    newline="\n",
    dir=target.parent,
    prefix=".backup.",
    suffix=".tmp",
    delete=False,
) as handle:
    temporary = Path(handle.name)
    json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY

if [[ ! -f "$HEALTH_PATH" ]]; then
  printf '{"schema_version":1,"status":"never_run"}\n' >"$HEALTH_PATH"
  chmod 0600 "$HEALTH_PATH"
fi

TEMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

cat >"${TEMP_DIR}/wechat-platform-backup.service" <<EOF
[Unit]
Description=Coordinated AI-cs and WeChat center backup
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=${CENTER_PROJECT}
ExecStart=${PYTHON_BIN} ${RUNNER} --config ${CONFIG_PATH}
TimeoutStartSec=3h
UMask=0077
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=${BACKUP_ROOT} ${STATE_DIR}
EOF

cat >"${TEMP_DIR}/wechat-platform-backup.timer" <<EOF
[Unit]
Description=Daily coordinated AI-cs and WeChat center backup

[Timer]
OnCalendar=${ON_CALENDAR}
Persistent=true
RandomizedDelaySec=${RANDOMIZED_DELAY}
Unit=wechat-platform-backup.service

[Install]
WantedBy=timers.target
EOF

cat >"${TEMP_DIR}/wechat-platform-backup-monitor.service" <<EOF
[Unit]
Description=Verify coordinated platform backup freshness
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
ExecStart=${PYTHON_BIN} ${RUNNER} --config ${CONFIG_PATH} --check-health
TimeoutStartSec=2m
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
EOF

cat >"${TEMP_DIR}/wechat-platform-backup-monitor.timer" <<EOF
[Unit]
Description=Check coordinated platform backup freshness every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:30:00 UTC
Persistent=true
RandomizedDelaySec=300
Unit=wechat-platform-backup-monitor.service

[Install]
WantedBy=timers.target
EOF

systemd-analyze verify \
  "${TEMP_DIR}/wechat-platform-backup.service" \
  "${TEMP_DIR}/wechat-platform-backup.timer" \
  "${TEMP_DIR}/wechat-platform-backup-monitor.service" \
  "${TEMP_DIR}/wechat-platform-backup-monitor.timer"
install -m 0644 "${TEMP_DIR}/wechat-platform-backup.service" "$SERVICE_PATH"
install -m 0644 "${TEMP_DIR}/wechat-platform-backup.timer" "$TIMER_PATH"
install -m 0644 \
  "${TEMP_DIR}/wechat-platform-backup-monitor.service" "$MONITOR_SERVICE_PATH"
install -m 0644 \
  "${TEMP_DIR}/wechat-platform-backup-monitor.timer" "$MONITOR_TIMER_PATH"
systemctl daemon-reload
systemctl enable --now \
  wechat-platform-backup.timer \
  wechat-platform-backup-monitor.timer

printf 'Installed timers: wechat-platform-backup.timer, wechat-platform-backup-monitor.timer\n'
printf 'Schedule: %s, randomized delay: %ss\n' "$ON_CALENDAR" "$RANDOMIZED_DELAY"
printf 'Health file: %s\n' "$HEALTH_PATH"
printf 'Run and verify the first backup with:\n'
printf '  systemctl start wechat-platform-backup.service\n'
printf '  systemctl status wechat-platform-backup.service\n'
