# Production deployment template

This directory contains a Linux Docker Compose template for PostgreSQL and the
center API. In the integrated product deployment, AI-cs Nginx is the only
public ingress and proxies the center domain over the shared
`wechat-platform-edge` Docker network. Caddy remains available only through
the `standalone-edge` profile for a center-only host. This template does not
prove that a real cloud environment has passed acceptance testing.

## Scope and prerequisites

- A supported 64-bit Linux host with current Docker Engine and Docker Compose v2.
- Public DNS A/AAAA records for the AI-cs website, administration, and center
  domains pointing to the same host.
- Inbound TCP 80 and TCP/UDP 443 allowed. Do not expose 5432 or 8790.
- An external backup destination with sufficient free space.
- The Windows WeChat Agent remains on its Windows machine and connects to this host over HTTPS.

Run all commands from the project root. Use the same unprivileged deployment account for routine operations.

## First deployment

1. Create the production environment file and restrict its permissions:

   ```bash
   cp .env.production.example .env.production
   chmod 600 .env.production
   ```

2. Replace every `CHANGE_ME` value. Generate a URL-safe PostgreSQL password and the center secrets:

   ```bash
   openssl rand -hex 32
   openssl rand -hex 32
   openssl rand -hex 32
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Use the first hex value as `POSTGRES_PASSWORD`, the second as `CENTER_API_KEY_PEPPER`, the third as `CENTER_PROVISIONING_TOKEN`, and the Fernet output as `CENTER_SECRET_KEY`. Configure the same third value as `WECHAT_PROVISIONING_TOKEN` only in the AI-cs backend. Do not commit `.env.production`, paste it into tickets, or put it in shell history.

3. Create the private cross-project service-discovery network and validate the
   resolved Compose model:

   ```bash
   docker network inspect wechat-platform-edge >/dev/null 2>&1 || \
     docker network create wechat-platform-edge
   docker compose --env-file .env.production -f docker-compose.production.yml config --quiet
   ```

4. Build the center image and start PostgreSQL and the center API. The one-shot `migrate` service
   upgrades PostgreSQL to Alembic head before the center API can start:

   ```bash
   docker compose --env-file .env.production -f docker-compose.production.yml build --pull center migrate
   docker compose --env-file .env.production -f docker-compose.production.yml up -d --wait
   docker compose --env-file .env.production -f docker-compose.production.yml run --rm center --env .env.production center doctor
   ```

5. Configure AI-cs `CENTER_DOMAIN` with the public center domain and
   `WECHAT_CENTER_URL=http://wechat-center-api:8790`, then deploy AI-cs. Its
   Nginx container terminates public TLS for all three domains. Confirm the
   database revision and runtime readiness:

   ```bash
   docker compose --env-file .env.production -f docker-compose.production.yml run --rm center --env .env.production center db-status
   curl --fail --show-error --silent https://your-center-domain/readyz
   ```

The integrated deployment must not start the center Caddy service because
AI-cs Nginx owns ports 80/443. For a center-only host, create the shared network
and start with `--profile standalone-edge`; Caddy then owns ports 80/443 and
uses `CENTER_DOMAIN`/`ACME_EMAIL`.

## AI-cs automatic tenant provisioning

When AI-cs is the customer entry point, do not ask the customer to bootstrap a second account or enter a center tenant ID/API key. On the first authenticated visit to the personal WeChat page, the AI-cs backend calls `POST /v1/provisioning/personal-wechat` with its internal tenant identity. The center atomically and idempotently creates the matching center tenant, owner membership, pending WeChat channel, Agent instance, and tenant API credential. Only the AI-cs backend receives the credential and stores it encrypted.

`CENTER_PROVISIONING_TOKEN` authenticates this server-to-server call. It does not determine the resource IDs or tenant API credential, so it can be rotated together with AI-cs `WECHAT_PROVISIONING_TOKEN`. Rotate the center API key pepper with the key-ring procedure below; changing it without the transition window invalidates existing tenant keys, Agent tokens, pending activation codes, and stored WeChat identity hashes.

