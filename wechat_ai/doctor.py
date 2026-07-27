from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib.util
import json
import platform
import sys
from typing import Any
from urllib import error, request

from wechat_ai.config import AppConfig, load_config, validate_config
from wechat_ai.knowledge import KnowledgeBase
from wechat_ai.store import SQLiteStore
from wechat_ai.wechat_runtime import detect_wechat_client_version


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


def check_wxauto4() -> DoctorCheck:
    if platform.system() != "Windows":
        return DoctorCheck(
            "wxauto4",
            "fail",
            "wxauto4 is only useful on Windows for this project.",
            "Run this project on Windows and install dependencies with `pip install -r requirements.txt`.",
        )
    if importlib.util.find_spec("wxauto4") is not None:
        return DoctorCheck("wxauto4", "ok", "wxauto4 is installed and importable.")
    return DoctorCheck("wxauto4", "fail", "wxauto4 is not installed.", "Run `pip install -r requirements.txt`.")


def check_weixin_window() -> DoctorCheck:
    if platform.system() != "Windows":
        return DoctorCheck("Weixin window", "warn", "Skipped because this is not Windows.")
    try:
        import uiautomation as auto
    except Exception as exc:
        return DoctorCheck(
            "Weixin window",
            "warn",
            f"Cannot import uiautomation: {exc}",
            "Run `pip install -r requirements.txt` to enable Weixin window diagnostics.",
        )

    try:
        auto.SetGlobalSearchTimeout(2)
        win = auto.WindowControl(searchDepth=1, Name="微信")
        if not win.Exists(1):
            return DoctorCheck(
                "Weixin window",
                "warn",
                "Could not find a top-level Weixin window named 微信.",
                "Open and log in to a tested Windows Weixin version, then keep the main window visible.",
            )
        class_name = str(getattr(win, "ClassName", "") or "")
        window_name = str(getattr(win, "Name", "") or "")
    except Exception as exc:
        return DoctorCheck(
            "Weixin window",
            "warn",
            f"Could not inspect the Weixin UI Automation tree: {exc}",
            "Run PowerShell and Weixin with the same privilege level, then try doctor again.",
        )

    if "LoginWindow" in class_name:
        return DoctorCheck(
            "Weixin window",
            "fail",
            f"Found Weixin login window instead of the logged-in chat window: {class_name}.",
            "Log in to a version listed in WECHAT_VERIFIED_VERSION_PREFIXES and keep the main window visible.",
        )
    return DoctorCheck(
        "Weixin window",
        "ok",
        f"Found visible Weixin window: name={window_name or '(empty)'}, class={class_name or '(empty)'}.",
    )


def check_wxauto4_attach(config: AppConfig | None = None) -> DoctorCheck:
    if platform.system() != "Windows":
        return DoctorCheck("wxauto4 attach", "warn", "Skipped because this is not Windows.")
    try:
        from wxauto4 import WeChat
    except Exception as exc:
        return DoctorCheck(
            "wxauto4 attach",
            "fail",
            f"Cannot import wxauto4 WeChat: {exc}",
            "Run `pip install -r requirements.txt` in the active virtual environment.",
        )
    try:
        client = WeChat(ads=False, resize=False)
    except Exception as exc:
        return DoctorCheck(
            "wxauto4 attach",
            "fail",
            f"wxauto4 could not attach to the logged-in Weixin window: {exc}",
            "Open and log in to a tested Weixin version, keep the main window visible, and run this command "
            "with the same privilege level as Weixin.",
        )

    if config is not None:
        version = detect_wechat_client_version(client, config.wechat_client_version)
        if not version:
            return DoctorCheck(
                "Weixin version",
                "fail",
                "Could not determine the desktop client version.",
                "Set WECHAT_CLIENT_VERSION to the installed file version after checking Weixin.exe properties.",
            )
        if not any(version.startswith(prefix) for prefix in config.wechat_verified_version_prefixes):
            return DoctorCheck(
                "Weixin version",
                "fail",
                f"Client {version} is not in WECHAT_VERIFIED_VERSION_PREFIXES.",
                "Run compatibility regression tests before adding this version to the verified list.",
            )

    supports_listener = all(callable(getattr(client, name, None)) for name in ("AddListenChat", "KeepRunning"))
    supports_polling = all(callable(getattr(client, name, None)) for name in ("ChatWith", "GetAllMessage", "SendMsg"))
    if supports_listener:
        return DoctorCheck("wxauto4 attach", "ok", "Attached to the logged-in Weixin window with listener API.")
    if supports_polling:
        return DoctorCheck("wxauto4 attach", "ok", "Attached to the logged-in Weixin window with polling API.")
    return DoctorCheck(
        "wxauto4 attach",
        "fail",
        "Attached to Weixin, but wxauto4 exposes neither listener methods "
        "(AddListenChat, KeepRunning) nor polling methods (ChatWith, GetAllMessage, SendMsg).",
        "Install a wxauto4 build that supports one of these method sets.",
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
    checks.append(check_wxauto4())
    checks.append(check_weixin_window())
    checks.append(check_wxauto4_attach(config))
    if check_llm_endpoint:
        checks.append(check_llm(config))

    print(render_checks(checks))
    return 1 if any(item.status == "fail" for item in checks) else 0
