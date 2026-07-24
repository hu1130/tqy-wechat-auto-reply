from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from wechat_ai.config import AppConfig
from wechat_ai.llm import LLMClient
from wechat_ai.tenant_auth import SecretCipher
from wechat_ai.tenant_store import ReplyJob, TenantStore


LLMFactory = Callable[[AppConfig], Any]
BOT_SETTING_TYPES: dict[str, type] = {
    "enabled": bool,
    "llm_provider": str,
    "llm_model": str,
    "openai_base_url": str,
    "ollama_base_url": str,
    "request_timeout": int,
    "max_context_messages": int,
    "api_credential_name": str,
}


def validate_bot_settings(settings: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(settings) - set(BOT_SETTING_TYPES))
    if unknown:
        raise ValueError(f"unknown bot profile settings: {', '.join(unknown)}")
    for name, value in settings.items():
        expected = BOT_SETTING_TYPES[name]
        if type(value) is not expected:
            raise ValueError(f"bot profile setting {name} has an invalid type")
        if expected is str and not value.strip():
            raise ValueError(f"bot profile setting {name} must not be empty")
        if name in {"request_timeout", "max_context_messages"} and value <= 0:
            raise ValueError(f"bot profile setting {name} must be positive")
    if settings.get("llm_provider") not in {None, "ollama", "openai_compatible", "ai_cs"}:
        raise ValueError("bot profile setting llm_provider is invalid")
    return dict(settings)


class TenantConfigurationError(ValueError):
    pass


