from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ctypes
import importlib.util
import json
import platform
import sys
from typing import Any
from urllib import error, request

from wechat_ai.config import AppConfig, load_config, validate_config
from wechat_ai.knowledge import KnowledgeBase
from wechat_ai.store import SQLiteStore


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    fix: str = ""


def _url_json(url: str, timeout: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = request.Request(url=url, headers=headers or {}, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def check_python() -> DoctorCheck:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 9):
        return DoctorCheck("Python", "ok", text)
    return DoctorCheck("Python", "fail", text, "Install Python 3.9 or newer.")


def check_platform() -> DoctorCheck:
    system = platform.system()
    if system == "Windows":
        return DoctorCheck("Windows", "ok", "Running on Windows.")
    return DoctorCheck(
        "Windows",
        "fail",
        f"Current system is {system or 'unknown'}. Real WeChat automation requires Windows desktop WeChat.",
        "Run the bot on a Windows machine with desktop WeChat logged in.",
    )


def check_env_file(env_path: str | Path) -> DoctorCheck:
    path = Path(env_path)
    if path.exists():
        return DoctorCheck(".env", "ok", str(path))
    return DoctorCheck(".env", "warn", f"{path} not found.", "Run `python main.py init` first.")


def check_config(config: AppConfig) -> list[DoctorCheck]:
    warnings = validate_config(config)
    if not warnings:
        return [DoctorCheck("Config", "ok", "Required config values look valid.")]
    return [DoctorCheck("Config", "fail", item) for item in warnings]


def check_wxauto() -> DoctorCheck:
    if platform.system() != "Windows":
        return DoctorCheck(
            "wxauto",
            "fail",
            "wxauto is only useful on Windows for this project.",
            "Run this project on Windows and install dependencies with `pip install -r requirements.txt`.",
        )
    if importlib.util.find_spec("wxauto") is not None:
        return DoctorCheck("wxauto", "ok", "wxauto is installed.")
    return DoctorCheck("wxauto", "fail", "wxauto is not installed.", "Run `pip install -r requirements.txt`.")


def _find_window_by_class(class_name: str) -> int:
    if platform.system() != "Windows":
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(class_name, None))
    except Exception:
        return 0


def check_wechat_window() -> DoctorCheck:
    if platform.system() != "Windows":
        return DoctorCheck(
            "WeChat window",
            "warn",
            "Skipped because this is not Windows.",
        )
    hwnd = _find_window_by_class("WeChatMainWndForPC")
    if hwnd:
        return DoctorCheck("WeChat window", "ok", f"class=WeChatMainWndForPC hwnd={hwnd}")
    return DoctorCheck(
        "WeChat window",
        "warn",
        "Classic Windows WeChat main window was not found.",
        "Open and log in to the classic Windows desktop WeChat 3.9.x main window. "
        "wxauto does not attach to the new Weixin/WeChat 4.x window used by WeChatAppEx.",
    )


def check_llm(config: AppConfig) -> DoctorCheck:
    timeout = min(max(config.request_timeout, 1), 10)
    if config.llm_provider == "ollama":
        url = f"{config.ollama_base_url}/api/tags"
        try:
            payload = _url_json(url, timeout=timeout)
        except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return DoctorCheck(
                "LLM",
                "fail",
                f"Cannot reach Ollama at {config.ollama_base_url}: {exc}",
                "Start Ollama and run `ollama pull <model>`, or switch LLM_PROVIDER to openai_compatible.",
            )

        model_names = {str(item.get("name", "")) for item in payload.get("models", [])}
        if config.llm_model in model_names:
            return DoctorCheck("LLM", "ok", f"Ollama is reachable and model {config.llm_model!r} is installed.")
        return DoctorCheck(
            "LLM",
            "warn",
            f"Ollama is reachable, but model {config.llm_model!r} was not listed.",
            f"Run `ollama pull {config.llm_model}` or set LLM_MODEL to an installed model.",
        )

    if config.llm_provider == "openai_compatible":
        if not config.openai_api_key:
            return DoctorCheck(
                "LLM",
                "fail",
                "OPENAI_API_KEY is empty.",
                "Set OPENAI_API_KEY in .env or run `python main.py init --force`.",
            )
        return DoctorCheck(
            "LLM",
            "ok",
            f"OpenAI-compatible provider configured: {config.openai_base_url}",
        )

    return DoctorCheck(
        "LLM",
        "fail",
        f"Unknown LLM_PROVIDER: {config.llm_provider}",
        "Use LLM_PROVIDER=ollama or LLM_PROVIDER=openai_compatible.",
    )