## Center API key pepper rotation

`CENTER_API_KEY_PEPPER` is the current credential root. Generate a new random
secret of at least 32 bytes and keep the former current value temporarily in
`CENTER_API_KEY_PREVIOUS_PEPPERS`. The previous list is comma-separated,
newest first, and supports at most four unique values.

1. Back up both systems with `deploy/coordinated_backup.sh`.
2. Set the new value as `CENTER_API_KEY_PEPPER` and move the old value to the
   first position in `CENTER_API_KEY_PREVIOUS_PEPPERS`.
3. Deploy all center replicas with the same key ring. Do not run mixed key
   rings behind the load balancer.
4. Run `center doctor`, require `/readyz` to return HTTP 200, and verify one
   existing tenant API key, one existing Agent token, and one pending activation
   code. Newly issued credentials are always signed with the current pepper.
5. Keep the transition window until all Agent installations have refreshed
   their credentials and every activation code issued under the former pepper
   has been consumed or expired.
6. Remove only the retired pepper, deploy all replicas together, and confirm
   that current credentials still work while a credential signed only by the
   retired pepper is rejected.

Never print either environment variable, put it in a Compose command line, or
commit it to the repository. Store the current and previous values in the
deployment secret manager. If the transition check fails, restore the previous
complete key ring; do not issue replacement credentials ad hoc.

## Manual tenant bootstrap

Use this only for standalone center administration or controlled maintenance, not for the normal AI-cs customer flow. Run bootstrap once for each manual tenant/channel binding:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml run --rm center \
  --env .env.production center bootstrap \
  --tenant-slug tenant-a \
  --tenant-name "Tenant A" \
  --owner-email owner@example.com \
  --wechat-account-id wechat-a \
  --agent-external-id agent-a \
  --agent-name "Windows Agent A"
```

The command prints the tenant API key and Agent token once. Store them in an approved secrets manager. Configure the matching Windows Agent with the returned IDs/token and `AGENT_CENTER_URL=https://your-center-domain`.

## Acceptance checks

```bash
docker compose --env-file .env.production -f docker-compose.production.yml ps
docker compose --env-file .env.production -f docker-compose.production.yml exec -T center \
  python main.py --env .env.production center doctor
curl --fail --show-error --silent https://your-center-domain/readyz
```

Expected readiness response is HTTP 200 with database, migration, and reply
Worker checks healthy. `/livez` only proves that the process is alive. Also
verify on the host that only AI-cs Nginx publishes 80/443 and
that PostgreSQL has no published port:

```bash
docker compose --env-file .env.production -f docker-compose.production.yml port postgres 5432
ss -lntup
```

Complete a separate real acceptance test with the intended Windows WeChat Agent, the real AI provider, disconnect/reconnect, and an observed reply. Unit/static tests do not replace this test.

The tenant management console is available at `https://your-center-domain/admin`.
Sign in with the `tenant_id` and tenant API key printed by `center bootstrap`.
The console manages the bot profile, knowledge base, AI credential, messages,
reply jobs, deliveries, contact reply policies, friend-request queues,
rate-limited WeChat operation jobs, uncertain result resolution, and audit
records. Contacts without an explicit `ai_auto` policy remain manual.

On each matching Windows WeChat machine, install the center-connected Agent in
the signed-in desktop user's interactive session:

```powershell
python main.py --env .env agent install --mode center
python main.py --env .env agent status
python main.py --env .env connector doctor
```

Do not enable the scheduled Agent until `connector doctor` reports
`safe_to_run: true`. A missing capability or unverified WeChat version is a
deployment failure, not a warning to bypass.

For final Windows acceptance, use dedicated test contacts to verify one exact
accept, one exact reject, secondary confirmation handling, one welcome message,
policy-controlled AI reply, pause/resume limits, center network interruption,
and Agent restart during an executing operation. Confirm that interrupted work
appears as `uncertain` and is not repeated automatically.

