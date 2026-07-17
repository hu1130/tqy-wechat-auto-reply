from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import os

ALLOWED_CHAT_POLICIES = {"auto", "manual", "dry_run", "ignore"}
ALLOWED_APPROVAL_MODES = {"console", "queue"}

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
    "MESSAGE_BUFFER_SECONDS": "3",
    "DEDUP_WINDOW_SECONDS": "60",
    "DRY_RUN": "1",
    "REPLY_ALLOWED_CHATS": "文件传输助手",
    "REPLY_BLOCKED_CHATS": "",
    "REPLY_ALLOWED_SENDERS": "",
    "REPLY_BLOCKED_SENDERS": "",
    "TEXT_MESSAGE_TYPES": "text,friend",
    "VOICE_TO_TEXT_ENABLED": "1",
    "VOICE_TO_TEXT_TIMEOUT_SECONDS": "2",
    "GROUP_CHATS_ENABLED": "0",
    "GROUP_CHAT_PATTERNS": "\u7fa4\u804a,\u5fae\u4fe1\u7fa4,\u5de5\u4f5c\u7fa4",
    "DASHBOARD_USERS": "admin:admin123:admin",
    "WELCOME_MESSAGE_ENABLED": "1",
    "WELCOME_MESSAGE": "\u60a8\u597d\uff0c\u6211\u662f\u6df1\u5ea6\u89c9\u9192\u7684\u53ee\u5f53\uff0c\u4e3b\u8981\u5e2e\u60a8\u4e86\u89e3\u6211\u4eec\u7684AI\u5ba2\u670d\u3001\u4f01\u4e1a\u667a\u80fd\u4f53\u548c\u76f8\u5173\u843d\u5730\u65b9\u6848\u3002\u60a8\u53ef\u4ee5\u76f4\u63a5\u544a\u8bc9\u6211\u60f3\u89e3\u51b3\u7684\u95ee\u9898\uff0c\u6211\u4f1a\u5148\u5e2e\u60a8\u6574\u7406\u65b9\u5411\uff0c\u5fc5\u8981\u65f6\u518d\u8f6c\u4eba\u5de5\u540c\u4e8b\u8ddf\u8fdb\u3002",
    "SENSITIVE_KEYWORDS": "转账,付款,收款,验证码,密码,银行卡,身份证,合同,报价,价格,地址,电话,手机号,隐私,发票,退款,投诉",
    "MAX_REPLY_CHARS": "120",
    "BLOCKED_REPLY_PATTERNS": "我是AI,作为AI,我不能,无法提供,验证码,密码,银行卡,身份证,转账",
    "DEFAULT_CHAT_POLICY": "manual",
    "CHAT_POLICIES": "",
    "REPLY_COOLDOWN_SECONDS": "30",
    "REQUIRE_MANUAL_CONFIRM": "1",
    "APPROVAL_MODE": "console",
    "PENDING_REPLIES_FILE": "logs/pending_replies.jsonl",
    "MAX_SENDS_PER_RUN": "20",
    "MAX_SENDS_PER_CHAT": "5",
    "MAX_SENDS_PER_DAY": "100",
    "MAX_SENDS_PER_CHAT_PER_DAY": "20",
    "MAX_ERRORS_BEFORE_PAUSE": "3",
    "LOG_FILE": "logs/wechat-ai.log",
    "AUDIT_LOG_FILE": "logs/audit.jsonl",
    "ALERT_FILE": "logs/alerts.log",
    "LOG_MAX_BYTES": "10485760",
    "LOG_BACKUP_COUNT": "5",
    "HEALTH_FILE": "logs/health.json",
    "REPORTS_DIR": "reports",
    "STATE_DB_FILE": "data/wechat-ai.sqlite3",
    "KNOWLEDGE_FILE": "knowledge/faq.md",
    "KNOWLEDGE_TOP_K": "3",
    "REQUIRE_KNOWLEDGE_MATCH": "0",
    "KNOWLEDGE_FALLBACK_REPLY": "这个问题我需要确认一下，稍后回复你。",
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


