from __future__ import annotations

from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from wechat_ai.config import ALLOWED_CHAT_POLICIES, AppConfig
from wechat_ai.store import SQLiteStore


@dataclass(frozen=True)
class DashboardUser:
    username: str
    password: str
    role: str


def run_dashboard_server(config: AppConfig, host: str = "127.0.0.1", port: int = 8787) -> None:
    if not config.state_db_file:
        raise RuntimeError("STATE_DB_FILE is empty. Set it before starting the dashboard.")
    store = SQLiteStore(config.state_db_file)
    users = _parse_users(config.dashboard_users)
    sessions: dict[str, DashboardUser] = {}
    handler_class = _build_handler(config, store, users, sessions)
    server = ThreadingHTTPServer((host, port), handler_class)
    print(f"Dashboard: http://{host}:{port}", flush=True)
    server.serve_forever()


def _parse_users(raw_users: str) -> dict[str, DashboardUser]:
    users: dict[str, DashboardUser] = {}
    for item in (part.strip() for part in raw_users.split(",") if part.strip()):
        parts = item.split(":")
        if len(parts) != 3:
            continue
        username, password, role = (part.strip() for part in parts)
        if username and password and role in {"admin", "agent", "viewer"}:
            users[username] = DashboardUser(username=username, password=password, role=role)
    if not users:
        users["admin"] = DashboardUser(username="admin", password="admin123", role="admin")
    return users