## Coordinated backup before upgrade

AI-cs MySQL/uploads and the WeChat center PostgreSQL database form one logical
customer data set. Do not use the two standalone backup scripts as an upgrade
gate: their snapshots can represent different points in time.

Run the coordinator from the center project. `AI_CS_PROJECT_DIR` must point to
the AI-cs checkout on the same Docker host:

```bash
mkdir -p /srv/wechat-platform-backups
chmod 700 /srv/wechat-platform-backups
AI_CS_PROJECT_DIR=/opt/ai-customer-service \
  bash deploy/coordinated_backup.sh /srv/wechat-platform-backups
```

The coordinator records which write-facing services were running, stops AI-cs
`nginx/api/knowledge-worker` and center `caddy/center`, confirms that all five
are stopped, and
then calls the existing `AI-cs/deploy/backup_ai_cs.sh` and
`deploy/backup_postgres.sh` scripts. It never falls back to a live,
non-coordinated snapshot. Every exit path attempts to restore the original
service state, and a child-backup or service-restore failure prevents the batch
from being published.

A successful run atomically publishes one
`coordinated-YYYYMMDDTHHMMSSZ-<random>` directory. Its `manifest.json` contains
the batch ID, UTC consistency-window timestamps, both relative child paths and
SHA-256 values, immutable API/center image IDs, Compose file hashes, and both
database migration revisions. Hidden `.partial` directories are failed work,
not backups, and must never be copied or restored.

Verify the complete set before copying it off-host, before every upgrade, and
again immediately before restore:

```bash
bash deploy/verify_backup_set.sh \
  /srv/wechat-platform-backups/coordinated-YYYYMMDDTHHMMSSZ-xxxxxxxxxxxx
```

Verification rejects malformed or duplicate-key JSON, batch/path mismatches,
path traversal, symbolic links, extra/missing files, checksum failures, and
partial sets. It also runs `gzip`/`tar` archive checks for AI-cs and
`pg_restore --list` through the center PostgreSQL container. A backup is valid
only when this command exits zero.

Copy the entire coordinated directory to external storage. Never copy or retain
only one child backup.

After a successful coordinated backup, local retention runs automatically. It
defaults to keeping every set from the last 30 days and always protects the
latest 7 sets:

```bash
COORDINATED_BACKUP_RETENTION_DAYS=30 \
COORDINATED_BACKUP_KEEP_LATEST=7 \
  bash deploy/coordinated_backup.sh /srv/wechat-platform-backups
```

Only completed sets that pass full verification again are deleted. Unknown
directories, partial batches, symbolic links, and failed verification sets are
never deleted automatically. Apply a separate lifecycle policy to the off-host
copy, and alert on backup command failure or low disk space.

### Unattended backup timer

After both projects are deployed on the same Linux host, install the root-owned
systemd timer. Use production paths without whitespace:

```bash
sudo \
  BACKUP_ON_CALENDAR='*-*-* 03:00:00 UTC' \
  BACKUP_RANDOMIZED_DELAY_SECONDS=1800 \
  COORDINATED_BACKUP_RETENTION_DAYS=30 \
  COORDINATED_BACKUP_KEEP_LATEST=7 \
  MAX_BACKUP_AGE_HOURS=36 \
  BACKUP_FAILURE_WEBHOOK_URL='https://alerts.example.com/wechat-backup' \
  bash deploy/install_backup_timer.sh \
    /opt/wechat-center \
    /opt/ai-customer-service \
    /srv/wechat-platform-backups
```

`BACKUP_FAILURE_WEBHOOK_URL` is optional and must use HTTPS. The installer stores
configuration as root-only `/etc/wechat-platform/backup.json`, enables
`wechat-platform-backup.timer` plus a six-hour freshness monitor, and writes
durable status including the exact latest backup set to
`/var/lib/wechat-platform/backup-health.json`. The monitor fails and sends the
optional webhook when no backup has ever succeeded, the latest set is missing,
or the last success is older than `MAX_BACKUP_AGE_HOURS`.

