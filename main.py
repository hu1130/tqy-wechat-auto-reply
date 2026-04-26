from __future__ import annotations

import argparse
import json
import sys

from wechat_ai.bot import WeChatAutoReplyBot
from wechat_ai.config import load_config
from wechat_ai.llm import LLMClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WeChat AI auto-reply bot")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run WeChat auto-reply bot")
    run_parser.add_argument("--dry-run", action="store_true", help="Generate replies without sending them")

    demo_parser = subparsers.add_parser("demo", help="Test LLM reply generation only")
    demo_parser.add_argument("question", nargs="+", help="Question to send to the LLM")

    subparsers.add_parser("config", help="Print loaded config")
    return parser


def run_demo(question: str) -> int:
    config = load_config()
    client = LLMClient(config)
    answer = client.generate_reply(
        chat_name="demo",
        history=[{"role": "user", "content": question, "sender": "user"}],
    )
    print(answer)
    return 0


def run_wechat_bot(force_dry_run: bool = False) -> int:
    config = load_config()
    if force_dry_run:
        config = config.with_overrides(dry_run=True)

    bot = WeChatAutoReplyBot(config=config, llm_client=LLMClient(config))
    bot.start()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            return run_demo(" ".join(args.question))

        if args.command == "config":
            config = load_config()
            print(json.dumps(config.to_public_dict(), ensure_ascii=False, indent=2))
            return 0

        dry_run = bool(getattr(args, "dry_run", False))
        return run_wechat_bot(force_dry_run=dry_run)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