def _build_handler(
    config: AppConfig,
    store: SQLiteStore,
    users: dict[str, DashboardUser],
    sessions: dict[str, DashboardUser],
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/login":
                self._send_html(_login_html())
                return
            if path == "/logout":
                self._clear_session()
                self._redirect("/login")
                return
            user = self._current_user()
            if not user:
                if path.startswith("/api/"):
                    self._send_json({"ok": False, "error": "unauthorized"}, 401)
                    return
                self._redirect("/login")
                return
            if path == "/":
                self._send_html(_dashboard_html(user))
                return
            if path == "/api/overview":
                self._send_json(
                    {
                        "user": {"username": user.username, "role": user.role},
                        "config": {
                            "default_chat_policy": config.default_chat_policy,
                            "group_chats_enabled": config.group_chats_enabled,
                            "message_buffer_seconds": config.message_buffer_seconds,
                            "voice_to_text_timeout_seconds": config.voice_to_text_timeout_seconds,
                        },
                        "customers": store.list_customers(limit=300),
                        "events": store.list_recent_events(limit=120),
                    }
                )
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/login":
                self._handle_login()
                return
            user = self._current_user()
            if not user:
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            if path == "/api/customers":
                if not self._require_role(user, {"admin", "agent"}):
                    return
                self._handle_customer_upsert()
                return
            if path.startswith("/api/customers/"):
                if not self._require_role(user, {"admin", "agent"}):
                    return
                chat_name = unquote(path.removeprefix("/api/customers/"))
                self._handle_customer_upsert(chat_name=chat_name)
                return
            if path.startswith("/api/chats/"):
                if not self._require_role(user, {"admin", "agent"}):
                    return
                self._handle_chat_action(path)
                return
            self.send_error(404, "Not found")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle_login(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
            form = parse_qs(raw)
            username = (form.get("username") or [""])[0].strip()
            password = (form.get("password") or [""])[0].strip()
            user = users.get(username)
            if not user or not secrets.compare_digest(user.password, password):
                self._send_html(_login_html(error="Invalid username or password"), status=401)
                return
            token = secrets.token_urlsafe(32)
            sessions[token] = user
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"dashboard_session={token}; HttpOnly; SameSite=Lax; Path=/")
            self.end_headers()

        def _handle_customer_upsert(self, chat_name: str = "") -> None:
            payload = self._read_json()
            effective_chat_name = (chat_name or str(payload.get("chat_name") or "")).strip()
            if not effective_chat_name:
                self._send_json({"ok": False, "error": "chat_name is required"}, 400)
                return
            current = store.get_customer(effective_chat_name)
            store.upsert_customer(
                chat_name=effective_chat_name,
                display_name=str(payload.get("display_name", current.display_name)),
                phone=str(payload.get("phone", current.phone)),
                source=str(payload.get("source", current.source or "wechat_desktop")),
                tags=str(payload.get("tags", current.tags)),
                lead_level=str(payload.get("lead_level", current.lead_level or "unknown")),
                status=str(payload.get("status", current.status or "new")),
                owner=str(payload.get("owner", current.owner)),
                notes=str(payload.get("notes", current.notes)),
            )
            self._send_json({"ok": True, "customer": store.get_customer(effective_chat_name).__dict__})

        def _handle_chat_action(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[0] != "api" or parts[1] != "chats":
                self.send_error(404, "Not found")
                return
            chat_name = unquote(parts[2])
            action = parts[3]
            payload = self._read_json()
            if action in {"pause", "handoff"}:
                store.pause_chat(chat_name, reason=str(payload.get("reason") or "human_handoff"))
                self._send_json({"ok": True, "chat_name": chat_name, "paused": True})
                return
            if action in {"resume", "takeback"}:
                store.resume_chat(chat_name)
                self._send_json({"ok": True, "chat_name": chat_name, "paused": False})
                return
            if action == "policy":
                policy = str(payload.get("policy") or "").strip().lower()
                if policy not in ALLOWED_CHAT_POLICIES:
                    self._send_json({"ok": False, "error": f"policy must be one of {sorted(ALLOWED_CHAT_POLICIES)}"}, 400)
                    return
                store.set_chat_policy(chat_name, policy)
                self._send_json({"ok": True, "chat_name": chat_name, "policy": policy})
                return
            self.send_error(404, "Not found")

        def _require_role(self, user: DashboardUser, allowed_roles: set[str]) -> bool:
            if user.role not in allowed_roles:
                self._send_json({"ok": False, "error": "permission denied"}, 403)
                return False
            return True

        def _current_user(self) -> DashboardUser | None:
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("dashboard_session")
            if not token:
                return None
            return sessions.get(token.value)

        def _clear_session(self) -> None:
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("dashboard_session")
            if token:
                sessions.pop(token.value, None)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "dashboard_session=; Max-Age=0; Path=/")
            self.end_headers()

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

    return DashboardHandler


def _login_html(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Login</title>
<style>
body{{margin:0;background:#f6f7f9;font-family:Segoe UI,Arial,sans-serif;color:#1f2937;display:grid;place-items:center;min-height:100vh}}
form{{width:340px;background:#fff;border:1px solid #d9dee7;border-radius:8px;padding:22px}}
h1{{font-size:20px;margin:0 0 18px}}label{{display:block;font-size:13px;margin:12px 0 6px;color:#4b5563}}
input{{width:100%;height:36px;border:1px solid #d9dee7;border-radius:6px;padding:0 10px}}
button{{width:100%;height:38px;margin-top:18px;border:0;border-radius:6px;background:#0f766e;color:#fff;font-weight:600;cursor:pointer}}
.error{{background:#fee2e2;color:#991b1b;padding:8px 10px;border-radius:6px;margin-bottom:12px;font-size:13px}}
.hint{{font-size:12px;color:#6b7280;margin-top:12px;line-height:1.5}}
</style></head><body>
<form method="post" action="/login">
  <h1>AI Service Dashboard</h1>
  {error_html}
  <label>Username</label><input name="username" autocomplete="username" autofocus>
  <label>Password</label><input name="password" type="password" autocomplete="current-password">
  <button type="submit">Login</button>
  <div class="hint">Default local account: admin / admin123. Change DASHBOARD_USERS in .env.</div>
</form></body></html>"""


def _dashboard_html(user: DashboardUser) -> str:
    readonly = "true" if user.role == "viewer" else "false"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Service Dashboard</title>
  <style>
    :root {{ --bg:#f6f7f9; --panel:#fff; --line:#d9dee7; --text:#1f2937; --muted:#6b7280; --accent:#0f766e; --danger:#b91c1c; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif; background:var(--bg); color:var(--text); }}
    header {{ height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 20px; border-bottom:1px solid var(--line); background:#fff; }}
    h1 {{ font-size:18px; margin:0; }} main {{ display:grid; grid-template-columns:390px 1fr; min-height:calc(100vh - 56px); }}
    aside {{ background:#fff; border-right:1px solid var(--line); overflow:auto; }} section {{ padding:18px; overflow:auto; }}
    .toolbar {{ display:flex; gap:8px; padding:12px; border-bottom:1px solid var(--line); }} input,select,textarea {{ border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }}
    input {{ height:34px; }} .toolbar input {{ width:100%; }} button {{ height:32px; border:1px solid var(--line); border-radius:6px; background:#fff; padding:0 10px; cursor:pointer; }}
    button.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); }} button.danger {{ color:var(--danger); border-color:#efb4b4; }}
    button:disabled {{ opacity:.45; cursor:not-allowed; }} .customer {{ padding:12px; border-bottom:1px solid #edf0f4; cursor:pointer; }}
    .customer:hover,.customer.active {{ background:#eef7f5; }} .row {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }}
    .title {{ font-size:14px; font-weight:650; }} .meta {{ color:var(--muted); font-size:12px; margin-top:6px; display:flex; gap:8px; flex-wrap:wrap; }}
    .badge {{ border-radius:999px; padding:2px 7px; background:#eef2ff; color:#3730a3; }} .high {{ background:#fee2e2; color:#991b1b; }} .medium {{ background:#fef3c7; color:#92400e; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; margin-bottom:14px; }} .panel h2 {{ margin:0; padding:12px 14px; border-bottom:1px solid var(--line); font-size:15px; }}
    .panel-body {{ padding:14px; }} .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} .grid label {{ display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--muted); }}
    textarea {{ min-height:70px; resize:vertical; }} table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ border-bottom:1px solid #edf0f4; padding:8px; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); background:#fafafa; }} .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }} .empty {{ color:var(--muted); padding:22px; text-align:center; }}
    @media(max-width:900px) {{ main {{ grid-template-columns:1fr; }} aside {{ max-height:42vh; border-right:0; border-bottom:1px solid var(--line); }} }}
  </style>
</head>
<body>
<header><h1>AI Service Dashboard</h1><div class="meta"><span>{user.username} · {user.role}</span><a href="/logout">Logout</a></div></header>
<main>
  <aside><div class="toolbar"><input id="search" placeholder="Search customer/session"><button onclick="newCustomer()" {"disabled" if readonly == "true" else ""}>New</button></div><div id="customers"></div></aside>
  <section>
    <div class="panel"><h2>Customer</h2><div class="panel-body">
      <div class="grid">
        <label>Chat name<input id="chat_name"></label><label>Display name<input id="display_name"></label>
        <label>Phone<input id="phone"></label><label>Source<input id="source"></label>
        <label>Lead level<select id="lead_level"><option>unknown</option><option>low</option><option>medium</option><option>high</option></select></label>
        <label>Status<select id="status_field"><option>new</option><option>contacted</option><option>interested</option><option>handoff</option><option>closed</option><option>invalid</option></select></label>
        <label>Owner<input id="owner"></label><label>Tags<input id="tags"></label>
      </div>
      <label style="display:flex;flex-direction:column;gap:5px;margin-top:10px;font-size:12px;color:#6b7280">Notes<textarea id="notes"></textarea></label>
      <div class="actions">
        <button class="primary" onclick="saveCustomer()" {"disabled" if readonly == "true" else ""}>Save customer</button>
        <button class="danger" onclick="pauseSelected()" {"disabled" if readonly == "true" else ""}>Human handoff</button>
        <button onclick="resumeSelected()" {"disabled" if readonly == "true" else ""}>Resume bot</button>
        <select id="policy"><option value="auto">auto</option><option value="manual">manual</option><option value="dry_run">dry_run</option><option value="ignore">ignore</option></select>
        <button onclick="setPolicy()" {"disabled" if readonly == "true" else ""}>Set policy</button>
      </div>
    </div></div>
    <div class="panel"><h2>Recent events</h2><div class="panel-body" id="events"></div></div>
  </section>
</main>
<script>
const readonly = {readonly};
let data = {{ customers: [], events: [], config: {{}}, user: {{}} }};
let selected = "";
const $ = id => document.getElementById(id);
async function load() {{ const res = await fetch('/api/overview'); if(res.status===401) location.href='/login'; data = await res.json(); render(); }}
function filteredCustomers() {{ const q = $('search').value.trim().toLowerCase(); return data.customers.filter(c => !q || c.chat_name.toLowerCase().includes(q) || (c.display_name||'').toLowerCase().includes(q)); }}
function render() {{
  const items = filteredCustomers();
  $('customers').innerHTML = items.length ? items.map(c => `<div class="customer ${{selected===c.chat_name?'active':''}}" onclick="selectCustomer(${{JSON.stringify(c.chat_name).replaceAll('"','&quot;')}})">
    <div class="row"><span class="title">${{escapeHtml(c.display_name || c.chat_name)}}</span><span class="badge ${{c.lead_level}}">${{escapeHtml(c.lead_level)}}</span></div>
    <div class="meta"><span>${{escapeHtml(c.status)}}</span><span>sent ${{c.sent_count}}</span><span>skip ${{c.skip_count}}</span><span>${{escapeHtml(c.last_event_at||'')}}</span></div>
  </div>`).join('') : '<div class="empty">No customers yet</div>';
  const current = data.customers.find(c => c.chat_name === selected);
  fillForm(current || {{}});
  const events = selected ? data.events.filter(e => e.chat_name === selected) : data.events;
  $('events').innerHTML = events.length ? `<table><thead><tr><th>Time</th><th>Customer</th><th>Action</th><th>Content</th><th>Reply/reason</th></tr></thead><tbody>${{events.map(e => `<tr><td>${{escapeHtml(e.ts)}}</td><td>${{escapeHtml(e.chat_name)}}</td><td>${{escapeHtml(e.action)}}</td><td>${{escapeHtml(e.content_preview)}}</td><td>${{escapeHtml(e.reply_preview || e.reason || '')}}</td></tr>`).join('')}}</tbody></table>` : '<div class="empty">No events</div>';
}}
function fillForm(c) {{ ['chat_name','display_name','phone','source','lead_level','owner','tags','notes'].forEach(id => $(id).value = c[id] || ''); $('status_field').value = c.status || 'new'; $('policy').value = c.policy || data.config.default_chat_policy || 'auto'; }}
function selectCustomer(name) {{ selected = name; render(); }}
function newCustomer() {{ selected = ''; fillForm({{ source:'wechat_desktop', lead_level:'unknown', status:'new' }}); $('chat_name').focus(); }}
function customerPayload() {{ return {{ chat_name:$('chat_name').value, display_name:$('display_name').value, phone:$('phone').value, source:$('source').value, lead_level:$('lead_level').value, status:$('status_field').value, owner:$('owner').value, tags:$('tags').value, notes:$('notes').value }}; }}
async function saveCustomer() {{ const p = customerPayload(); if(!p.chat_name) return alert('chat_name is required'); await fetch('/api/customers', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(p) }}); selected = p.chat_name; await load(); }}
async function chatAction(action, body={{}}) {{ const name = selected || $('chat_name').value; if(!name) return; await fetch(`/api/chats/${{encodeURIComponent(name)}}/${{action}}`, {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body) }}); await load(); }}
function pauseSelected() {{ chatAction('pause', {{ reason:'human_handoff' }}); }}
function resumeSelected() {{ chatAction('resume'); }}
function setPolicy() {{ chatAction('policy', {{ policy:$('policy').value }}); }}
function escapeHtml(v) {{ return String(v ?? '').replace(/[&<>"']/g, s => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[s])); }}
$('search').addEventListener('input', render);
load(); setInterval(load, 2000);
</script></body></html>"""
