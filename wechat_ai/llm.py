from __future__ import annotations

import json
from typing import Any
from urllib import request, error

from wechat_ai.config import AppConfig
from wechat_ai.utils import build_history_prompt, strip_think_tags


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def generate_reply(self, chat_name: str, history: list[dict[str, str]]) -> str:
        if self.config.llm_provider == "openai_compatible":
            reply = self._call_openai_compatible(chat_name=chat_name, history=history)
        else:
            reply = self._call_ollama(chat_name=chat_name, history=history)
        return strip_think_tags(reply)

    def _call_ollama(self, chat_name: str, history: list[dict[str, str]]) -> str:
        prompt = build_history_prompt(
            system_prompt=self.config.system_prompt,
            chat_name=chat_name,
            history=history,
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

    def _call_openai_compatible(self, chat_name: str, history: list[dict[str, str]]) -> str:
        if not self.config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai_compatible")

        messages: list[dict[str, str]] = [{"role": "system", "content": self.config.system_prompt}]
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
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": 0.7,
        }
        response = self._post_json(
            url=f"{self.config.openai_base_url}/chat/completions",
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

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=data, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.config.request_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code} when calling {url}: {body}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"Failed to call {url}: {exc}. Run `python main.py doctor` to check your LLM settings."
            ) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON response from {url}: {raw[:500]}") from exc
