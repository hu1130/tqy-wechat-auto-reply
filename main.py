from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from wechat_ai.admin import (
    pause_chat,
    render_pending_replies,
    render_report,
    render_status,
    resume_chat,
    save_daily_report,
    set_chat_policy,
)
from wechat_ai.agent_activation import activate_and_save, resolve_center_url
from wechat_ai.agent_profile import AgentProfileStore, public_profile_status
from wechat_ai.bot import WeChatAutoReplyBot
from wechat_ai.config import load_config
from wechat_ai.center_runtime import (
    bootstrap_tenant,
    build_center_application,
    center_diagnostics,
    center_service_readiness,
)
from wechat_ai.center_server import run_center_production_server, run_center_server
from wechat_ai.center_worker import CenterReplyWorker
from wechat_ai.connector_agent import (
    connector_compatibility,
    connector_status,
    list_connectors,
    run_center_connector_agent,
    run_local_connector_agent,
)
from wechat_ai.doctor import run_doctor
from wechat_ai.friend_requests import accept_friend_requests
from wechat_ai.image_probe import run_image_probe
from wechat_ai.llm import LLMClient
from wechat_ai.single_instance import SingleInstanceLock
from wechat_ai.simulator import StaticReplyClient, simulate_message
from wechat_ai.web_admin import run_dashboard_server
from wechat_ai.windows_agent import (
    DEFAULT_TASK_NAME,
    WindowsAgentTask,
    WindowsTaskScheduler,
    backup_sqlite_database,
)
from wechat_ai.wizard import init_env
from wechat_ai.tenant_auth import ApiKeyManager, SecretCipher
from wechat_ai.tenant_store import TenantStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="tqy-wechat-auto-reply")
    parser.add_argument("--env", default=".env", help="Path to env file, default: .env")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run WeChat auto-reply bot")
    mode_group = run_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Generate replies without sending them")
    mode_group.add_argument("--live", action="store_true", help="Send replies to WeChat, overriding DRY_RUN")
    run_parser.add_argument(
        "--chat",
        action="append",
        dest="chats",
        help="Chat name to listen to. Can be passed multiple times.",
    )

    demo_parser = subparsers.add_parser("demo", help="Test LLM reply generation only")
    demo_parser.add_argument("--chat", default="demo", help="Chat name to use in the prompt")
    demo_parser.add_argument("question", nargs="+", help="Question to send to the LLM")

    simulate_parser = subparsers.add_parser("simulate", help="Run one fake WeChat message through the bot")
    simulate_parser.add_argument("--chat", help="Chat name to simulate, default: first WECHAT_LISTEN_CHATS item")
    simulate_parser.add_argument("--sender", default="simulated-user", help="Sender name to simulate")
    simulate_parser.add_argument("--type", default="text", dest="msg_type", help="wxauto message type to simulate")
    simulate_parser.add_argument("--reply", help="Use this fixed reply instead of calling the LLM endpoint")
    simulate_parser.add_argument(
        "--live-path",
        action="store_true",
        help="Exercise the non-dry-run path with a fake chat object; no real WeChat message is sent",
    )
    simulate_parser.add_argument(
        "--queue",
        action="store_true",
        help="Force approval queue mode and write the generated reply to PENDING_REPLIES_FILE",
    )
    simulate_parser.add_argument("message", nargs="+", help="Incoming message content to simulate")

    subparsers.add_parser("config", help="Print loaded config")

    init_parser = subparsers.add_parser("init", help="Create a friendly .env file")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing env file")
    init_parser.add_argument("--yes", action="store_true", help="Use defaults without prompting")

    doctor_parser = subparsers.add_parser("doctor", help="Check whether the bot can run")
    doctor_parser.add_argument("--skip-llm", action="store_true", help="Do not check the LLM endpoint")

    image_probe_parser = subparsers.add_parser("image-probe", help="Inspect wxauto4 image messages in one chat")
    image_probe_parser.add_argument("--chat", required=True, help="Chat name to watch while you send an image")
    image_probe_parser.add_argument("--seconds", type=int, default=60, help="How long to watch, default: 60 seconds")
    image_probe_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds")
    image_probe_parser.add_argument("--out", default="logs", help="Directory for image probe JSON output")

    friends_parser = subparsers.add_parser("friends", help="Scan or accept visible WeChat friend requests")
    friends_mode = friends_parser.add_mutually_exclusive_group()
    friends_mode.add_argument("--dry-run", action="store_true", help="Only scan friend request accept buttons")
    friends_mode.add_argument("--live", action="store_true", help="Click accept buttons for friend requests")
    friends_parser.add_argument("--limit", type=int, default=10, help="Maximum friend requests to accept")
    friends_parser.add_argument(
        "--no-switch",
        action="store_true",
        help="Do not switch to contacts page first; scan the current WeChat screen",
    )

    admin_parser = subparsers.add_parser("admin", help="Manage runtime state")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command")
    admin_subparsers.add_parser("status", help="Show persisted chat state")
    pending_parser = admin_subparsers.add_parser("pending", help="Show queued replies waiting for manual approval")
    pending_parser.add_argument("--limit", type=int, default=20, help="Number of pending replies to show")

    pause_parser = admin_subparsers.add_parser("pause", help="Pause one chat")
    pause_parser.add_argument("chat", help="Chat name to pause")
    pause_parser.add_argument("--reason", default="manual", help="Pause reason")

    handoff_parser = admin_subparsers.add_parser("handoff", help="Pause one chat for human handoff")
    handoff_parser.add_argument("chat", help="Chat name to hand off to a human")
    handoff_parser.add_argument("--reason", default="human_handoff", help="Handoff reason")

    resume_parser = admin_subparsers.add_parser("resume", help="Resume one chat")
    resume_parser.add_argument("chat", help="Chat name to resume")

    takeback_parser = admin_subparsers.add_parser("takeback", help="Resume bot replies after human handoff")
    takeback_parser.add_argument("chat", help="Chat name to resume")

    policy_parser = admin_subparsers.add_parser("policy", help="Set one chat policy")
    policy_parser.add_argument("chat", help="Chat name")
    policy_parser.add_argument("policy", help="auto, manual, dry_run, or ignore")

    report_parser = admin_subparsers.add_parser("report", help="Show daily audit report")
    report_parser.add_argument("--date", help="Date in YYYY-MM-DD format, default: today")
    report_parser.add_argument("--save", action="store_true", help="Save report to REPORTS_DIR")

    dashboard_parser = subparsers.add_parser("dashboard", help="Run local web dashboard")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Dashboard host")
    dashboard_parser.add_argument("--port", type=int, default=8787, help="Dashboard port")

    connector_parser = subparsers.add_parser("connector", help="Run or inspect desktop connector agent")
    connector_subparsers = connector_parser.add_subparsers(dest="connector_command")
    connector_subparsers.add_parser("list", help="List supported connector channels")
    connector_status_parser = connector_subparsers.add_parser("status", help="Show connector status")
    connector_status_parser.add_argument("--id", default="local-wechat-desktop", help="Connector id")
    connector_status_parser.add_argument("--channel", default="wechat_desktop", help="Connector channel")
    connector_doctor_parser = connector_subparsers.add_parser("doctor", help="Show connector compatibility report")
    connector_doctor_parser.add_argument("--id", default="local-wechat-desktop", help="Connector id")
    connector_doctor_parser.add_argument("--channel", default="wechat_desktop", help="Connector channel")
    connector_run_parser = connector_subparsers.add_parser("run-local", help="Run local connector agent and print events")
    connector_run_parser.add_argument("--id", default="local-wechat-desktop", help="Connector id")
    connector_run_parser.add_argument("--channel", default="wechat_desktop", help="Connector channel")
    connector_run_parser.add_argument("--interval", type=float, default=1.0, help="Polling interval seconds")
    connector_run_parser.add_argument("--echo", action="store_true", help="Echo inbound messages back through the connector")
    connector_center_parser = connector_subparsers.add_parser(
        "run-center",
        help="Run the persistent Windows connector Agent against the center service",
    )
    connector_center_parser.add_argument("--id", default="local-wechat-desktop", help="Connector id")
    connector_center_parser.add_argument("--channel", default="wechat_desktop", help="Connector channel")
    connector_center_parser.add_argument("--interval", type=float, default=1.0, help="Polling interval seconds")

    center_parser = subparsers.add_parser("center", help="Manage the multi-tenant center service")
    center_subparsers = center_parser.add_subparsers(dest="center_command")
    center_subparsers.add_parser("init-db", help="Upgrade the center database to migration head")
    center_subparsers.add_parser("db-status", help="Show current and head database revisions")
    center_subparsers.add_parser("doctor", help="Check center secrets, database, and schema")
    center_serve = center_subparsers.add_parser("serve", help="Run the center HTTP API")
    center_serve.add_argument(
        "--production",
        action="store_true",
        help="Use the production WSGI server instead of the development HTTP server",
    )
    center_serve.add_argument("--threads", type=int, default=8, help="Production server worker threads")
    center_bootstrap = center_subparsers.add_parser(
        "bootstrap",
        help="Create one tenant, owner, WeChat account, and Agent credentials",
    )
    center_bootstrap.add_argument("--tenant-slug", required=True)
    center_bootstrap.add_argument("--tenant-name", required=True)
    center_bootstrap.add_argument("--owner-email", required=True)
    center_bootstrap.add_argument("--wechat-account-id", required=True)
    center_bootstrap.add_argument("--agent-external-id", required=True)
    center_bootstrap.add_argument("--agent-name", default="")

    agent_parser = subparsers.add_parser("agent", help="Manage the unattended Windows desktop agent")
    agent_subparsers = agent_parser.add_subparsers(dest="agent_command")
    agent_activate = agent_subparsers.add_parser("activate", help="Activate this Windows Agent")
    agent_activate.add_argument("--code", required=True, help="One-time activation code")
    agent_activate.add_argument(
        "--center-url",
        help="Center URL; defaults to the installer build or WECHAT_AGENT_CENTER_URL",
    )
    agent_run = agent_subparsers.add_parser("run", help="Run the activated center Agent")
    agent_run.add_argument("--id", default="local-wechat-desktop", help="Connector id")
    agent_run.add_argument("--channel", default="wechat_desktop", help="Connector channel")
    agent_run.add_argument("--interval", type=float, default=1.0, help="Polling interval seconds")
    agent_install = agent_subparsers.add_parser("install", help="Install current-user logon task")
    agent_install.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    agent_install.add_argument(
        "--mode",
        choices=("local", "center"),
        help="Start the local bot or center Agent; activated installs default to center",
    )
    agent_uninstall = agent_subparsers.add_parser("uninstall", help="Remove current-user logon task")
    agent_uninstall.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    agent_status = agent_subparsers.add_parser("status", help="Show current-user logon task status")
    agent_status.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    agent_subparsers.add_parser("backup", help="Create and verify a state database snapshot")
    agent_subparsers.add_parser("show-config", help="Show redacted secure Agent binding status")
    agent_reset = agent_subparsers.add_parser("reset", help="Remove the secure Agent binding")
    agent_reset.add_argument("--yes", action="store_true", help="Remove the binding without prompting")
    return parser


