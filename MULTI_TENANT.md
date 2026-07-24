# Multi-tenant center and Windows Agent

This phase separates tenant data and delivery coordination from desktop WeChat automation:

- PostgreSQL stores tenants, users, memberships, channel accounts, Agent instances, conversations, messages, persistent AI reply jobs, bot profiles, knowledge bases, delivery jobs, credentials, secrets, and audit events.
- A tenant API key is bound to one user membership and checked against the current RBAC role.
- An Agent token is bound to one tenant, one Agent instance, and one channel account.
- The Windows Agent persists inbound events and delivery results in a local SQLite spool before reporting them to the center.
- Outbound work follows `claimed -> sending -> completed/failed/uncertain`. A `sending` job is never automatically reclaimed.

## Start PostgreSQL

Create a local `.env.center` from `.env.center.example`, replace every placeholder, then run:

```powershell
docker compose --env-file .env.center -f docker-compose.center.yml up -d
python main.py --env .env.center center init-db
python main.py --env .env.center center doctor
```

Generate the two required server secrets with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Bootstrap a tenant

```powershell
python main.py --env .env.center center bootstrap `
  --tenant-slug tenant-a `
  --tenant-name "Tenant A" `
  --owner-email owner@example.com `
  --wechat-account-id wechat-a `
  --agent-external-id windows-a `
  --agent-name "Windows Agent A"
```

The command prints the tenant and Agent IDs plus two plaintext credentials exactly once. Put the Agent values on only the matching Windows machine:

```dotenv
AGENT_CENTER_URL=https://center.example.com
AGENT_TENANT_ID=<tenant_id>
AGENT_INSTANCE_ID=<agent_id>
AGENT_CHANNEL_ACCOUNT_ID=<channel_account_id>
AGENT_TOKEN=<agent_token>
AGENT_SPOOL_FILE=data/agent-spool.sqlite3
```

Automatic replies fail closed until the tenant creates a default bot profile. Configure the AI credential and profile through HTTPS with the printed tenant API key:

```powershell
$headers = @{
  Authorization = "Bearer <tenant_api_key>"
  "X-Tenant-ID" = "<tenant_id>"
}
Invoke-RestMethod -Method Put -Uri "https://center.example.com/v1/ai-credentials/primary" `
  -Headers $headers -ContentType "application/json" `
  -Body '{"value":"<tenant-ai-api-key>"}'
Invoke-RestMethod -Method Put -Uri "https://center.example.com/v1/bot-profile" `
  -Headers $headers -ContentType "application/json" `
  -Body '{"name":"default","prompt":"Reply as our customer service team.","settings":{"enabled":true,"llm_provider":"openai_compatible","llm_model":"<model>","openai_base_url":"https://api.example.com/v1","api_credential_name":"primary"}}'
Invoke-RestMethod -Method Put -Uri "https://center.example.com/v1/knowledge-base" `
  -Headers $headers -ContentType "application/json" `
  -Body '{"name":"default","status":"active","content":"# Products\nTenant-specific product and support facts."}'
```

The center encrypts the AI credential with `CENTER_SECRET_KEY`; API responses never return its plaintext or ciphertext.

## Run

Run the center behind an HTTPS reverse proxy. The built-in server listens on HTTP and should normally stay on `127.0.0.1`:

```powershell
python main.py --env .env.center center serve
python main.py --env .env connector run-center --channel wechat_desktop
```

For a production center process, use Waitress behind the HTTPS reverse proxy:

```powershell
python main.py --env .env.center center serve --production --threads 8
```

Open `https://<center-domain>/admin` and sign in with the bootstrap `tenant_id`
and tenant API key. The key is retained only in that browser tab's session storage.

On the matching Windows WeChat machine, install the multi-tenant Agent as a
hidden current-user logon task:

```powershell
python main.py --env .env agent install --mode center
python main.py --env .env agent status
```

Each desktop WeChat account must use a separate Windows Agent process and preferably a separate Windows user session or virtual machine.

## WeChat operations workflow

The tenant console at `/admin` is the control plane for contacts, friend
requests, and operational limits. New or unconfigured contacts are `manual` by
default. Only a contact with an explicit `ai_auto` policy creates center AI
reply work.

The Windows Agent performs these steps:

1. Verify the installed WeChat version and all required connector capabilities.
2. Traverse the contact list through UI Automation scrolling, scan pending
   friend requests, then sync typed snapshots. Contact snapshots report
   `full_scroll_pattern`, `partial_scroll_pattern`, or
   `visible_automation_tree`; partial snapshots are merged and never treated as
   proof that an absent contact was deleted.
3. Persist a claimed operation in the local SQLite spool before execution.
4. Start exactly one center operation and serialize it with message sends.
5. Re-scan the exact friend-request fingerprint before clicking.
6. Complete a unique matching secondary confirmation when WeChat shows one.
7. Persist and report `succeeded`, `failed`, or `uncertain` evidence.

`Approve all` only queues the current pending requests. The Agent still obeys
the configured batch size, minimum interval, hourly limit, daily limit, and
pause flag. An operation interrupted after start becomes `uncertain`; it is
never clicked again automatically. Resolve it in the operation-job view after
checking the actual WeChat client.

Before enabling a customer account, run:

```powershell
python main.py --env .env connector doctor
python main.py --env .env agent status
```

`connector doctor` must report `safe_to_run: true`. Treat a new WeChat release
as unsupported until it passes the real Windows acceptance checklist and its
version prefix is explicitly added to `WECHAT_VERIFIED_VERSION_PREFIXES`.

## Verification

Run the complete unit and local protocol suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Run the PostgreSQL integration test against a disposable database:

```powershell
$env:CENTER_TEST_DATABASE_URL="postgresql+psycopg://user:password@127.0.0.1:5432/wechat_ai_test"
python -m unittest tests.test_tenant_store_postgres -v
```

These tests verify tenant isolation, authentication, encrypted tenant AI credentials, persistent AI generation, idempotency, PostgreSQL locking, the actual HTTP handler, local Agent persistence, and delivery state transitions. They do not prove compatibility with an installed WeChat client; that remains a separate Windows/WeChat acceptance test.
