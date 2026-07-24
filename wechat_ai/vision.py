from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from wechat_ai.config import AppConfig
from wechat_ai.reliability import CircuitBreaker, RetryPolicy, TokenBucketRateLimiter


RETRYABLE_VISION_HTTP_STATUS = {429, 500, 502, 503, 504}


class VisionRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class VisionClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._retry_policy = RetryPolicy(
            max_attempts=max(1, int(getattr(config, "api_retry_attempts", 4))),
            base_delay=float(getattr(config, "api_retry_base_delay_seconds", 1.0)),
            max_delay=float(getattr(config, "api_retry_max_delay_seconds", 30.0)),
            should_retry=lambda exc: isinstance(exc, VisionRequestError) and exc.retryable,
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

    def is_configured(self) -> bool:
        return bool(
            self.config.vision_provider == "openai_compatible"
            and self.config.vision_base_url
            and self.config.vision_api_key
            and self.config.vision_model
        )

    def describe_image(self, image_path: str | Path, prompt: str = "") -> str:
        if not self.is_configured():
            return ""
        if self.config.vision_provider != "openai_compatible":
            raise RuntimeError(f"Unsupported VISION_PROVIDER: {self.config.vision_provider}")

        path = Path(image_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"Image file does not exist: {path}")

        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        user_prompt = prompt or self.config.vision_prompt
        payload = {
            "model": self.config.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}",
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }
        response = self._post_json(
            url=self._chat_url(),
            payload=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.vision_api_key}",
            },
        )
        return clean_vision_text(self._extract_text(response))

    def _chat_url(self) -> str:
        base_url = self.config.vision_base_url.rstrip("/")
        if base_url.endswith(("/chat/completions", "/responses")):
            return base_url
        return f"{base_url}/chat/completions"

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url=url, data=data, headers=headers, method="POST")
        return self._circuit_breaker.call(
            lambda: self._retry_policy.call(
                lambda: self._open_json_request(req),
                delay_override=lambda exc, retry_number, default: getattr(exc, "retry_after", None),
            )
        )

    def _open_json_request(self, req: request.Request) -> dict[str, Any]:
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()
        try:
            with request.urlopen(req, timeout=self.config.vision_timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise VisionRequestError(
                f"HTTP {exc.code} when calling vision model: {body[:500]}",
                retryable=exc.code in RETRYABLE_VISION_HTTP_STATUS,
                retry_after=self._retry_after_seconds(exc),
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise VisionRequestError(f"Failed to call vision model: {exc}", retryable=True) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VisionRequestError(
                f"Invalid JSON response from vision model: {raw[:500]}",
                retryable=True,
            ) from exc

    @staticmethod
    def _retry_after_seconds(exc: error.HTTPError) -> float | None:
        headers = getattr(exc, "headers", None)
        if headers is None:
            return None
        value = headers.get("Retry-After")
        try:
            return max(0.0, float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                texts = [
                    str(item.get("text", "")).strip()
                    for item in content
                    if isinstance(item, dict) and str(item.get("text", "")).strip()
                ]
                if texts:
                    return "\n".join(texts)

        output = response.get("output")
        if isinstance(output, list):
            texts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content", []) or []:
                    if isinstance(part, dict) and str(part.get("text", "")).strip():
                        texts.append(str(part["text"]).strip())
            if texts:
                return "\n".join(texts)
        return ""


def clean_vision_text(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    remove_patterns = [
        r"由于图片分辨率限制[^。！？\n]*(?:。|！|？)?",
        r"部分文字细节可能不够清晰[^。！？\n]*(?:。|！|？)?",
        r"如果有更多具体需求[^。！？\n]*(?:。|！|？)?",
        r"如需(?:更多|进一步)[^。！？\n]*(?:。|！|？)?",
        r"请告知[^。！？\n]*(?:。|！|？)?",
        r"以上信息[^。！？\n]*(?:。|！|？)?",
    ]
    for pattern in remove_patterns:
        text = re.sub(pattern, "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip(" ，。；;\n")