Run the first backup during a maintenance window and verify all three views:

```bash
sudo systemctl start wechat-platform-backup.service
sudo systemctl status wechat-platform-backup.service
sudo journalctl -u wechat-platform-backup.service --since today
sudo cat /var/lib/wechat-platform/backup-health.json
```

The service returns nonzero when backup, verification, retention, or health
persistence fails. A kernel `flock` rejects overlapping runs before any Docker
service is touched and releases automatically when the process exits or the
host restarts.

## Standalone center backup

The standalone script remains useful for center-only diagnostics, but it is
not a valid full-platform upgrade or disaster-recovery checkpoint:

The backup script requires an absolute destination, creates a PostgreSQL custom-format dump, verifies its catalog, and writes a SHA-256 companion file:

```bash
mkdir -p /srv/wechat-ai-backups
chmod 700 /srv/wechat-ai-backups
bash deploy/backup_postgres.sh /srv/wechat-ai-backups
```

Copy both `.dump` and `.dump.sha256` to storage outside this host. Test restoration periodically on a separate environment.

Before a center-only development upgrade:

```bash
bash deploy/backup_postgres.sh /srv/wechat-ai-backups
docker compose --env-file .env.production -f docker-compose.production.yml build --pull center
docker compose --env-file .env.production -f docker-compose.production.yml run --rm migrate
docker compose --env-file .env.production -f docker-compose.production.yml up -d --wait --remove-orphans
docker compose --env-file .env.production -f docker-compose.production.yml run --rm center --env .env.production center db-status
```

`center init-db` now upgrades through the versioned Alembic chain. Unknown or
structurally drifted legacy schemas fail closed instead of being stamped. Run
the migration against a recent production backup copy before each release that
changes schema; DDL may briefly lock tables.

## Coordinated restore

Restoration is an all-or-nothing operational procedure. **Never restore only
AI-cs or only the center from a coordinated batch**, even when one side appears
healthy. Their tenant, channel, Agent, credential-binding, message, and delivery
records must return to the same recovery batch.

Use the coordinated restore entry point from the center project:

```bash
AI_CS_PROJECT_DIR=/srv/AI-cs \
  bash deploy/coordinated_restore.sh \
  /srv/wechat-platform-backups/coordinated-YYYYMMDDTHHMMSSZ-xxxxxxxxxxxx \
  --confirm-replace-both-systems
```

The script verifies the complete backup set, blocks both public ingresses,
restores center PostgreSQL and then AI-cs, checks both migration states,
reconciles the knowledge store, and compares every active AI-cs personal WeChat
binding with the restored center channel table. AI-cs Nginx is restarted only
after all checks pass. A lock prevents concurrent coordinated restores.

If any destructive step or post-restore check fails, both public ingresses
remain stopped. Repair the failure or restart the procedure with another
complete coordinated batch; never manually expose the successfully restored
half. Keep an independent maintenance page outside these Compose projects when
the service must show a recovery notice.

## Standalone center restore

Restoration replaces the configured center database. Take a fresh backup first when the current database is still readable. Then pass the absolute dump path and the explicit confirmation flag:

```bash
bash deploy/restore_postgres.sh \
  /srv/wechat-ai-backups/wechat-ai-postgres-YYYYMMDDTHHMMSSZ.dump \
  --confirm-replace-database
```

The script verifies the SHA-256 file and dump catalog before stopping the center. It recreates the database, restores with `--exit-on-error`, verifies required tables, restarts the services, and runs `center doctor`. If a destructive restore step fails, the center remains stopped; inspect the error and restore a known-good backup before restarting it.

## Operations

```bash
docker compose --env-file .env.production -f docker-compose.production.yml logs --tail=200 center caddy postgres
docker compose --env-file .env.production -f docker-compose.production.yml restart center
docker compose --env-file .env.production -f docker-compose.production.yml down
```

Do not use `down --volumes` in production. The named PostgreSQL and Caddy volumes survive ordinary `down`, but they are not backups.
