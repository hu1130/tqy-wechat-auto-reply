from __future__ import annotations

import json
import time
from typing import Any
from urllib import request, error

from wechat_ai.config import AppConfig
from wechat_ai.reliability import CircuitBreaker, RetryPolicy, TokenBucketRateLimiter
from wechat_ai.utils import build_history_prompt, strip_think_tags


RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


class LLMRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._retry_policy = RetryPolicy(
            max_attempts=max(1, int(getattr(config, "api_retry_attempts", 4))),
            base_delay=float(getattr(config, "api_retry_base_delay_seconds", 1.0)),
            max_delay=float(getattr(config, "api_retry_max_delay_seconds", 30.0)),
            should_retry=lambda exc: isinstance(exc, LLMRequestError) and exc.retryable,
            sleep=lambda delay: time.sleep(delay),
        )
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=max(1, int(getattr(config, "api_circuit_failure_threshold", 5))),
            recovery_timeout=float(getattr(config, "api_circuit_recovery_seconds", 60.0)),
        )
        min_interval = float(getattr(config, "api_min_interval_seconds", 0.0))
        self._rate_limiter = (
            TokenBucketRateLimiter(rate=1.0 / min_interval, capacity=1.0)
            if min_interval > 0
            else None
        )

    def generate_reply(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        if self.config.llm_provider == "openai_compatible":
            reply = self._call_openai_compatible(
                chat_name=chat_name,
                history=history,
                knowledge_context=knowledge_context,
            )
        else:
            reply = self._call_ollama(
                chat_name=chat_name,
                history=history,
                knowledge_context=knowledge_context,
            )
        return strip_think_tags(reply)

    def _call_ollama(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        prompt = build_history_prompt(
            system_prompt=self.config.system_prompt,
            chat_name=chat_name,
            history=history,
            knowledge_context=knowledge_context,
            knowledge_fallback_reply=self.config.knowledge_fallback_reply,
        )
        payload = {
            "model": self.config.llm_model,
            "prompt": prompt,
            "stream": False,
        }
        response = self._post_json(
            url=f"{self.config.ollama_base_url}/api/generate",
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
        return str(response.get("response", "")).strip()

    def _call_openai_compatible(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        if not self.config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai_compatible")

        if self.config.openai_base_url.rstrip("/").endswith("/chat"):
            return self._call_session_chat_api(
                chat_name=chat_name,
                history=history,
                knowledge_context=knowledge_context,
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": self.config.system_prompt}]
        if knowledge_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "公司知识库摘录如下。只能基于这些资料和聊天上下文回复。"
                        f"如果资料不足，回复：{self.config.knowledge_fallback_reply}\n\n"
                        f"{knowledge_context}"
                    ),
                }
            )
        for item in history:
            role = item.get("role", "user")
            content = item.get("content", "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append(
            {
                "role": "user",
                "content": f"当前聊天对象是：{chat_name}。请直接输出下一条微信回复正文。",
            }
        )

        payload = {
            "messages": messages,
            "temperature": 0.7,
        }
        if self.config.llm_model:
            payload["model"] = self.config.llm_model
        response = self._post_json(
            url=self._openai_chat_url(),
            payload=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.openai_api_key}",
            },
        )
        choices = response.get("choices", [])
        if not choices:
            raise RuntimeError("OpenAI-compatible response has no choices")
        message = choices[0].get("message", {})
        return str(message.get("content", "")).strip()

    def _call_session_chat_api(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        message = self._latest_user_message(history) or build_history_prompt(
            system_prompt=self.config.system_prompt,
            chat_name=chat_name,
            history=history,
            knowledge_context=knowledge_context,
            knowledge_fallback_reply=self.config.knowledge_fallback_reply,
        )
        payload = {
            "api_key": self.config.openai_api_key,
            "session_id": chat_name,
            "message": message,
            "preferred_language": "zh-CN",
        }
        response = self._post_json(
            url=self._openai_chat_url(),
            payload=payload,
            headers={
                "Content-Type": "application/json",
            },
        )
        return self._extract_reply_text(response)

    def _build_session_chat_message(
        self,
        chat_name: str,
        history: list[dict[str, str]],
        knowledge_context: str = "",
    ) -> str:
        recent_items = [
            item
            for item in history
            if str(item.get("content", "")).strip()
        ]
        latest_user_message = self._latest_user_message(recent_items)
        if not latest_user_message:
            return ""

        lines = [
            "Recent WeChat conversation context is below.",
            "Reply only to the latest user message. Do not repeat the context.",
            "Keep the reply short, natural, and suitable to send directly in WeChat.",
            "If the answer is better as multiple WeChat messages, separate them with newlines. Do not number them.",
            f"Chat: {chat_name}",
        ]
        if knowledge_context:
            lines.extend(
                [
                    "",
                    "Company knowledge:",
                    knowledge_context,
                ]
            )
        lines.append("")
        lines.append("Conversation:")
        for item in recent_items:
            role = str(item.get("role", "user")).strip() or "user"
            sender = str(item.get("sender", role)).strip() or role
            content = str(item.get("content", "")).strip()
            lines.append(f"[{role}|{sender}] {content}")
        lines.extend(["", f"Latest user message: {latest_user_message}"])
        return "\n".join(lines)

    @staticmethod
    def _latest_user_message(history: list[dict[str, str]]) -> str:
        for item in reversed(history):
            if item.get("role") == "user":
                content = item.get("content", "").strip()
                if content:
                    return content
        return ""

    def _extract_reply_text(self, response: dict[str, Any]) -> str:
        for key in ("reply", "response", "answer", "content", "text", "message"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = self._extract_reply_text(value)
                if nested:
                    return nested
        data = response.get("data")
        if isinstance(data, dict):
            nested = self._extract_reply_text(data)
            if nested:
                return nested
        messages = response.get("messages")
        if isinstance(messages, list):
            texts = [
                str(item.get("text", "")).strip()
                for item in messages
                if isinstance(item, dict) and str(item.get("text", "")).strip()
            ]
            if texts:
                return "\n".join(texts)
        raise RuntimeError(f"Chat API response has no reply text: {json.dumps(response, ensure_ascii=False)[:500]}")

    def _openai_chat_url(self) -> str:
        base_url = self.config.openai_base_url.rstrip("/")
        if base_url.endswith(("/chat", "/chat/completions")):
            return base_url
        return f"{base_url}/chat/completions"

    @staticmethod
    def _read_http_error_body(exc: error.HTTPError) -> str:
        try:
            raw = exc.read()
        except Exception:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="ignore")
        return str(raw)

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=data, headers=headers, method="POST")
        return self._circuit_breaker.call(
            lambda: self._retry_policy.call(
                lambda: self._open_json_request(req, url),
                delay_override=lambda exc, retry_number, default: getattr(exc, "retry_after", None),
            )
        )

    def _open_json_request(self, req: request.Request, url: str) -> dict[str, Any]:
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()
        try:
            with request.urlopen(req, timeout=self.config.request_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = self._read_http_error_body(exc)
            raise LLMRequestError(
                f"HTTP {exc.code} when calling {url}: {body}",
                retryable=exc.code in RETRYABLE_HTTP_STATUS,
                retry_after=self._retry_after_seconds(exc),
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LLMRequestError(
                f"Failed to call {url}: {exc}. Run `python main.py doctor` to check your LLM settings.",
                retryable=True,
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMRequestError(
                f"Invalid JSON response from {url}: {raw[:500]}",
                retryable=True,
            ) from exc

    @staticmethod
    def _retry_after_seconds(exc: error.HTTPError) -> float | None:
        headers = getattr(exc, "headers", None)
        if headers is None:
            return None
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None