def _split_mapping(value: str, name: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in _split_csv(value):
        if "=" not in item:
            raise RuntimeError(f"{name} entries must use chat=policy format, got {item!r}")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise RuntimeError(f"{name} entries must use chat=policy format, got {item!r}")
        mapping[key] = raw_value
    return mapping


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
            f"MESSAGE_BUFFER_SECONDS={merged['MESSAGE_BUFFER_SECONDS']}",
            f"DEDUP_WINDOW_SECONDS={merged['DEDUP_WINDOW_SECONDS']}",
            f"DRY_RUN={merged['DRY_RUN']}",
            "",
            "# Safety guardrails",
            f"REPLY_ALLOWED_CHATS={merged['REPLY_ALLOWED_CHATS']}",
            f"REPLY_BLOCKED_CHATS={merged['REPLY_BLOCKED_CHATS']}",
            f"REPLY_ALLOWED_SENDERS={merged['REPLY_ALLOWED_SENDERS']}",
            f"REPLY_BLOCKED_SENDERS={merged['REPLY_BLOCKED_SENDERS']}",
            f"TEXT_MESSAGE_TYPES={merged['TEXT_MESSAGE_TYPES']}",
            f"VOICE_TO_TEXT_ENABLED={merged['VOICE_TO_TEXT_ENABLED']}",
            f"VOICE_TO_TEXT_TIMEOUT_SECONDS={merged['VOICE_TO_TEXT_TIMEOUT_SECONDS']}",
            f"GROUP_CHATS_ENABLED={merged['GROUP_CHATS_ENABLED']}",
            f"GROUP_CHAT_PATTERNS={merged['GROUP_CHAT_PATTERNS']}",
            f"DASHBOARD_USERS={merged['DASHBOARD_USERS']}",
            f"WELCOME_MESSAGE_ENABLED={merged['WELCOME_MESSAGE_ENABLED']}",
            f"WELCOME_MESSAGE={merged['WELCOME_MESSAGE']}",
            f"SENSITIVE_KEYWORDS={merged['SENSITIVE_KEYWORDS']}",
            f"MAX_REPLY_CHARS={merged['MAX_REPLY_CHARS']}",
            f"BLOCKED_REPLY_PATTERNS={merged['BLOCKED_REPLY_PATTERNS']}",
            f"DEFAULT_CHAT_POLICY={merged['DEFAULT_CHAT_POLICY']}",
            f"CHAT_POLICIES={merged['CHAT_POLICIES']}",
            f"REPLY_COOLDOWN_SECONDS={merged['REPLY_COOLDOWN_SECONDS']}",
            f"REQUIRE_MANUAL_CONFIRM={merged['REQUIRE_MANUAL_CONFIRM']}",
            f"APPROVAL_MODE={merged['APPROVAL_MODE']}",
            f"PENDING_REPLIES_FILE={merged['PENDING_REPLIES_FILE']}",
            f"MAX_SENDS_PER_RUN={merged['MAX_SENDS_PER_RUN']}",
            f"MAX_SENDS_PER_CHAT={merged['MAX_SENDS_PER_CHAT']}",
            f"MAX_SENDS_PER_DAY={merged['MAX_SENDS_PER_DAY']}",
            f"MAX_SENDS_PER_CHAT_PER_DAY={merged['MAX_SENDS_PER_CHAT_PER_DAY']}",
            f"MAX_ERRORS_BEFORE_PAUSE={merged['MAX_ERRORS_BEFORE_PAUSE']}",
            f"LOG_FILE={merged['LOG_FILE']}",
            f"AUDIT_LOG_FILE={merged['AUDIT_LOG_FILE']}",
            f"ALERT_FILE={merged['ALERT_FILE']}",
            f"LOG_MAX_BYTES={merged['LOG_MAX_BYTES']}",
            f"LOG_BACKUP_COUNT={merged['LOG_BACKUP_COUNT']}",
            f"HEALTH_FILE={merged['HEALTH_FILE']}",
            f"REPORTS_DIR={merged['REPORTS_DIR']}",
            f"STATE_DB_FILE={merged['STATE_DB_FILE']}",
            "",
            "# Knowledge base",
            f"KNOWLEDGE_FILE={merged['KNOWLEDGE_FILE']}",
            f"KNOWLEDGE_TOP_K={merged['KNOWLEDGE_TOP_K']}",
            f"REQUIRE_KNOWLEDGE_MATCH={merged['REQUIRE_KNOWLEDGE_MATCH']}",
            f"KNOWLEDGE_FALLBACK_REPLY={merged['KNOWLEDGE_FALLBACK_REPLY']}",
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
    message_buffer_seconds: int
    dedup_window_seconds: int
    dry_run: bool
    system_prompt: str
    reply_allowed_chats: list[str] = field(default_factory=list)
    reply_blocked_chats: list[str] = field(default_factory=list)
    reply_allowed_senders: list[str] = field(default_factory=list)
    reply_blocked_senders: list[str] = field(default_factory=list)
    text_message_types: list[str] = field(default_factory=lambda: ["text", "friend"])
    voice_to_text_enabled: bool = True
    voice_to_text_timeout_seconds: int = 2
    group_chats_enabled: bool = False
    group_chat_patterns: list[str] = field(default_factory=list)
    dashboard_users: str = "admin:admin123:admin"
    welcome_message_enabled: bool = True
    welcome_message: str = ""
    sensitive_keywords: list[str] = field(default_factory=list)
    max_reply_chars: int = 120
    blocked_reply_patterns: list[str] = field(default_factory=list)
    default_chat_policy: str = "manual"
    chat_policies: dict[str, str] = field(default_factory=dict)
    reply_cooldown_seconds: int = 30
    require_manual_confirm: bool = True
    approval_mode: str = "console"
    pending_replies_file: str = "logs/pending_replies.jsonl"
    max_sends_per_run: int = 20
    max_sends_per_chat: int = 5
    max_sends_per_day: int = 100
    max_sends_per_chat_per_day: int = 20
    max_errors_before_pause: int = 3
    log_file: str = "logs/wechat-ai.log"
    audit_log_file: str = "logs/audit.jsonl"
    alert_file: str = "logs/alerts.log"
    log_max_bytes: int = 10485760
    log_backup_count: int = 5
    health_file: str = "logs/health.json"
    reports_dir: str = "reports"
    state_db_file: str = "data/wechat-ai.sqlite3"
    knowledge_file: str = "knowledge/faq.md"
    knowledge_top_k: int = 3
    require_knowledge_match: bool = False
    knowledge_fallback_reply: str = "这个问题我需要确认一下，稍后回复你。"

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
            "message_buffer_seconds": self.message_buffer_seconds,
            "dedup_window_seconds": self.dedup_window_seconds,
            "dry_run": self.dry_run,
            "reply_allowed_chats": self.reply_allowed_chats,
            "reply_blocked_chats": self.reply_blocked_chats,
            "reply_allowed_senders": self.reply_allowed_senders,
            "reply_blocked_senders": self.reply_blocked_senders,
            "text_message_types": self.text_message_types,
            "voice_to_text_enabled": self.voice_to_text_enabled,
            "voice_to_text_timeout_seconds": self.voice_to_text_timeout_seconds,
            "group_chats_enabled": self.group_chats_enabled,
            "group_chat_patterns": self.group_chat_patterns,
            "dashboard_users_set": bool(self.dashboard_users),
            "welcome_message_enabled": self.welcome_message_enabled,
            "welcome_message_set": bool(self.welcome_message),
            "sensitive_keywords": self.sensitive_keywords,
            "max_reply_chars": self.max_reply_chars,
            "blocked_reply_patterns": self.blocked_reply_patterns,
            "default_chat_policy": self.default_chat_policy,
            "chat_policies": self.chat_policies,
            "reply_cooldown_seconds": self.reply_cooldown_seconds,
            "require_manual_confirm": self.require_manual_confirm,
            "approval_mode": self.approval_mode,
            "pending_replies_file": self.pending_replies_file,
            "max_sends_per_run": self.max_sends_per_run,
            "max_sends_per_chat": self.max_sends_per_chat,
            "max_sends_per_day": self.max_sends_per_day,
            "max_sends_per_chat_per_day": self.max_sends_per_chat_per_day,
            "max_errors_before_pause": self.max_errors_before_pause,
            "log_file": self.log_file,
            "audit_log_file": self.audit_log_file,
            "alert_file": self.alert_file,
            "log_max_bytes": self.log_max_bytes,
            "log_backup_count": self.log_backup_count,
            "health_file": self.health_file,
            "reports_dir": self.reports_dir,
            "state_db_file": self.state_db_file,
            "knowledge_file": self.knowledge_file,
            "knowledge_top_k": self.knowledge_top_k,
            "require_knowledge_match": self.require_knowledge_match,
            "knowledge_fallback_reply": self.knowledge_fallback_reply,
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
    if not config.reply_allowed_chats:
        warnings.append("REPLY_ALLOWED_CHATS is required. Start with a small whitelist such as 文件传输助手.")
    if config.max_context_messages <= 0:
        warnings.append("MAX_CONTEXT_MESSAGES should be greater than 0.")
    if config.message_buffer_seconds < 0:
        warnings.append("MESSAGE_BUFFER_SECONDS should not be negative.")
    if config.dedup_window_seconds < 0:
        warnings.append("DEDUP_WINDOW_SECONDS should not be negative.")
    if config.voice_to_text_timeout_seconds < 0:
        warnings.append("VOICE_TO_TEXT_TIMEOUT_SECONDS should not be negative.")
    if config.request_timeout <= 0:
        warnings.append("REQUEST_TIMEOUT should be greater than 0.")
    if config.max_reply_chars < 0:
        warnings.append("MAX_REPLY_CHARS should not be negative.")
    if config.default_chat_policy not in ALLOWED_CHAT_POLICIES:
        warnings.append("DEFAULT_CHAT_POLICY should be one of auto, manual, dry_run, ignore.")
    invalid_policies = sorted(set(config.chat_policies.values()) - ALLOWED_CHAT_POLICIES)
    if invalid_policies:
        warnings.append(f"CHAT_POLICIES contains invalid policies: {', '.join(invalid_policies)}.")
    if config.reply_cooldown_seconds < 0:
        warnings.append("REPLY_COOLDOWN_SECONDS should not be negative.")
    if config.approval_mode not in ALLOWED_APPROVAL_MODES:
        warnings.append("APPROVAL_MODE should be either 'console' or 'queue'.")
    if config.approval_mode == "queue" and not config.pending_replies_file:
        warnings.append("PENDING_REPLIES_FILE is required when APPROVAL_MODE=queue.")
    if config.max_sends_per_run < 0:
        warnings.append("MAX_SENDS_PER_RUN should not be negative.")
    if config.max_sends_per_chat < 0:
        warnings.append("MAX_SENDS_PER_CHAT should not be negative.")
    if config.max_sends_per_day < 0:
        warnings.append("MAX_SENDS_PER_DAY should not be negative.")
    if config.max_sends_per_chat_per_day < 0:
        warnings.append("MAX_SENDS_PER_CHAT_PER_DAY should not be negative.")
    if config.max_errors_before_pause < 0:
        warnings.append("MAX_ERRORS_BEFORE_PAUSE should not be negative.")
    if config.log_max_bytes < 0:
        warnings.append("LOG_MAX_BYTES should not be negative.")
    if config.log_backup_count < 0:
        warnings.append("LOG_BACKUP_COUNT should not be negative.")
    if config.knowledge_top_k < 0:
        warnings.append("KNOWLEDGE_TOP_K should not be negative.")
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
        message_buffer_seconds=_get_int(
            "MESSAGE_BUFFER_SECONDS",
            int(DEFAULT_ENV_VALUES["MESSAGE_BUFFER_SECONDS"]),
        ),
        dedup_window_seconds=_get_int("DEDUP_WINDOW_SECONDS", int(DEFAULT_ENV_VALUES["DEDUP_WINDOW_SECONDS"])),
        dry_run=_get_bool("DRY_RUN", True),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            DEFAULT_SYSTEM_PROMPT,
        ).strip(),
        reply_allowed_chats=_split_csv(
            os.getenv("REPLY_ALLOWED_CHATS", DEFAULT_ENV_VALUES["REPLY_ALLOWED_CHATS"])
        ),
        reply_blocked_chats=_split_csv(
            os.getenv("REPLY_BLOCKED_CHATS", DEFAULT_ENV_VALUES["REPLY_BLOCKED_CHATS"])
        ),
        reply_allowed_senders=_split_csv(
            os.getenv("REPLY_ALLOWED_SENDERS", DEFAULT_ENV_VALUES["REPLY_ALLOWED_SENDERS"])
        ),
        reply_blocked_senders=_split_csv(
            os.getenv("REPLY_BLOCKED_SENDERS", DEFAULT_ENV_VALUES["REPLY_BLOCKED_SENDERS"])
        ),
        text_message_types=[
            item.lower()
            for item in _split_csv(os.getenv("TEXT_MESSAGE_TYPES", DEFAULT_ENV_VALUES["TEXT_MESSAGE_TYPES"]))
        ],
        voice_to_text_enabled=_get_bool("VOICE_TO_TEXT_ENABLED", True),
        voice_to_text_timeout_seconds=_get_int(
            "VOICE_TO_TEXT_TIMEOUT_SECONDS",
            int(DEFAULT_ENV_VALUES["VOICE_TO_TEXT_TIMEOUT_SECONDS"]),
        ),
        group_chats_enabled=_get_bool("GROUP_CHATS_ENABLED", False),
        group_chat_patterns=_split_csv(os.getenv("GROUP_CHAT_PATTERNS", DEFAULT_ENV_VALUES["GROUP_CHAT_PATTERNS"])),
        dashboard_users=os.getenv("DASHBOARD_USERS", DEFAULT_ENV_VALUES["DASHBOARD_USERS"]).strip(),
        welcome_message_enabled=_get_bool("WELCOME_MESSAGE_ENABLED", True),
        welcome_message=os.getenv("WELCOME_MESSAGE", DEFAULT_ENV_VALUES["WELCOME_MESSAGE"]).strip(),
        sensitive_keywords=_split_csv(
            os.getenv("SENSITIVE_KEYWORDS", DEFAULT_ENV_VALUES["SENSITIVE_KEYWORDS"])
        ),
        max_reply_chars=_get_int("MAX_REPLY_CHARS", int(DEFAULT_ENV_VALUES["MAX_REPLY_CHARS"])),
        blocked_reply_patterns=_split_csv(
            os.getenv("BLOCKED_REPLY_PATTERNS", DEFAULT_ENV_VALUES["BLOCKED_REPLY_PATTERNS"])
        ),
        default_chat_policy=os.getenv(
            "DEFAULT_CHAT_POLICY",
            DEFAULT_ENV_VALUES["DEFAULT_CHAT_POLICY"],
        ).strip().lower(),
        chat_policies={
            key: value.lower()
            for key, value in _split_mapping(
                os.getenv("CHAT_POLICIES", DEFAULT_ENV_VALUES["CHAT_POLICIES"]),
                "CHAT_POLICIES",
            ).items()
        },
        reply_cooldown_seconds=_get_int(
            "REPLY_COOLDOWN_SECONDS",
            int(DEFAULT_ENV_VALUES["REPLY_COOLDOWN_SECONDS"]),
        ),
        require_manual_confirm=_get_bool("REQUIRE_MANUAL_CONFIRM", True),
        approval_mode=os.getenv("APPROVAL_MODE", DEFAULT_ENV_VALUES["APPROVAL_MODE"]).strip().lower(),
        pending_replies_file=os.getenv(
            "PENDING_REPLIES_FILE",
            DEFAULT_ENV_VALUES["PENDING_REPLIES_FILE"],
        ).strip(),
        max_sends_per_run=_get_int("MAX_SENDS_PER_RUN", int(DEFAULT_ENV_VALUES["MAX_SENDS_PER_RUN"])),
        max_sends_per_chat=_get_int("MAX_SENDS_PER_CHAT", int(DEFAULT_ENV_VALUES["MAX_SENDS_PER_CHAT"])),
        max_sends_per_day=_get_int("MAX_SENDS_PER_DAY", int(DEFAULT_ENV_VALUES["MAX_SENDS_PER_DAY"])),
        max_sends_per_chat_per_day=_get_int(
            "MAX_SENDS_PER_CHAT_PER_DAY",
            int(DEFAULT_ENV_VALUES["MAX_SENDS_PER_CHAT_PER_DAY"]),
        ),
        max_errors_before_pause=_get_int(
            "MAX_ERRORS_BEFORE_PAUSE",
            int(DEFAULT_ENV_VALUES["MAX_ERRORS_BEFORE_PAUSE"]),
        ),
        log_file=os.getenv("LOG_FILE", DEFAULT_ENV_VALUES["LOG_FILE"]).strip(),
        audit_log_file=os.getenv("AUDIT_LOG_FILE", DEFAULT_ENV_VALUES["AUDIT_LOG_FILE"]).strip(),
        alert_file=os.getenv("ALERT_FILE", DEFAULT_ENV_VALUES["ALERT_FILE"]).strip(),
        log_max_bytes=_get_int("LOG_MAX_BYTES", int(DEFAULT_ENV_VALUES["LOG_MAX_BYTES"])),
        log_backup_count=_get_int("LOG_BACKUP_COUNT", int(DEFAULT_ENV_VALUES["LOG_BACKUP_COUNT"])),
        health_file=os.getenv("HEALTH_FILE", DEFAULT_ENV_VALUES["HEALTH_FILE"]).strip(),
        reports_dir=os.getenv("REPORTS_DIR", DEFAULT_ENV_VALUES["REPORTS_DIR"]).strip(),
        state_db_file=os.getenv("STATE_DB_FILE", DEFAULT_ENV_VALUES["STATE_DB_FILE"]).strip(),
        knowledge_file=os.getenv("KNOWLEDGE_FILE", DEFAULT_ENV_VALUES["KNOWLEDGE_FILE"]).strip(),
        knowledge_top_k=_get_int("KNOWLEDGE_TOP_K", int(DEFAULT_ENV_VALUES["KNOWLEDGE_TOP_K"])),
        require_knowledge_match=_get_bool("REQUIRE_KNOWLEDGE_MATCH", False),
        knowledge_fallback_reply=os.getenv(
            "KNOWLEDGE_FALLBACK_REPLY",
            DEFAULT_ENV_VALUES["KNOWLEDGE_FALLBACK_REPLY"],
        ).strip(),
    )