class AICSReplyClient:
    def __init__(self, config: AppConfig) -> None:
        base_url = str(config.openai_base_url or "").strip().rstrip("/")
        api_key = str(config.openai_api_key or "").strip()
        try:
            parsed = httpx.URL(base_url)
        except (httpx.InvalidURL, ValueError) as error:
            raise TenantConfigurationError("AI-cs callback URL is invalid") from error
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise TenantConfigurationError("AI-cs callback URL is invalid")
        if not api_key:
            raise TenantConfigurationError("AI-cs callback credential is missing")
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=f"{base_url}/",
            timeout=httpx.Timeout(config.request_timeout),
            follow_redirects=False,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    def generate_reply(self, **values: object) -> str:
        payload = {
            "channel_account_id": values.get("channel_account_id"),
            "conversation_id": values.get("conversation_id"),
            "contact_external_id": values.get("contact_external_id"),
            "contact_display_name": values.get("contact_display_name") or "",
            "source_message_id": values.get("source_message_id"),
            "message": values.get("message"),
            "message_type": values.get("message_type") or "text",
        }
        try:
            response = self._client.post("v1/personal-wechat/reply", json=payload)
        except httpx.TimeoutException as error:
            raise RuntimeError("AI-cs reply request timed out") from error
        except httpx.RequestError as error:
            raise RuntimeError("AI-cs reply request failed") from error
        if response.status_code >= 400:
            raise RuntimeError(f"AI-cs reply request returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError("AI-cs reply response was not valid JSON") from error
        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list):
            raise RuntimeError("AI-cs reply response did not contain messages")
        replies = [
            str(item.get("text") or "").strip()
            for item in messages
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if not replies:
            raise RuntimeError("AI-cs returned an empty reply")
        return "\n".join(replies)


class CenterReplyWorker:
    def __init__(
        self,
        store: TenantStore,
        config: AppConfig,
        secret_cipher: SecretCipher,
        *,
        llm_factory: LLMFactory = LLMClient,
        ai_cs_factory: LLMFactory = AICSReplyClient,
        claim_limit: int = 10,
        lease_seconds: int = 300,
        retry_delay_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.config = config
        self.secret_cipher = secret_cipher
        self.llm_factory = llm_factory
        self.ai_cs_factory = ai_cs_factory
        self.claim_limit = claim_limit
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self._clients: dict[str, tuple[tuple[object, ...], Any]] = {}
        self._health_lock = threading.Lock()
        self._running = False
        self._last_success_monotonic: float | None = None
        self._last_success_at: str | None = None
        self._consecutive_failures = 0

    def process_once(self) -> dict[str, int]:
        stats = {"claimed": 0, "generated": 0, "failed": 0}
        for tenant_id in self.store.list_active_tenant_ids():
            try:
                tenant_config = self._tenant_config(tenant_id)
            except Exception as exc:
                self._event(
                    "center_reply_tenant_config_error",
                    tenant_id=tenant_id,
                    error=str(exc),
                )
                continue
            if not tenant_config["enabled"]:
                continue
            jobs = self.store.claim_reply_jobs(
                tenant_id,
                limit=self.claim_limit,
                lease_seconds=self.lease_seconds,
            )
            stats["claimed"] += len(jobs)
            for job in jobs:
                if self._process_job(tenant_id, job, tenant_config["config"]):
                    stats["generated"] += 1
                else:
                    stats["failed"] += 1
        return stats

    def run_forever(
        self,
        *,
        interval_seconds: float = 1.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        with self._health_lock:
            self._running = True
        try:
            while not stop.is_set():
                try:
                    stats = self.process_once()
                    self._record_loop_success()
                    if stats["claimed"]:
                        self._event("center_reply_batch", **stats)
                except Exception as exc:
                    self._record_loop_failure()
                    self._event("center_reply_worker_error", error=str(exc))
                stop.wait(interval_seconds)
        finally:
            with self._health_lock:
                self._running = False

    def health_report(self, *, max_staleness_seconds: float = 15.0) -> dict[str, object]:
        with self._health_lock:
            running = self._running
            last_success_monotonic = self._last_success_monotonic
            last_success_at = self._last_success_at
            consecutive_failures = self._consecutive_failures
        age_seconds = (
            None
            if last_success_monotonic is None
            else max(0.0, time.monotonic() - last_success_monotonic)
        )
        healthy = bool(
            running
            and age_seconds is not None
            and age_seconds <= max_staleness_seconds
        )
        return {
            "ok": healthy,
            "running": running,
            "last_success_at": last_success_at,
            "last_success_age_seconds": age_seconds,
            "consecutive_failures": consecutive_failures,
        }

    def _record_loop_success(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._health_lock:
            self._last_success_monotonic = time.monotonic()
            self._last_success_at = now
            self._consecutive_failures = 0

    def _record_loop_failure(self) -> None:
        with self._health_lock:
            self._consecutive_failures += 1

    def _process_job(
        self,
        tenant_id: str,
        job: ReplyJob,
        tenant_config: AppConfig,
    ) -> bool:
        lease_token = str(job.lease_token or "")
        try:
            _, message, conversation, messages = self.store.get_reply_job_context(
                tenant_id,
                job.id,
                lease_token,
                max_messages=max(1, tenant_config.max_context_messages),
            )
            history = [
                {
                    "role": "assistant" if item.direction == "outbound" else "user",
                    "sender": item.sender_external_id or "customer",
                    "content": item.content,
                }
                for item in messages
                if item.content.strip()
            ]
            client = self._client_for(tenant_id, tenant_config)
            knowledge = self.store.get_knowledge_base(tenant_id)
            knowledge_context = ""
            if knowledge is not None and knowledge.status == "active":
                knowledge_context = knowledge.content[:20_000]
            if tenant_config.llm_provider == "ai_cs":
                reply = str(
                    client.generate_reply(
                        channel_account_id=message.channel_account_id,
                        conversation_id=conversation.external_conversation_id,
                        contact_external_id=message.sender_external_id,
                        contact_display_name=conversation.customer_display_name,
                        source_message_id=message.external_message_id,
                        message=message.content,
                        message_type=message.message_type,
                    )
                ).strip()
            else:
                reply = str(
                    client.generate_reply(
                        chat_name=conversation.external_conversation_id,
                        history=history,
                        knowledge_context=knowledge_context,
                    )
                ).strip()
            if not reply:
                raise RuntimeError("AI returned an empty reply")
            self.store.complete_reply_job(
                tenant_id,
                job.id,
                lease_token,
                reply,
            )
            self._event(
                "center_reply_generated",
                tenant_id=tenant_id,
                message_id=message.id,
                reply_job_id=job.id,
            )
            return True
        except Exception as exc:
            error_text = str(exc)
            if tenant_config.openai_api_key:
                error_text = error_text.replace(
                    tenant_config.openai_api_key,
                    "[REDACTED]",
                )
            max_attempts = (
                1
                if isinstance(exc, (TenantConfigurationError, ValueError))
                else max(1, self.config.api_retry_attempts)
            )
            self.store.fail_reply_job(
                tenant_id,
                job.id,
                lease_token,
                error_text,
                max_attempts=max_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            self._event(
                "center_reply_failed",
                tenant_id=tenant_id,
                reply_job_id=job.id,
                error=error_text,
            )
            return False

    def _tenant_config(self, tenant_id: str) -> dict[str, object]:
        profile = self.store.get_bot_profile(tenant_id)
        settings = validate_bot_settings(
            dict(profile.settings_json) if profile is not None else {}
        )
        enabled = settings.pop("enabled", profile is not None)
        if type(enabled) is not bool:
            raise ValueError("bot profile setting enabled must be a boolean")
        if not enabled:
            return {"enabled": False, "config": self.config}

        overrides: dict[str, object] = {}
        if profile is not None:
            overrides["system_prompt"] = profile.prompt
        allowed_settings = (
            "llm_provider",
            "llm_model",
            "openai_base_url",
            "ollama_base_url",
            "request_timeout",
            "max_context_messages",
        )
        for name in allowed_settings:
            if name not in settings:
                continue
            value = settings[name]
            overrides[name] = value

        credential_name = settings.get("api_credential_name")
        if credential_name is not None:
            credential = self.store.get_api_credential(
                tenant_id,
                "llm",
                credential_name.strip(),
            )
            if credential is None:
                raise TenantConfigurationError("tenant LLM credential was not found")
            overrides["openai_api_key"] = self.secret_cipher.decrypt(
                credential.encrypted_value
            )
        return {"enabled": enabled, "config": self.config.with_overrides(**overrides)}

    def _client_for(self, tenant_id: str, config: AppConfig) -> Any:
        signature = (
            config.llm_provider,
            config.llm_model,
            config.openai_base_url,
            config.ollama_base_url,
            config.openai_api_key,
            config.system_prompt,
            config.request_timeout,
            config.api_retry_attempts,
            config.api_retry_base_delay_seconds,
            config.api_retry_max_delay_seconds,
            config.api_circuit_failure_threshold,
            config.api_circuit_recovery_seconds,
            config.api_min_interval_seconds,
        )
        cached = self._clients.get(tenant_id)
        if cached is None or cached[0] != signature:
            factory = self.ai_cs_factory if config.llm_provider == "ai_cs" else self.llm_factory
            cached = (signature, factory(config))
            self._clients[tenant_id] = cached
        return cached[1]

    @staticmethod
    def _event(event: str, **values: object) -> None:
        print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


__all__ = ["AICSReplyClient", "CenterReplyWorker", "validate_bot_settings"]
