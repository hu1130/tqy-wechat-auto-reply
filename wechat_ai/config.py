from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os

DEFAULT_SYSTEM_PROMPT = (
    "你是一个微信聊天助手。请根据最近上下文，生成自然、简短、像真人发送的中文回复。"
    "只输出回复正文，不要解释，不要加引号，不要输出思考过程。"
)

DEFAULT_ENV_VALUES = {
    "LLM_PROVIDER": "ollama",
    "LLM_MODEL": "deepseek-r1:14b",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_API_KEY": "",
    "REQUEST_TIMEOUT": "120",
    "WECHAT_LISTEN_CHATS": "文件传输助手",
    "WECHAT_SELF_NAMES": "",
    "MAX_CONTEXT_MESSAGES": "8",
    "DEDUP_WINDOW_SECONDS": "60",
    "DRY_RUN": "1",
    "SYSTEM_PROMPT": DEFAULT_SYSTEM_PROMPT,
}


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
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {value!r}") from exc


def env_template(values: dict[str, str] | None = None) -> str:
    merged = dict(DEFAULT_ENV_VALUES)
    if values:
        merged.update({key: str(value) for key, value in values.items()})

    return "\n".join(
        [
            "# LLM settings",
            f"LLM_PROVIDER={merged['LLM_PROVIDER']}",
            f"LLM_MODEL={merged['LLM_MODEL']}",
            f"OLLAMA_BASE_URL={merged['OLLAMA_BASE_URL']}",
            f"OPENAI_BASE_URL={merged['OPENAI_BASE_URL']}",
            f"OPENAI_API_KEY={merged['OPENAI_API_KEY']}",
            f"REQUEST_TIMEOUT={merged['REQUEST_TIMEOUT']}",
            "",
            "# WeChat bot settings",
            f"WECHAT_LISTEN_CHATS={merged['WECHAT_LISTEN_CHATS']}",
            f"WECHAT_SELF_NAMES={merged['WECHAT_SELF_NAMES']}",
            f"MAX_CONTEXT_MESSAGES={merged['MAX_CONTEXT_MESSAGES']}",
            f"DEDUP_WINDOW_SECONDS={merged['DEDUP_WINDOW_SECONDS']}",
            f"DRY_RUN={merged['DRY_RUN']}",
            "",
            "# Reply style",
            f"SYSTEM_PROMPT={merged['SYSTEM_PROMPT']}",
            "",
        ]
    )


def write_env_file(path: str | Path, values: dict[str, str] | None = None, force: bool = False) -> Path:
    env_path = Path(path)
    if env_path.exists() and not force:
        raise RuntimeError(f"{env_path} already exists. Use --force to overwrite it.")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(env_template(values), encoding="utf-8")
    return env_path


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


def validate_config(config: AppConfig) -> list[str]:
    warnings: list[str] = []
    if config.llm_provider not in {"ollama", "openai_compatible"}:
        warnings.append("LLM_PROVIDER should be either 'ollama' or 'openai_compatible'.")
    if config.llm_provider == "openai_compatible" and not config.openai_api_key:
        warnings.append("OPENAI_API_KEY is required when LLM_PROVIDER=openai_compatible.")
    if not config.wechat_listen_chats:
        warnings.append("WECHAT_LISTEN_CHATS is empty. At least one chat name is required.")
    if config.max_context_messages <= 0:
        warnings.append("MAX_CONTEXT_MESSAGES should be greater than 0.")
    if config.dedup_window_seconds < 0:
        warnings.append("DEDUP_WINDOW_SECONDS should not be negative.")
    if config.request_timeout <= 0:
        warnings.append("REQUEST_TIMEOUT should be greater than 0.")
    return warnings


def load_config(env_path: str | Path = ".env") -> AppConfig:
    _load_env_file(Path(env_path))

    return AppConfig(
        llm_provider=os.getenv("LLM_PROVIDER", DEFAULT_ENV_VALUES["LLM_PROVIDER"]).strip().lower(),
        llm_model=os.getenv("LLM_MODEL", DEFAULT_ENV_VALUES["LLM_MODEL"]).strip(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", DEFAULT_ENV_VALUES["OLLAMA_BASE_URL"]).strip().rstrip("/"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_ENV_VALUES["OPENAI_BASE_URL"]).strip().rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY", DEFAULT_ENV_VALUES["OPENAI_API_KEY"]).strip(),
        request_timeout=_get_int("REQUEST_TIMEOUT", int(DEFAULT_ENV_VALUES["REQUEST_TIMEOUT"])),
        wechat_listen_chats=_split_csv(os.getenv("WECHAT_LISTEN_CHATS", DEFAULT_ENV_VALUES["WECHAT_LISTEN_CHATS"])),
        wechat_self_names=_split_csv(os.getenv("WECHAT_SELF_NAMES", DEFAULT_ENV_VALUES["WECHAT_SELF_NAMES"])),
        max_context_messages=_get_int("MAX_CONTEXT_MESSAGES", int(DEFAULT_ENV_VALUES["MAX_CONTEXT_MESSAGES"])),
        dedup_window_seconds=_get_int("DEDUP_WINDOW_SECONDS", int(DEFAULT_ENV_VALUES["DEDUP_WINDOW_SECONDS"])),
        dry_run=_get_bool("DRY_RUN", True),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            DEFAULT_SYSTEM_PROMPT,
        ).strip(),
    )
