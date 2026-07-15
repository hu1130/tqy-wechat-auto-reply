from __future__ import annotations

import argparse
import json
import sys

from wechat_ai.admin import (
    pause_chat,
    render_pending_replies,
    render_report,
    render_status,
    resume_chat,
    save_daily_report,
    set_chat_policy,
)
from wechat_ai.bot import WeChatAutoReplyBot
from wechat_ai.config import load_config
from wechat_ai.doctor import run_doctor
from wechat_ai.llm import LLMClient
from wechat_ai.simulator import StaticReplyClient, simulate_message
from wechat_ai.wizard import init_env


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

    admin_parser = subparsers.add_parser("admin", help="Manage runtime state")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command")
    admin_subparsers.add_parser("status", help="Show persisted chat state")
    pending_parser = admin_subparsers.add_parser("pending", help="Show queued replies waiting for manual approval")
    pending_parser.add_argument("--limit", type=int, default=20, help="Number of pending replies to show")

    pause_parser = admin_subparsers.add_parser("pause", help="Pause one chat")
    pause_parser.add_argument("chat", help="Chat name to pause")
    pause_parser.add_argument("--reason", default="manual", help="Pause reason")

    resume_parser = admin_subparsers.add_parser("resume", help="Resume one chat")
    resume_parser.add_argument("chat", help="Chat name to resume")

    policy_parser = admin_subparsers.add_parser("policy", help="Set one chat policy")
    policy_parser.add_argument("chat", help="Chat name")
    policy_parser.add_argument("policy", help="auto, manual, dry_run, or ignore")

    report_parser = admin_subparsers.add_parser("report", help="Show daily audit report")
    report_parser.add_argument("--date", help="Date in YYYY-MM-DD format, default: today")
    report_parser.add_argument("--save", action="store_true", help="Save report to REPORTS_DIR")
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
    if args.admin_command == "resume":
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
            config = load_config(args.env)
            print(json.dumps(config.to_public_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "init":
            path = init_env(args.env, force=args.force, yes=args.yes)
            print(f"Created {path}. Run `python main.py doctor` before starting the bot.")
            return 0

        if args.command == "doctor":
            return run_doctor(env_path=args.env, check_llm_endpoint=not args.skip_llm)

        if args.command == "admin":
            return run_admin_command(env_path=args.env, args=args)

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
