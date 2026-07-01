from __future__ import annotations

from getpass import getpass
from pathlib import Path

from wechat_ai.config import DEFAULT_ENV_VALUES, write_env_file


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_secret(prompt: str, default: str = "") -> str:
    suffix = " [already set]" if default else ""
    answer = getpass(f"{prompt}{suffix}: ").strip()
    return answer or default


def collect_interactive_values() -> dict[str, str]:
    provider = _ask("LLM provider: ollama/openai_compatible", DEFAULT_ENV_VALUES["LLM_PROVIDER"]).lower()
    if provider not in {"ollama", "openai_compatible"}:
        provider = DEFAULT_ENV_VALUES["LLM_PROVIDER"]

    values = dict(DEFAULT_ENV_VALUES)
    values["LLM_PROVIDER"] = provider

    if provider == "ollama":
        values["LLM_MODEL"] = _ask("Ollama model", DEFAULT_ENV_VALUES["LLM_MODEL"])
        values["OLLAMA_BASE_URL"] = _ask("Ollama base URL", DEFAULT_ENV_VALUES["OLLAMA_BASE_URL"])
    else:
        values["LLM_MODEL"] = _ask("Model name", "gpt-4o-mini")
        values["OPENAI_BASE_URL"] = _ask("OpenAI-compatible base URL", DEFAULT_ENV_VALUES["OPENAI_BASE_URL"])
        values["OPENAI_API_KEY"] = _ask_secret("OpenAI API key")

    values["WECHAT_LISTEN_CHATS"] = _ask(
        "Wechat chats to listen to, comma separated",
        DEFAULT_ENV_VALUES["WECHAT_LISTEN_CHATS"],
    )
    values["WECHAT_SELF_NAMES"] = _ask(
        "Your own WeChat names, comma separated, optional",
        DEFAULT_ENV_VALUES["WECHAT_SELF_NAMES"],
    )
    values["DRY_RUN"] = _ask("Start in dry-run mode? 1=yes, 0=no", DEFAULT_ENV_VALUES["DRY_RUN"])
    return values


def init_env(env_path: str | Path = ".env", force: bool = False, yes: bool = False) -> Path:
    values = dict(DEFAULT_ENV_VALUES) if yes else collect_interactive_values()
    return write_env_file(env_path, values=values, force=force)