def check_state_db(config: AppConfig) -> DoctorCheck:
    if not config.state_db_file:
        return DoctorCheck(
            "State DB",
            "warn",
            "STATE_DB_FILE is empty. Runtime state will not be persisted.",
            "Set STATE_DB_FILE=data/wechat-ai.sqlite3 for company use.",
        )
    try:
        SQLiteStore(config.state_db_file)
    except Exception as exc:
        return DoctorCheck(
            "State DB",
            "fail",
            f"Cannot open {config.state_db_file}: {exc}",
            "Use a writable local path such as data/wechat-ai.sqlite3.",
        )
    return DoctorCheck("State DB", "ok", config.state_db_file)


def check_knowledge_base(config: AppConfig) -> DoctorCheck:
    if not config.knowledge_file:
        if config.require_knowledge_match:
            return DoctorCheck(
                "Knowledge",
                "fail",
                "REQUIRE_KNOWLEDGE_MATCH=1 but KNOWLEDGE_FILE is empty.",
                "Set KNOWLEDGE_FILE to a UTF-8 Markdown FAQ file.",
            )
        return DoctorCheck("Knowledge", "warn", "KNOWLEDGE_FILE is empty.")
    path = Path(config.knowledge_file)
    if not path.exists():
        status = "fail" if config.require_knowledge_match else "warn"
        return DoctorCheck(
            "Knowledge",
            status,
            f"{config.knowledge_file} not found.",
            "Create the knowledge file before enabling REQUIRE_KNOWLEDGE_MATCH=1.",
        )
    try:
        kb = KnowledgeBase(path)
    except OSError as exc:
        return DoctorCheck(
            "Knowledge",
            "fail",
            f"Cannot read {config.knowledge_file}: {exc}",
            "Make sure the file is UTF-8 and readable.",
        )
    if not kb.entries:
        return DoctorCheck(
            "Knowledge",
            "warn",
            f"{config.knowledge_file} has no usable entries.",
            "Add Markdown sections with company FAQ content.",
        )
    return DoctorCheck("Knowledge", "ok", f"{len(kb.entries)} entries loaded from {config.knowledge_file}.")


def render_checks(checks: list[DoctorCheck]) -> str:
    labels = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    lines = []
    for item in checks:
        label = labels.get(item.status, item.status.upper())
        lines.append(f"[{label}] {item.name}: {item.detail}")
        if item.fix:
            lines.append(f"      fix: {item.fix}")
    return "\n".join(lines)


def run_doctor(env_path: str | Path = ".env", check_llm_endpoint: bool = True) -> int:
    checks: list[DoctorCheck] = [check_python(), check_platform(), check_env_file(env_path)]

    try:
        config = load_config(env_path)
    except RuntimeError as exc:
        checks.append(DoctorCheck("Config", "fail", str(exc)))
        print(render_checks(checks))
        return 1

    checks.extend(check_config(config))
    checks.append(check_state_db(config))
    checks.append(check_knowledge_base(config))
    checks.append(check_wxauto())
    checks.append(check_wechat_window())
    if check_llm_endpoint:
        checks.append(check_llm(config))

    print(render_checks(checks))
    return 1 if any(item.status == "fail" for item in checks) else 0