def run_demo(question: str, env_path: str, chat_name: str) -> int:
    config = load_config(env_path)
    client = LLMClient(config)
    answer = client.generate_reply(
        chat_name=chat_name,
        history=[{"role": "user", "content": question, "sender": "user"}],
    )
    print(answer)
    return 0


def run_wechat_bot(
    env_path: str,
    force_dry_run: bool = False,
    force_live: bool = False,
    chats: list[str] | None = None,
) -> int:
    config = load_config(env_path)
    if force_dry_run:
        config = config.with_overrides(dry_run=True)
    if force_live:
        config = config.with_overrides(dry_run=False)
    if chats:
        config = config.with_overrides(wechat_listen_chats=chats)

    state_db = Path(config.state_db_file).expanduser().resolve() if config.state_db_file else None
    if state_db and state_db.is_file():
        backup_sqlite_database(state_db, keep=config.state_db_backup_retention)

    with SingleInstanceLock.from_config(config, env_path):
        bot = WeChatAutoReplyBot(config=config, llm_client=LLMClient(config))
        bot.start()
    return 0


def run_simulation(
    env_path: str,
    message: str,
    chat_name: str | None = None,
    sender: str = "simulated-user",
    msg_type: str = "text",
    reply: str | None = None,
    live_path: bool = False,
    queue: bool = False,
) -> int:
    config = load_config(env_path)
    effective_chat_name = chat_name or (config.wechat_listen_chats[0] if config.wechat_listen_chats else "demo")
    overrides: dict[str, object] = {}
    if queue:
        overrides.update(dry_run=False, require_manual_confirm=True, approval_mode="queue")
    elif live_path:
        overrides.update(dry_run=False, require_manual_confirm=False)
    else:
        overrides.update(dry_run=True)
    config = config.with_overrides(**overrides)

    client = StaticReplyClient(reply) if reply is not None else LLMClient(config)
    result = simulate_message(
        config=config,
        llm_client=client,
        chat_name=effective_chat_name,
        sender=sender,
        msg_type=msg_type,
        content=message,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_admin_command(env_path: str, args: argparse.Namespace) -> int:
    config = load_config(env_path)
    if args.admin_command == "status":
        print(render_status(config))
        return 0
    if args.admin_command == "pending":
        print(render_pending_replies(config, limit=args.limit))
        return 0
    if args.admin_command == "pause":
        print(pause_chat(config, chat_name=args.chat, reason=args.reason))
        return 0
    if args.admin_command == "handoff":
        print(pause_chat(config, chat_name=args.chat, reason=args.reason))
        return 0
    if args.admin_command == "resume":
        print(resume_chat(config, chat_name=args.chat))
        return 0
    if args.admin_command == "takeback":
        print(resume_chat(config, chat_name=args.chat))
        return 0
    if args.admin_command == "policy":
        print(set_chat_policy(config, chat_name=args.chat, policy=args.policy))
        return 0
    if args.admin_command == "report":
        if args.save:
            print(save_daily_report(config, report_date=args.date))
            return 0
        print(render_report(config, report_date=args.date))
        return 0
    raise RuntimeError("Missing admin command. Use status, pending, pause, resume, policy, or report.")


def run_dashboard_command(env_path: str, args: argparse.Namespace) -> int:
    config = load_config(env_path)
    run_dashboard_server(config, host=args.host, port=args.port)
    return 0


def run_connector_command(env_path: str, args: argparse.Namespace) -> int:
    if args.connector_command in {"doctor", "run-center"}:
        config = load_agent_runtime_config(
            env_path,
            require_binding=args.connector_command == "run-center",
        )
    else:
        config = load_config(env_path)
    if args.connector_command == "list":
        print(json.dumps(list_connectors(), ensure_ascii=False, indent=2))
        return 0
    if args.connector_command == "status":
        print(json.dumps(connector_status(config, connector_id=args.id, channel=args.channel), ensure_ascii=False, indent=2))
        return 0
    if args.connector_command == "doctor":
        print(json.dumps(connector_compatibility(config, connector_id=args.id, channel=args.channel), ensure_ascii=False, indent=2))
        return 0
    if args.connector_command == "run-local":
        with SingleInstanceLock.from_config(config, env_path):
            run_local_connector_agent(
                config,
                connector_id=args.id,
                channel=args.channel,
                interval_seconds=args.interval,
                echo=bool(args.echo),
            )
        return 0
    if args.connector_command == "run-center":
        with SingleInstanceLock.from_config(config, env_path):
            run_center_connector_agent(
                config,
                connector_id=args.id,
                channel=args.channel,
                interval_seconds=args.interval,
            )
        return 0
    raise RuntimeError("Missing connector command. Use list, status, doctor, run-local, or run-center.")


def _agent_connection_values(config: object) -> tuple[object, ...]:
    return tuple(
        getattr(config, name, "")
        for name in (
            "agent_center_url",
            "agent_tenant_id",
            "agent_instance_id",
            "agent_channel_account_id",
            "agent_token",
        )
    )


def load_agent_runtime_config(
    env_path: str,
    *,
    require_binding: bool,
    store: AgentProfileStore | None = None,
) -> object:
    config = load_config(env_path)
    connection_values = _agent_connection_values(config)
    if all(connection_values):
        return config
    if any(connection_values):
        if require_binding:
            raise RuntimeError(
                "Agent environment configuration is incomplete; configure all AGENT_* values "
                "or remove them and run `agent activate`."
            )
        return config

    try:
        profile_store = store or AgentProfileStore()
    except RuntimeError:
        if require_binding:
            raise
        return config
    if not profile_store.is_activated():
        if require_binding:
            raise RuntimeError("Agent is not activated. Run `agent activate` first.")
        return config
    profile = profile_store.load()
    return config.with_overrides(**profile_store.config_overrides(profile))


def run_center_command(env_path: str, args: argparse.Namespace) -> int:
    config = load_config(env_path)
    if args.center_command == "doctor":
        report = center_diagnostics(
            config.center_database_url,
            config.center_api_key_pepper,
            config.center_secret_key,
            config.center_provisioning_token,
            previous_api_key_peppers=config.center_api_key_previous_peppers,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if not config.center_database_url:
        raise RuntimeError("CENTER_DATABASE_URL is required")

    if args.center_command == "db-status":
        from wechat_ai.center_migrations import migration_status

        store = TenantStore(config.center_database_url)
        try:
            status = migration_status(store.engine)
            print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        finally:
            store.engine.dispose()
        return 0 if status.at_head and status.schema_valid else 1
    if args.center_command == "init-db":
        from wechat_ai.center_migrations import migration_status

        store = TenantStore(config.center_database_url)
        try:
            store.create_schema()
            status = migration_status(store.engine)
            print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        finally:
            store.engine.dispose()
        return 0
    if not config.center_api_key_pepper:
        raise RuntimeError("CENTER_API_KEY_PEPPER is required")
    if args.center_command == "bootstrap":
        store = TenantStore(config.center_database_url)
        store.create_schema()
        result = bootstrap_tenant(
            store,
            ApiKeyManager(config.center_api_key_pepper),
            tenant_slug=args.tenant_slug,
            tenant_name=args.tenant_name,
            owner_email=args.owner_email,
            channel="wechat_desktop",
            external_account_id=args.wechat_account_id,
            agent_external_id=args.agent_external_id,
            agent_name=args.agent_name,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        print("Store tenant_api_key and agent_token now; plaintext values are not retained.")
        return 0
    if args.center_command == "serve":
        if args.production and not config.center_provisioning_token:
            raise RuntimeError("CENTER_PROVISIONING_TOKEN is required in production")
        store, application = build_center_application(
            config.center_database_url,
            config.center_api_key_pepper,
            config.center_secret_key,
            provisioning_token=config.center_provisioning_token,
            previous_api_key_peppers=config.center_api_key_previous_peppers,
        )
        worker = CenterReplyWorker(store, config, SecretCipher(config.center_secret_key))
        application.readiness_probe = lambda: center_service_readiness(store, worker)
        server_runner = run_center_production_server if args.production else run_center_server
        server_options = {"threads": args.threads} if args.production else {}
        server_runner(
            application,
            host=config.center_host,
            port=config.center_port,
            background_runner=worker.run_forever,
            **server_options,
        )
        return 0
    raise RuntimeError("Missing center command. Use init-db, db-status, doctor, bootstrap, or serve.")


def run_agent_command(env_path: str, args: argparse.Namespace) -> int:
    project_dir = Path(__file__).resolve().parent
    if args.agent_command == "activate":
        center_url = resolve_center_url(args.center_url)
        store = AgentProfileStore()
        activate_and_save(
            activation_code=args.code,
            center_url=center_url,
            store=store,
        )
        print("Windows Agent activation completed. Credentials are protected for the current user.")
        return 0
    if args.agent_command == "show-config":
        print(json.dumps(public_profile_status(AgentProfileStore()), ensure_ascii=False, indent=2))
        return 0
    if args.agent_command == "reset":
        store = AgentProfileStore()
        if not args.yes:
            try:
                answer = input("Remove this Agent binding? Type YES to continue: ")
            except EOFError:
                raise RuntimeError("Confirmation is required; use --yes for unattended reset.") from None
            if answer.strip() != "YES":
                print("Agent binding was not changed.")
                return 0
        removed = store.reset_binding()
        print("Agent binding removed." if removed else "Agent is not activated.")
        return 0
    if args.agent_command == "run":
        config = load_agent_runtime_config(env_path, require_binding=True)
        with SingleInstanceLock.from_config(config, env_path):
            run_center_connector_agent(
                config,
                connector_id=args.id,
                channel=args.channel,
                interval_seconds=args.interval,
            )
        return 0

    config = load_config(env_path)
    if args.agent_command == "backup":
        if not config.state_db_file:
            raise RuntimeError("STATE_DB_FILE is required for database backup.")
        database_path = Path(config.state_db_file).expanduser().resolve()
        backup = backup_sqlite_database(
            database_path,
            keep=config.state_db_backup_retention,
        )
        print(str(backup))
        return 0

    task_name = str(getattr(args, "task_name", DEFAULT_TASK_NAME))
    scheduler = WindowsTaskScheduler()
    if args.agent_command == "install":
        requested_mode = getattr(args, "mode", None)
        store = AgentProfileStore()
        has_profile = store.is_activated()
        has_env_binding = all(_agent_connection_values(config))
        mode = requested_mode or ("center" if has_profile or has_env_binding else "local")
        if mode == "center":
            load_agent_runtime_config(env_path, require_binding=True, store=store)
        env_file = Path(env_path).expanduser().resolve()
        if mode == "center" and not env_file.is_file() and not has_profile:
            raise RuntimeError(
                "A persistent Agent binding is required for installation; run `agent activate` first."
            )
        resolved_env = (
            env_file
            if env_file.is_file() or mode == "local"
            else store.ensure_runtime_marker()
        )
        pythonw_path = Path(sys.executable).with_name("pythonw.exe").resolve()
        task = WindowsAgentTask(
            project_dir=project_dir,
            env_path=resolved_env,
            pythonw_path=pythonw_path,
            watchdog_path=project_dir / "run_watchdog.ps1",
            powershell_path=Path(
                os.environ.get("SystemRoot", r"C:\Windows")
            ) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            mode=mode,
            task_name=task_name,
        )
        scheduler.install(task)
        print(f"Installed Windows desktop agent task: {task_name}")
        return 0
    if args.agent_command == "uninstall":
        scheduler.uninstall(task_name)
        print(f"Removed Windows desktop agent task: {task_name}")
        return 0
    if args.agent_command == "status":
        result = scheduler.status(task_name)
        print(result.stdout or "")
        return 0
    raise RuntimeError(
        "Missing agent command. Use activate, run, install, uninstall, status, backup, "
        "show-config, or reset."
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            return run_demo(" ".join(args.question), env_path=args.env, chat_name=args.chat)

        if args.command == "simulate":
            return run_simulation(
                env_path=args.env,
                message=" ".join(args.message),
                chat_name=args.chat,
                sender=args.sender,
                msg_type=args.msg_type,
                reply=args.reply,
                live_path=args.live_path,
                queue=args.queue,
            )

        if args.command == "config":
            requested_path = Path(args.env).expanduser().resolve()
            if requested_path.name == "agent.runtime":
                profile_store = AgentProfileStore()
                config = load_agent_runtime_config(
                    args.env,
                    require_binding=True,
                    store=profile_store,
                )
            else:
                config = load_config(args.env)
            print(json.dumps(config.to_public_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "init":
            path = init_env(args.env, force=args.force, yes=args.yes)
            print(f"Created {path}. Run `python main.py doctor` before starting the bot.")
            return 0

        if args.command == "doctor":
            return run_doctor(env_path=args.env, check_llm_endpoint=not args.skip_llm)

        if args.command == "image-probe":
            run_image_probe(
                chat_name=args.chat,
                seconds=args.seconds,
                interval_seconds=args.interval,
                output_dir=args.out,
            )
            return 0

        if args.command == "friends":
            result = accept_friend_requests(
                live=bool(args.live),
                limit=args.limit,
                switch_to_contacts=not bool(args.no_switch),
            )
            print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
            return 0

        if args.command == "admin":
            return run_admin_command(env_path=args.env, args=args)

        if args.command == "dashboard":
            return run_dashboard_command(env_path=args.env, args=args)

        if args.command == "connector":
            return run_connector_command(env_path=args.env, args=args)

        if args.command == "agent":
            return run_agent_command(env_path=args.env, args=args)

        if args.command == "center":
            return run_center_command(env_path=args.env, args=args)

        return run_wechat_bot(
            env_path=args.env,
            force_dry_run=bool(getattr(args, "dry_run", False)),
            force_live=bool(getattr(args, "live", False)),
            chats=getattr(args, "chats", None),
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
