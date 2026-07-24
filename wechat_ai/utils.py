from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import re
import time


THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_tags(text: str) -> str:
    cleaned = THINK_TAG_RE.sub("", text)
    return cleaned.strip()


def build_history_prompt(
    system_prompt: str,
    chat_name: str,
    history: list[dict[str, str]],
    knowledge_context: str = "",
    knowledge_fallback_reply: str = "",
) -> str:
    lines = [system_prompt, "", f"当前聊天对象: {chat_name}", "最近消息:"]
    if knowledge_context:
        lines.extend(
            [
                "",
                "公司知识库摘录:",
                knowledge_context,
                "",
                "只能基于公司知识库摘录和聊天上下文回复。"
                f"如果资料不足，回复：{knowledge_fallback_reply or '我确认后回复你。'}",
            ]
        )
    for item in history:
        role = item.get("role", "user")
        sender = item.get("sender", role)
        content = item.get("content", "").strip()
        if not content:
            continue
        lines.append(f"[{role}|{sender}] {content}")
    lines.extend(
        [
            "",
            "请基于以上上下文，直接输出下一条微信回复。",
            "要求：简短、自然、像真人发送，不要解释，不要加引号，不要输出思考过程。",
        ]
    )
    return "\n".join(lines)


@dataclass
class MessageDeduplicator:
    ttl_seconds: int
    _seen: dict[str, float] = field(default_factory=dict)

    def is_duplicate(self, chat_name: str, sender: str, content: str, msg_type: str) -> bool:
        now = time.time()
        self._purge(now)
        fingerprint = hashlib.sha1(
            f"{chat_name}|{sender}|{msg_type}|{content}".encode("utf-8")
        ).hexdigest()
        seen_at = self._seen.get(fingerprint)
        if seen_at is not None and now - seen_at <= self.ttl_seconds:
            return True
        self._seen[fingerprint] = now
        return False

    def _purge(self, now: float) -> None:
        expired = [
            key
            for key, seen_at in self._seen.items()
            if now - seen_at > self.ttl_seconds
        ]
        for key in expired:
            self._seen.pop(key, None)


def append_history(history: deque[dict[str, str]], role: str, sender: str, content: str) -> None:
    history.append({"role": role, "sender": sender, "content": content})
