from __future__ import annotations

from dataclasses import dataclass, field

from wechat_ai.bot import WeChatAutoReplyBot
from wechat_ai.config import AppConfig


@dataclass
class SimulatedMessage:
    content: str
    sender: str = "simulated-user"
    type: str = "text"


@dataclass
class SimulatedChat:
    who: str
    sent: list[str] = field(default_factory=list)

    def SendMsg(self, message: str) -> None:
        self.sent.append(message)


class StaticReplyClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate_reply(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        return self.reply


def simulate_message(
    config: AppConfig,
    llm_client: object,
    chat_name: str,
    sender: str,
    content: str,
    msg_type: str = "text",
) -> dict[str, object]:
    chat = SimulatedChat(who=chat_name)
    message = SimulatedMessage(content=content, sender=sender, type=msg_type)
    bot = WeChatAutoReplyBot(config=config, llm_client=llm_client)  # type: ignore[arg-type]
    bot._on_message(message, chat)
    return {
        "chat_name": chat_name,
        "sender": sender,
        "msg_type": msg_type,
        "content": content,
        "dry_run": config.dry_run,
        "approval_mode": config.approval_mode,
        "sent_messages": chat.sent,
        "sent_count": bot.sent_count,
    }
