from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class AppConfig:
    llm_provider: str
    llm_model: str
    ollama_base_url: str
    openai_base_url: str
    openai_api_key: str
    request_timeout: int
    wechat_listen_chats: list[str]
    wechat_self_names: list[str]
    max_context_messages: int
    dedup_window_seconds: int
    dry_run: bool
    system_prompt: str

    def with_overrides(self, **overrides: object) -> "AppConfig":
        return replace(self, **overrides)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "ollama_base_url": self.ollama_base_url,
            "openai_base_url": self.openai_base_url,
            "openai_api_key_set": bool(self.openai_api_key),
            "request_timeout": self.request_timeout,
            "wechat_listen_chats": self.wechat_listen_chats,
            "wechat_self_names": self.wechat_self_names,
            "max_context_messages": self.max_context_messages,
            "dedup_window_seconds": self.dedup_window_seconds,
            "dry_run": self.dry_run,
            "system_prompt": self.system_prompt,
        }


def load_config(env_path: str | Path = ".env") -> AppConfig:
    _load_env_file(Path(env_path))

    return AppConfig(
        llm_provider=os.getenv("LLM_PROVIDER", "ollama").strip().lower(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-r1:14b").strip(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        request_timeout=_get_int("REQUEST_TIMEOUT", 120),
        wechat_listen_chats=_split_csv(os.getenv("WECHAT_LISTEN_CHATS", "文件传输助手")),
        wechat_self_names=_split_csv(os.getenv("WECHAT_SELF_NAMES", "")),
        max_context_messages=_get_int("MAX_CONTEXT_MESSAGES", 8),
        dedup_window_seconds=_get_int("DEDUP_WINDOW_SECONDS", 60),
        dry_run=_get_bool("DRY_RUN", True),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            "你是一个微信聊天助手。请根据最近上下文，生成自然、简短、像真人发送的中文回复。只输出回复正文，不要解释，不要加引号，不要输出思考过程。",
        ).strip(),
    )

