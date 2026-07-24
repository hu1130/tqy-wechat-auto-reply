from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from wechat_ai.center_admin_web import read_center_admin_asset
from wechat_ai.center_agent import (
    AgentHeartbeat,
    ContactSnapshot,
    DeliveryCommand,
    DeliveryResult,
    DeliveryStart,
    FriendRequestSnapshot,
    InboundEnvelope,
    OperationCommand,
    OperationResult,
    OperationStart,
)
from wechat_ai.center_worker import validate_bot_settings
from wechat_ai.tenant_auth import AuthenticationFailed
from wechat_ai.tenant_store import ProvisioningConflictError


MAX_REQUEST_BYTES = 1024 * 1024
TenantAuthenticator = Callable[[str, str, str], Any]
AgentAuthenticator = Callable[[str, str, str, str], Any]


@dataclass(frozen=True)
class CenterResponse:
    status: int
    body: dict[str, Any]


class CenterRequestError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CenterApplication:
    def __init__(
        self,
        store: Any,
        authenticate_tenant: TenantAuthenticator,
        authenticate_agent: AgentAuthenticator,
        secret_cipher: Any | None = None,
        *,
        activation_security: Any | None = None,
        provisioning_security: Any | None = None,
        readiness_probe: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.authenticate_tenant = authenticate_tenant
        self.authenticate_agent = authenticate_agent
        self.secret_cipher = secret_cipher
        self.activation_security = activation_security
        self.provisioning_security = provisioning_security
        self.readiness_probe = readiness_probe

    def dispatch(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
    ) -> CenterResponse:
        method = method.upper()
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        data = dict(payload or {})
        try:
            if method == "GET" and path == "/health":
                try:
                    healthy = bool(self.store.healthcheck())
                except Exception:
                    healthy = False
                return CenterResponse(
                    200 if healthy else 503,
                    {"ok": healthy, "status": "running" if healthy else "unavailable"},
                )
            if method == "GET" and path == "/livez":
                return CenterResponse(200, {"ok": True, "status": "running"})
            if method == "GET" and path == "/readyz":
                if self.readiness_probe is None:
                    return CenterResponse(
                        503,
                        {"ok": False, "status": "not_ready", "checks": {}},
                    )
                try:
                    report = dict(self.readiness_probe())
                except Exception:
                    report = {"ok": False, "checks": {}}
                ready = bool(report.get("ok"))
                report["status"] = "ready" if ready else "not_ready"
                return CenterResponse(200 if ready else 503, report)
            if path == "/v1/provisioning/personal-wechat":
                return self._dispatch_personal_wechat_provisioning(
                    method, headers, data
                )
            if path.startswith("/v1/agent/"):
                return self._dispatch_agent(method, path, headers, data)
            if path.startswith("/v1/"):
                return self._dispatch_tenant(method, path, parsed.query, headers, data)
            raise CenterRequestError(404, "not_found")
        except CenterRequestError as exc:
            return CenterResponse(exc.status, {"ok": False, "error": str(exc)})
        except AuthenticationFailed:
            return CenterResponse(401, {"ok": False, "error": "unauthorized"})
        except PermissionError:
            return CenterResponse(403, {"ok": False, "error": "forbidden"})
        except ProvisioningConflictError:
            return CenterResponse(
                409, {"ok": False, "error": "provisioning_contract_conflict"}
            )
        except LookupError:
            return CenterResponse(404, {"ok": False, "error": "not_found"})
        except (KeyError, TypeError, ValueError) as exc:
            return CenterResponse(400, {"ok": False, "error": str(exc) or "invalid_request"})

    def _dispatch_personal_wechat_provisioning(
        self,
        method: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> CenterResponse:
        if method != "POST":
            raise CenterRequestError(405, "method_not_allowed")
        if self.provisioning_security is None:
            raise CenterRequestError(503, "provisioning_unavailable")
        if set(payload) != {"external_tenant_id", "tenant_name", "owner_email"}:
            raise ValueError(
                "provisioning request must contain exactly external_tenant_id, "
                "tenant_name, and owner_email"
            )
        token = _bearer_token(headers)
        if not self.provisioning_security.authenticate(token):
            raise CenterRequestError(401, "unauthorized")
        external_tenant_id = _required_single_line_text(
            payload, "external_tenant_id", maximum=128
        )
        tenant_name = _required_single_line_text(
            payload, "tenant_name", maximum=200
        )
        owner_email = _required_single_line_text(
            payload, "owner_email", maximum=320
        ).lower()
        if (
            "@" not in owner_email
            or owner_email.startswith("@")
            or owner_email.endswith("@")
            or any(character.isspace() for character in owner_email)
        ):
            raise ValueError("owner_email is invalid")
        expected_idempotency_key = f"personal-wechat:{external_tenant_id}"
        if _require_idempotency_key(headers) != expected_idempotency_key:
            raise CenterRequestError(
                400,
                "Idempotency-Key must equal personal-wechat:<external_tenant_id>",
            )

        material = self.provisioning_security.derive(external_tenant_id)
        tenant, _owner, channel, agent, created = (
            self.store.provision_personal_wechat_bundle(
                tenant_slug=material.tenant_slug,
                tenant_name=tenant_name,
                owner_email=owner_email,
                channel_external_account_id=material.channel_external_account_id,
                agent_external_id=material.agent_external_id,
                tenant_key_id=material.tenant_key_record.key_id,
                tenant_key_prefix=material.tenant_key_record.prefix,
                tenant_key_hash=material.tenant_key_record.key_hash,
            )
        )
        return CenterResponse(
            201 if created else 200,
            {
                "center_tenant_id": str(tenant.id),
                "center_tenant_api_key": material.tenant_api_key,
                "channel_account_id": str(channel.id),
                "agent_id": str(agent.id),
                "created": created,
            },
        )

    def _dispatch_agent(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> CenterResponse:
        if method != "POST":
            raise CenterRequestError(405, "method_not_allowed")

        if path == "/v1/agent/activate":
            if self.activation_security is None:
                raise CenterRequestError(503, "agent_activation_unavailable")
            activation_code = _required_single_line_text(
                payload, "activation_code", maximum=128
            )
            device_id = _required_single_line_text(payload, "device_id", maximum=255)
            device_name = _required_single_line_text(payload, "device_name", maximum=200)
            agent_version = _required_single_line_text(
                payload, "agent_version", maximum=100
            )
            if not activation_code.startswith("wxact_"):
                raise CenterRequestError(400, "activation_failed")
            try:
                code_hash = self.activation_security.hash_activation_code(activation_code)
                device_hash = self.activation_security.hash_device_id(device_id)
                issued_token = self.activation_security.issue_agent_token()
                activation, agent = self.store.consume_agent_activation(
                    code_hash=code_hash,
                    device_hash=device_hash,
                    device_name=device_name,
                    agent_version=agent_version,
                    agent_key_id=issued_token.record.key_id,
                    agent_key_prefix=issued_token.record.prefix,
                    agent_key_hash=issued_token.record.key_hash,
                )
            except (LookupError, TypeError, ValueError) as exc:
                raise CenterRequestError(400, "activation_failed") from exc
            return CenterResponse(
                200,
                {
                    "tenant_id": str(activation.tenant_id),
                    "agent_instance_id": str(agent.id),
                    "channel_account_id": str(activation.channel_account_id),
                    "agent_token": issued_token.secret,
                },
            )

        token = _bearer_token(headers)

        if path == "/v1/agent/heartbeat":
            heartbeat = AgentHeartbeat.from_dict(payload)
            self.authenticate_agent(
                heartbeat.tenant_id,
                heartbeat.agent_id,
                heartbeat.channel_account_id,
                token,
            )
            observed_at = _parse_timestamp(heartbeat.observed_at)
            identity = _normalize_wechat_identity(
                getattr(heartbeat, "wechat_identity", {})
            )
            identity_fingerprint = ""
            if identity:
                if self.activation_security is None:
                    raise CenterRequestError(503, "wechat_identity_unavailable")
                identity_fingerprint = self.activation_security.hash_wechat_identity(
                    _canonical_wechat_identity(identity)
                )
            _, identity_metadata = self.store.observe_channel_wechat_identity(
                tenant_id=heartbeat.tenant_id,
                channel_account_id=heartbeat.channel_account_id,
                identity=identity,
                identity_fingerprint=identity_fingerprint,
                now=observed_at,
            )
            self.store.heartbeat_agent(
                tenant_id=heartbeat.tenant_id,
                agent_id=heartbeat.agent_id,
                status=heartbeat.status,
                capabilities=getattr(heartbeat, "capabilities", {}),
                now=observed_at,
            )
            identity_status = str(identity_metadata["status"])
            return CenterResponse(
                200,
                {
                    "accepted": True,
                    "identity_status": identity_status,
                    "operations_allowed": identity_status == "verified",
                    "wechat_identity": identity,
                },
            )

        if path == "/v1/agent/inbound":
            envelope = InboundEnvelope.from_dict(payload)
            self._authenticate_agent_for_operations(
                envelope.tenant_id,
                envelope.agent_id,
                envelope.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != envelope.external_message_id:
                raise CenterRequestError(400, "Idempotency-Key must equal external_message_id")
            self.store.record_inbound_message(
                tenant_id=envelope.tenant_id,
                channel_account_id=envelope.channel_account_id,
                external_message_id=envelope.external_message_id,
                external_conversation_id=envelope.conversation_external_id,
                sender_external_id=envelope.sender_external_id,
                message_type=envelope.message_type,
                content=envelope.content,
                raw_json={"occurred_at": envelope.occurred_at},
                received_at=_parse_timestamp(envelope.occurred_at),
            )
            return CenterResponse(200, {"accepted": True})

        if path == "/v1/agent/contacts/sync":
            snapshot = ContactSnapshot.from_dict(payload)
            self._authenticate_agent_for_operations(
                snapshot.tenant_id,
                snapshot.agent_id,
                snapshot.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != snapshot.snapshot_id:
                raise CenterRequestError(400, "Idempotency-Key must equal snapshot_id")
            observed_at = _parse_timestamp(snapshot.observed_at)
            contacts = [
                {
                    "external_contact_id": item.external_contact_id,
                    "display_name": item.display_name,
                    "remark": item.remark,
                    "kind": item.kind,
                    "status": item.status,
                    "metadata": item.raw,
                }
                for item in snapshot.contacts
            ]
            self.store.sync_channel_contacts(
                snapshot.tenant_id,
                snapshot.channel_account_id,
                contacts,
                observed_at,
            )
            return CenterResponse(200, {"accepted": True, "synced": len(contacts)})

        if path == "/v1/agent/friend-requests/sync":
            snapshot = FriendRequestSnapshot.from_dict(payload)
            self._authenticate_agent_for_operations(
                snapshot.tenant_id,
                snapshot.agent_id,
                snapshot.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != snapshot.snapshot_id:
                raise CenterRequestError(400, "Idempotency-Key must equal snapshot_id")
            observed_at = _parse_timestamp(snapshot.observed_at)
            requests = [
                {
                    "external_request_id": item.external_request_id,
                    "requester_external_id": item.external_request_id,
                    "display_name": item.display_name,
                    "verification_message": item.verification_message,
                    "requested_at": _parse_timestamp(item.requested_at),
                    "status": item.status,
                    "raw_json": item.raw,
                }
                for item in snapshot.requests
            ]
            self.store.sync_friend_requests(
                snapshot.tenant_id,
                snapshot.channel_account_id,
                requests,
                observed_at,
            )
            return CenterResponse(200, {"accepted": True, "synced": len(requests)})

        if path == "/v1/agent/deliveries/claim":
            tenant_id = _required_text(payload, "tenant_id")
            agent_id = _required_single_line_text(payload, "agent_id", maximum=36)
            channel_account_id = _required_single_line_text(
                payload, "channel_account_id", maximum=36
            )
            self._authenticate_agent_for_operations(
                tenant_id, agent_id, channel_account_id, token
            )
            limit = _bounded_int(payload.get("limit", 20), "limit", 1, 100)
            jobs = self.store.claim_deliveries(
                tenant_id=tenant_id,
                agent_id=agent_id,
                channel_account_id=channel_account_id,
                limit=limit,
            )
            commands = [
                _delivery_command(job, tenant_id, agent_id, channel_account_id).to_dict()
                for job in jobs
            ]
            return CenterResponse(200, {"deliveries": commands})

        if path == "/v1/agent/deliveries/start":
            start = DeliveryStart.from_dict(payload)
            self._authenticate_agent_for_operations(
                start.tenant_id,
                start.agent_id,
                start.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != f"{start.delivery_id}:{start.attempt}:start":
                raise CenterRequestError(400, "invalid delivery start idempotency key")
            delivery = self.store.start_delivery(
                tenant_id=start.tenant_id,
                agent_id=start.agent_id,
                delivery_id=start.delivery_id,
                lease_token=start.lease_token,
                attempt=start.attempt,
            )
            return CenterResponse(
                200,
                {
                    "accepted": True,
                    "send_allowed": str(getattr(delivery, "status", "sending")) == "sending",
                },
            )

        if path == "/v1/agent/deliveries/complete":
            result = DeliveryResult.from_dict(payload)
            self._authenticate_agent_for_operations(
                result.tenant_id,
                result.agent_id,
                result.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != f"{result.delivery_id}:{result.attempt}":
                raise CenterRequestError(400, "invalid delivery completion idempotency key")
            _parse_timestamp(result.completed_at)
            self.store.complete_delivery(
                tenant_id=result.tenant_id,
                agent_id=result.agent_id,
                delivery_id=result.delivery_id,
                lease_token=result.lease_token,
                attempt=result.attempt,
                result_status=result.status,
                error=result.error or "",
            )
            return CenterResponse(200, {"accepted": True})

        if path == "/v1/agent/operations/claim":
            tenant_id = _required_text(payload, "tenant_id")
            agent_id = _required_text(payload, "agent_id")
            channel_account_id = _required_text(payload, "channel_account_id")
            self._authenticate_agent_for_operations(
                tenant_id, agent_id, channel_account_id, token
            )
            limit = _bounded_int(payload.get("limit", 1), "limit", 1, 100)
            jobs = self.store.claim_operation_jobs(
                tenant_id,
                agent_id,
                channel_account_id,
                limit=limit,
            )
            commands = [
                _operation_command(job, tenant_id, agent_id, channel_account_id).to_dict()
                for job in jobs
            ]
            return CenterResponse(200, {"operations": commands})

        if path == "/v1/agent/operations/start":
            start = OperationStart.from_dict(payload)
            self._authenticate_agent_for_operations(
                start.tenant_id,
                start.agent_id,
                start.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != f"{start.operation_id}:{start.attempt}:start":
                raise CenterRequestError(400, "invalid operation start idempotency key")
            self.store.start_operation_job(
                start.tenant_id,
                start.operation_id,
                start.agent_id,
                start.lease_token,
                start.attempt,
            )
            return CenterResponse(200, {"accepted": True})

        if path == "/v1/agent/operations/complete":
            result = OperationResult.from_dict(payload)
            self._authenticate_agent_for_operations(
                result.tenant_id,
                result.agent_id,
                result.channel_account_id,
                token,
            )
            if _require_idempotency_key(headers) != f"{result.operation_id}:{result.attempt}":
                raise CenterRequestError(400, "invalid operation completion idempotency key")
            _parse_timestamp(result.completed_at)
            self.store.complete_operation_job(
                result.tenant_id,
                result.operation_id,
                result.agent_id,
                result.lease_token,
                result.attempt,
                result.status,
                result=result.evidence,
                error=result.error or "",
            )
            return CenterResponse(200, {"accepted": True})

        raise CenterRequestError(404, "not_found")

    def _authenticate_agent_for_operations(
        self,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        token: str,
    ) -> None:
        self.authenticate_agent(tenant_id, agent_id, channel_account_id, token)
        identity = self.store.get_channel_wechat_identity(
            tenant_id, channel_account_id
        )
        if str(identity.get("status", "unavailable")) != "verified":
            raise CenterRequestError(409, "wechat_identity_not_verified")

    def _dispatch_tenant(
        self,
        method: str,
        path: str,
        query: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
    ) -> CenterResponse:
        tenant_id = _header(headers, "X-Tenant-ID").strip()
        if not tenant_id:
            raise CenterRequestError(400, "X-Tenant-ID is required")
        token = _bearer_token(headers)

        if method == "GET" and path == "/v1/session":
            principal = self.authenticate_tenant(tenant_id, token, "messages.read")
            return CenterResponse(
                200,
                {
                    "ok": True,
                    "session": {
                        "tenant_id": tenant_id,
                        "user_id": str(getattr(principal, "subject_id", "")),
                        "role": str(getattr(principal, "role", "")),
                    },
                },
            )

        if method == "GET" and path == "/v1/tenant":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            tenant = self.store.get_tenant(tenant_id)
            return CenterResponse(200, {"ok": True, "tenant": _jsonable(tenant)})

        if method == "GET" and path == "/v1/members":
            self.authenticate_tenant(tenant_id, token, "members.manage")
            members = self.store.list_tenant_members(
                tenant_id,
                limit=_query_limit(query),
            )
            return CenterResponse(200, {"ok": True, "members": _jsonable(members)})

        if method == "GET" and path == "/v1/channel-accounts":
            self.authenticate_tenant(tenant_id, token, "channels.manage")
            accounts = self.store.list_channel_accounts(
                tenant_id,
                limit=_query_limit(query),
            )
            return CenterResponse(
                200,
                {
                    "ok": True,
                    "channel_accounts": [
                        _channel_account_public(
                            account,
                            self.store.channel_wechat_identity_metadata(account),
                        )
                        for account in accounts
                    ],
                },
            )

        channel_prefix = "/v1/channel-accounts/"
        identity_suffix = "/confirm-wechat-identity"
        if (
            method == "POST"
            and path.startswith(channel_prefix)
            and path.endswith(identity_suffix)
        ):
            principal = self.authenticate_tenant(
                tenant_id, token, "channels.manage"
            )
            _require_user_principal(principal)
            channel_account_id = path[
                len(channel_prefix) : -len(identity_suffix)
            ].strip("/")
            if not channel_account_id or "/" in channel_account_id:
                raise CenterRequestError(404, "not_found")
            account, identity, _ = self.store.confirm_channel_wechat_identity(
                tenant_id,
                channel_account_id,
                str(principal.subject_id),
                _require_idempotency_key(headers),
            )
            return CenterResponse(
                200,
                {
                    "ok": True,
                    "channel_account": _channel_account_public(account, identity),
                    "wechat_identity": identity,
                },
            )

        if method == "GET" and path == "/v1/agents":
            self.authenticate_tenant(tenant_id, token, "agents.manage")
            agents = self.store.list_agent_instances(
                tenant_id,
                limit=_query_limit(query),
            )
            return CenterResponse(200, {"ok": True, "agents": _jsonable(agents)})

        if method == "POST" and path == "/v1/agent-activations":
            principal = self.authenticate_tenant(tenant_id, token, "agents.manage")
            _require_user_principal(principal)
            idempotency_key = _require_idempotency_key(headers)
            if self.activation_security is None:
                raise CenterRequestError(503, "agent_activation_unavailable")
            agent_id = _required_text(payload, "agent_id")
            channel_account_id = _required_text(payload, "channel_account_id")
            expires_in_seconds = _bounded_int(
                payload.get("expires_in_seconds"),
                "expires_in_seconds",
                300,
                86400,
            )
            activation_code = self.activation_security.derive_activation_code(
                tenant_id,
                idempotency_key,
                agent_id,
                channel_account_id,
                expires_in_seconds,
            )
            activation, created = self.store.create_agent_activation(
                tenant_id=tenant_id,
                agent_id=agent_id,
                channel_account_id=channel_account_id,
                expires_in_seconds=expires_in_seconds,
                code_hash=self.activation_security.hash_activation_code(activation_code),
                idempotency_key=idempotency_key,
                actor_user_id=str(principal.subject_id),
            )
            return CenterResponse(
                201 if created else 200,
                {
                    "activation_code": activation_code,
                    "activation": _activation_public(activation),
                },
            )

        if method == "GET" and path == "/v1/contacts":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            params = parse_qs(query)
            channel_account_id = _query_text(params, "channel_account_id") or None
            contacts = self.store.list_channel_contacts_items(
                tenant_id,
                channel_account_id=channel_account_id,
                kind=_query_text(params, "kind") or None,
                status=_query_text(params, "status") or None,
                query=_query_text(params, "query"),
                limit=_query_limit(query),
            )
            policies = self.store.list_chat_policies(
                tenant_id,
                contact_ids=[str(getattr(item, "id", "")) for item in contacts],
            )
            by_contact = {
                str(getattr(item, "contact_id", "")): item
                for item in policies
            }
            return CenterResponse(
                200,
                {
                    "ok": True,
                    "contacts": [
                        _contact_public(item, by_contact.get(str(getattr(item, "id", ""))))
                        for item in contacts
                    ],
                },
            )

        contact_policy_prefix = "/v1/contacts/"
        if method == "PUT" and path.startswith(contact_policy_prefix) and path.endswith("/policy"):
            principal = self.authenticate_tenant(tenant_id, token, "channels.manage")
            _require_user_principal(principal)
            _require_idempotency_key(headers)
            contact_id = path[len(contact_policy_prefix) : -len("/policy")].strip("/")
            if not contact_id:
                raise CenterRequestError(400, "contact_id is required")
            policy = self.store.set_chat_policy(
                tenant_id,
                contact_id,
                _required_choice(payload, "policy", {"ai_auto", "manual", "paused", "blocked"}),
                _required_bool(payload, "welcome_enabled"),
                str(principal.subject_id),
            )
            return CenterResponse(200, {"ok": True, "policy": _jsonable(policy)})

        if method == "POST" and path == "/v1/contacts/policies/batch":
            principal = self.authenticate_tenant(tenant_id, token, "channels.manage")
            _require_user_principal(principal)
            _require_idempotency_key(headers)
            channel_account_id = _required_text(payload, "channel_account_id")
            contact_ids = _required_string_list(payload, "contact_ids", maximum=500)
            available = self.store.list_channel_contacts_items(
                tenant_id,
                channel_account_id=channel_account_id,
                limit=500,
            )
            available_ids = {str(getattr(item, "id", "")) for item in available}
            if any(item not in available_ids for item in contact_ids):
                raise ValueError("one or more contacts do not belong to the channel account")
            welcome_enabled = (
                _required_bool(payload, "welcome_enabled")
                if "welcome_enabled" in payload
                else None
            )
            policies = self.store.batch_set_chat_policies(
                tenant_id,
                contact_ids,
                _required_choice(payload, "policy", {"ai_auto", "manual", "paused", "blocked"}),
                welcome_enabled,
                str(principal.subject_id),
            )
            return CenterResponse(200, {"ok": True, "policies": _jsonable(policies)})

        if method == "GET" and path == "/v1/friend-requests":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            params = parse_qs(query)
            channel_account_id = _query_text(params, "channel_account_id") or None
            requests = self.store.list_friend_requests(
                tenant_id,
                channel_account_id=channel_account_id,
                status=_query_text(params, "status") or None,
                query=_query_text(params, "query"),
                limit=_query_limit(query),
            )
            active_jobs = self.store.list_operation_jobs(
                tenant_id,
                channel_account_id=channel_account_id,
                limit=500,
            )
            active_by_request: dict[str, Any] = {}
            for job in active_jobs:
                friend_request_id = str(getattr(job, "friend_request_id", "") or "")
                if friend_request_id and str(getattr(job, "status", "")) in {
                    "queued",
                    "claimed",
                    "executing",
                    "uncertain",
                }:
                    active_by_request[friend_request_id] = job
            public_requests: list[dict[str, Any]] = []
            for request in requests:
                value = dict(_jsonable(request))
                active = active_by_request.get(str(value.get("id") or ""))
                value["operation_status"] = (
                    str(getattr(active, "status", "")) if active is not None else ""
                )
                value["operation_job_id"] = (
                    str(getattr(active, "id", "")) if active is not None else ""
                )
                public_requests.append(value)
            return CenterResponse(200, {"ok": True, "friend_requests": public_requests})

        friend_actions = {
            "/v1/friend-requests/approve-selected": ("selected", "accept_friend"),
            "/v1/friend-requests/approve-batch": ("batch", "accept_friend"),
            "/v1/friend-requests/approve-all": ("all", "accept_friend"),
            "/v1/friend-requests/reject-selected": ("selected", "reject_friend"),
        }
        if method == "POST" and path in friend_actions:
            principal = self.authenticate_tenant(tenant_id, token, "deliveries.manage")
            _require_user_principal(principal)
            selection, action = friend_actions[path]
            idempotency_key = _require_idempotency_key(headers)
            send_welcome = bool(payload.get("send_welcome_message", False))
            if type(payload.get("send_welcome_message", False)) is not bool:
                raise ValueError("send_welcome_message must be a boolean")
            raw_welcome_message = payload.get("welcome_message", "")
            if type(raw_welcome_message) is not str:
                raise ValueError("welcome_message must be a string")
            welcome_message = raw_welcome_message.strip()
            if len(welcome_message) > 4000:
                raise ValueError("welcome_message must not exceed 4000 characters")
            if send_welcome and not welcome_message:
                raise ValueError("welcome_message is required when send_welcome_message is true")
            if not send_welcome:
                welcome_message = ""
            auto_enable_reply = payload.get("auto_enable_reply", False)
            if type(auto_enable_reply) is not bool:
                raise ValueError("auto_enable_reply must be a boolean")
            request_ids = (
                _required_string_list(payload, "friend_request_ids", maximum=500)
                if selection == "selected"
                else None
            )
            limit = None
            if selection == "batch":
                limit = _bounded_int(payload.get("batch_size", 10), "batch_size", 1, 100)
            jobs = self.store.enqueue_friend_request_operations(
                tenant_id,
                _required_text(payload, "channel_account_id"),
                request_ids=request_ids,
                selection=selection,
                action=action,
                actor_user_id=str(principal.subject_id),
                idempotency_key=idempotency_key,
                welcome_message=welcome_message,
                auto_enable_reply=auto_enable_reply,
                limit=limit,
            )
            return CenterResponse(
                201,
                {"ok": True, "operation_jobs": [_operation_public(job) for job in jobs]},
            )

        if method == "GET" and path == "/v1/operation-jobs":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            params = parse_qs(query)
            jobs = self.store.list_operation_jobs(
                tenant_id,
                channel_account_id=_query_text(params, "channel_account_id") or None,
                status=_query_text(params, "status") or None,
                operation_type=_query_text(params, "operation_type") or None,
                limit=_query_limit(query),
            )
            return CenterResponse(
                200,
                {"ok": True, "operation_jobs": [_operation_public(job) for job in jobs]},
            )

        operation_prefix = "/v1/operation-jobs/"
        if method == "POST" and path.startswith(operation_prefix):
            principal = self.authenticate_tenant(tenant_id, token, "deliveries.manage")
            _require_user_principal(principal)
            idempotency_key = _require_idempotency_key(headers)
            suffix = path[len(operation_prefix) :]
            operation_id, separator, action = suffix.partition("/")
            if not operation_id or not separator:
                raise CenterRequestError(404, "not_found")
            if action == "cancel":
                job = self.store.cancel_operation_job(
                    tenant_id,
                    operation_id,
                    str(principal.subject_id),
                )
            elif action == "retry":
                job = self.store.retry_operation_job(
                    tenant_id,
                    operation_id,
                    str(principal.subject_id),
                )
            elif action == "resolve":
                job = self.store.resolve_operation_job(
                    tenant_id,
                    operation_id,
                    _required_choice(payload, "resolution", {"succeeded", "failed", "retry", "cancelled"}),
                    idempotency_key,
                    str(principal.subject_id),
                    _required_text(payload, "note"),
                )
            else:
                raise CenterRequestError(404, "not_found")
            return CenterResponse(200, {"ok": True, "operation_job": _operation_public(job)})

        if method == "GET" and path == "/v1/operation-limits":
            self.authenticate_tenant(tenant_id, token, "channels.manage")
            channel_account_id = _query_text(parse_qs(query), "channel_account_id")
            if not channel_account_id:
                raise CenterRequestError(400, "channel_account_id is required")
            limits = self.store.get_operation_limit(tenant_id, channel_account_id)
            return CenterResponse(200, {"ok": True, "operation_limits": _jsonable(limits)})

        operation_limits_prefix = "/v1/operation-limits/"
        if method == "PUT" and path.startswith(operation_limits_prefix):
            principal = self.authenticate_tenant(tenant_id, token, "channels.manage")
            _require_user_principal(principal)
            _require_idempotency_key(headers)
            channel_account_id = path[len(operation_limits_prefix) :].strip("/")
            if not channel_account_id:
                raise CenterRequestError(400, "channel_account_id is required")
            limits = self.store.update_operation_limit(
                tenant_id,
                channel_account_id,
                batch_size=_optional_bounded_int(payload, "batch_size", 1, 100),
                min_interval_seconds=_optional_bounded_int(payload, "min_interval_seconds", 1, 3600),
                max_per_hour=_optional_bounded_int(payload, "max_per_hour", 1, 10000),
                max_per_day=_optional_bounded_int(payload, "max_per_day", 1, 100000),
                paused=_optional_bool(payload, "paused"),
                welcome_message=_optional_text(payload, "welcome_message", maximum=4000),
                auto_enable_reply=_optional_bool(payload, "auto_enable_reply"),
                actor_user_id=str(principal.subject_id),
            )
            return CenterResponse(200, {"ok": True, "operation_limits": _jsonable(limits)})

        if method == "GET" and path == "/v1/messages":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            limit = _query_limit(query)
            messages = self.store.list_messages(tenant_id=tenant_id, limit=limit)
            return CenterResponse(200, {"ok": True, "messages": _jsonable(messages)})

        if method == "GET" and path == "/v1/reply-jobs":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            limit = _query_limit(query)
            statuses = parse_qs(query).get("status") or []
            status = statuses[0].strip() if statuses else None
            jobs = self.store.list_reply_jobs(
                tenant_id,
                status=status or None,
                limit=limit,
            )
            return CenterResponse(
                200,
                {"ok": True, "reply_jobs": [_reply_job_public(job) for job in jobs]},
            )

        if method == "GET" and path == "/v1/deliveries":
            self.authenticate_tenant(tenant_id, token, "messages.read")
            limit = _query_limit(query)
            jobs = self.store.list_delivery_jobs(tenant_id=tenant_id, limit=limit)
            return CenterResponse(
                200,
                {"ok": True, "deliveries": [_delivery_public(job) for job in jobs]},
            )

        if method == "GET" and path == "/v1/audit":
            self.authenticate_tenant(tenant_id, token, "audit.read")
            limit = _query_limit(query)
            events = self.store.list_audit_events(tenant_id=tenant_id, limit=limit)
            return CenterResponse(200, {"ok": True, "events": _jsonable(events)})

        if method == "GET" and path == "/v1/bot-profile":
            self.authenticate_tenant(tenant_id, token, "tenant.manage")
            profile = self.store.get_bot_profile(tenant_id)
            return CenterResponse(200, {"ok": True, "profile": _jsonable(profile)})

        if method == "PUT" and path == "/v1/bot-profile":
            principal = self.authenticate_tenant(tenant_id, token, "tenant.manage")
            settings = payload.get("settings", {})
            if not isinstance(settings, Mapping):
                raise ValueError("settings must be a JSON object")
            profile = self.store.upsert_bot_profile(
                tenant_id,
                name=str(payload.get("name") or "default"),
                prompt=_required_text(payload, "prompt"),
                settings=validate_bot_settings(dict(settings)),
            )
            self.store.append_audit(
                tenant_id,
                "bot_profile.updated",
                actor_user_id=str(principal.subject_id),
                entity_type="bot_profile",
                entity_id=profile.id,
                details={"name": profile.name},
            )
            return CenterResponse(200, {"ok": True, "profile": _jsonable(profile)})

        if method == "GET" and path == "/v1/knowledge-base":
            self.authenticate_tenant(tenant_id, token, "tenant.manage")
            knowledge = self.store.get_knowledge_base(tenant_id)
            return CenterResponse(200, {"ok": True, "knowledge_base": _jsonable(knowledge)})

        if method == "PUT" and path == "/v1/knowledge-base":
            principal = self.authenticate_tenant(tenant_id, token, "tenant.manage")
            content = str(payload.get("content") or "")
            if len(content) > 20_000:
                raise ValueError("knowledge base content exceeds 20000 characters")
            status = str(payload.get("status") or "active")
            if status not in {"active", "disabled"}:
                raise ValueError("knowledge base status must be active or disabled")
            knowledge = self.store.upsert_knowledge_base(
                tenant_id,
                name=str(payload.get("name") or "default"),
                content=content,
                status=status,
            )
            self.store.append_audit(
                tenant_id,
                "knowledge_base.updated",
                actor_user_id=str(principal.subject_id),
                entity_type="knowledge_base",
                entity_id=knowledge.id,
                details={"name": knowledge.name, "status": knowledge.status},
            )
            return CenterResponse(
                200,
                {"ok": True, "knowledge_base": _jsonable(knowledge)},
            )

        credential_prefix = "/v1/ai-credentials/"
        if method == "PUT" and path.startswith(credential_prefix):
            if self.secret_cipher is None:
                raise CenterRequestError(503, "secret_storage_unavailable")
            principal = self.authenticate_tenant(tenant_id, token, "tenant.manage")
            name = path[len(credential_prefix) :].strip("/")
            if not name:
                raise CenterRequestError(400, "credential name is required")
            credential = self.store.store_api_credential(
                tenant_id,
                "llm",
                name,
                self.secret_cipher.encrypt(_required_text(payload, "value")),
            )
            self.store.append_audit(
                tenant_id,
                "llm_credential.updated",
                actor_user_id=str(principal.subject_id),
                entity_type="api_credential",
                entity_id=credential.id,
                details={"name": name},
            )
            return CenterResponse(
                200,
                {"ok": True, "credential": {"id": credential.id, "name": name}},
            )

        delivery_prefix = "/v1/deliveries/"
        if method == "POST" and path.startswith(delivery_prefix) and path.endswith("/resolve"):
            principal = self.authenticate_tenant(tenant_id, token, "deliveries.manage")
            if getattr(principal, "subject_type", "") != "user":
                raise PermissionError("user principal required")
            delivery_id = path[len(delivery_prefix) : -len("/resolve")].strip("/")
            if not delivery_id:
                raise CenterRequestError(400, "delivery_id is required")
            result = self.store.resolve_uncertain_delivery(
                tenant_id,
                delivery_id,
                _required_text(payload, "resolution"),
                _require_idempotency_key(headers),
                actor_user_id=str(principal.subject_id),
                note=_required_text(payload, "note"),
            )
            return CenterResponse(200, {"ok": True, "delivery": _delivery_public(result)})

        if method == "POST" and path == "/v1/deliveries":
            self.authenticate_tenant(tenant_id, token, "deliveries.manage")
            idempotency_key = _require_idempotency_key(headers)
            conversation_id = _required_text(payload, "conversation_id")
            channel_account_id = _required_text(payload, "channel_account_id")
            conversation = self.store.get_conversation(tenant_id, conversation_id)
            if conversation.channel_account_id != channel_account_id:
                raise ValueError("conversation belongs to another channel account")
            result = self.store.enqueue_delivery(
                tenant_id=tenant_id,
                channel_account_id=channel_account_id,
                conversation_id=conversation_id,
                payload={
                    "conversation_external_id": conversation.external_conversation_id,
                    "content": _required_text(payload, "content"),
                },
                dedupe_key=idempotency_key,
            )
            return CenterResponse(201, {"ok": True, "delivery": _delivery_public(result)})

        raise CenterRequestError(404, "not_found")


def run_center_server(
    application: CenterApplication,
    host: str = "127.0.0.1",
    port: int = 8790,
    background_runner: Callable[[], None] | None = None,
) -> None:
    _start_background_runner(background_runner)
    handler = build_center_handler(application)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Center API: http://{host}:{port}", flush=True)
    server.serve_forever()


def run_center_production_server(
    application: CenterApplication,
    host: str = "127.0.0.1",
    port: int = 8790,
    background_runner: Callable[[], None] | None = None,
    *,
    threads: int = 8,
) -> None:
    if threads < 4:
        raise ValueError("production server threads must be at least 4")
    try:
        from waitress import serve
    except ImportError as exc:
        raise RuntimeError("waitress is required for center serve --production") from exc

    _start_background_runner(background_runner)
    print(f"Center API (production): http://{host}:{port}", flush=True)
    serve(
        build_center_wsgi_application(application),
        host=host,
        port=port,
        threads=threads,
        expose_tracebacks=False,
        max_request_body_size=MAX_REQUEST_BYTES,
        clear_untrusted_proxy_headers=True,
    )


def build_center_wsgi_application(application: CenterApplication) -> Callable[..., list[bytes]]:
    def wsgi(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        query = str(environ.get("QUERY_STRING") or "")
        target = f"{path}?{query}" if query else path
        headers = _wsgi_headers(environ)
        asset = _center_admin_asset(method, target)
        if asset is not None:
            status_code, content_type, encoded = asset
            start_response(
                f"{status_code} {HTTPStatus(status_code).phrase}",
                _browser_headers(content_type, len(encoded)),
            )
            return [encoded]
        try:
            payload = _read_wsgi_json(environ) if method in {"POST", "PUT"} else {}
            response = application.dispatch(method, target, headers, payload)
        except CenterRequestError as exc:
            response = CenterResponse(exc.status, {"ok": False, "error": str(exc)})
        except Exception:
            response = CenterResponse(500, {"ok": False, "error": "internal_error"})

        encoded = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
        status = f"{response.status} {HTTPStatus(response.status).phrase}"
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
            ],
        )
        return [encoded]

    return wsgi


def _start_background_runner(background_runner: Callable[[], None] | None) -> None:
    if background_runner is None:
        return
    threading.Thread(
        target=background_runner,
        name="center-reply-worker",
        daemon=True,
    ).start()


def _wsgi_headers(environ: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            headers[name] = str(value)
    if environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = str(environ["CONTENT_TYPE"])
    if environ.get("CONTENT_LENGTH"):
        headers["Content-Length"] = str(environ["CONTENT_LENGTH"])
    return headers


def _read_wsgi_json(environ: Mapping[str, Any]) -> dict[str, Any]:
    raw_length = str(environ.get("CONTENT_LENGTH") or "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise CenterRequestError(400, "invalid_content_length") from exc
    if length <= 0:
        return {}
    if length > MAX_REQUEST_BYTES:
        raise CenterRequestError(413, "request_too_large")
    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        raise CenterRequestError(400, "request_body_required")
    try:
        payload = json.loads(stream.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CenterRequestError(400, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise CenterRequestError(400, "json_object_required")
    return payload


def build_center_handler(application: CenterApplication) -> type[BaseHTTPRequestHandler]:
    class CenterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asset = _center_admin_asset(self.command, self.path)
            if asset is not None:
                self._send_asset(*asset)
                return
            self._dispatch({})

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
            except CenterRequestError as exc:
                self._send(CenterResponse(exc.status, {"ok": False, "error": str(exc)}))
                return
            self._dispatch(payload)

        def do_PUT(self) -> None:
            try:
                payload = self._read_json()
            except CenterRequestError as exc:
                self._send(CenterResponse(exc.status, {"ok": False, "error": str(exc)}))
                return
            self._dispatch(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _dispatch(self, payload: dict[str, Any]) -> None:
            try:
                response = application.dispatch(self.command, self.path, self.headers, payload)
            except Exception:
                response = CenterResponse(500, {"ok": False, "error": "internal_error"})
            self._send(response)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError as exc:
                raise CenterRequestError(400, "invalid_content_length") from exc
            if length <= 0:
                return {}
            if length > MAX_REQUEST_BYTES:
                raise CenterRequestError(413, "request_too_large")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CenterRequestError(400, "invalid_json") from exc
            if not isinstance(payload, dict):
                raise CenterRequestError(400, "json_object_required")
            return payload

        def _send(self, response: CenterResponse) -> None:
            encoded = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _send_asset(self, status: int, content_type: str, encoded: bytes) -> None:
            self.send_response(status)
            for name, value in _browser_headers(content_type, len(encoded)):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

    return CenterHandler


def _center_admin_asset(method: str, target: str) -> tuple[int, str, bytes] | None:
    path = urlparse(target).path.rstrip("/") or "/"
    if method != "GET" or (path != "/" and path != "/admin" and not path.startswith("/admin/")):
        return None
    try:
        content_type, body = read_center_admin_asset(target)
    except (FileNotFoundError, OSError):
        return 404, "text/plain; charset=utf-8", b"Not found"
    return 200, content_type, body


def _browser_headers(content_type: str, length: int) -> list[tuple[str, str]]:
    return [
        ("Content-Type", content_type),
        ("Content-Length", str(length)),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        (
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        ),
    ]


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_single_line_text(
    payload: Mapping[str, Any],
    name: str,
    *,
    maximum: int,
) -> str:
    raw = payload.get(name)
    if type(raw) is not str:
        raise ValueError(f"{name} must be a string")
    value = raw.strip()
    if not value:
        raise ValueError(f"{name} is required")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains an invalid control character")
    if len(value) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def _bearer_token(headers: Mapping[str, str]) -> str:
    value = _header(headers, "Authorization").strip()
    if not value.lower().startswith("bearer "):
        raise CenterRequestError(401, "bearer_token_required")
    token = value[7:].strip()
    if not token:
        raise CenterRequestError(401, "bearer_token_required")
    return token


def _require_idempotency_key(headers: Mapping[str, str]) -> str:
    value = _header(headers, "Idempotency-Key").strip()
    if not value:
        raise CenterRequestError(400, "Idempotency-Key is required")
    if len(value) > 200:
        raise CenterRequestError(400, "Idempotency-Key is too long")
    return value


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _query_limit(query: str) -> int:
    values = parse_qs(query).get("limit") or [100]
    return _bounded_int(values[0], "limit", 1, 500)


def _query_text(parameters: Mapping[str, list[str]], name: str) -> str:
    values = parameters.get(name) or []
    return str(values[0]).strip() if values else ""


def _required_choice(payload: Mapping[str, Any], name: str, allowed: set[str]) -> str:
    value = _required_text(payload, name)
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _required_bool(payload: Mapping[str, Any], name: str) -> bool:
    value = payload.get(name)
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_bool(payload: Mapping[str, Any], name: str) -> bool | None:
    if name not in payload:
        return None
    return _required_bool(payload, name)


def _optional_text(
    payload: Mapping[str, Any],
    name: str,
    *,
    maximum: int,
) -> str | None:
    if name not in payload:
        return None
    value = payload[name]
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return normalized


def _optional_bounded_int(
    payload: Mapping[str, Any],
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if name not in payload:
        return None
    value = payload[name]
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return _bounded_int(value, name, minimum, maximum)


def _required_string_list(
    payload: Mapping[str, Any],
    name: str,
    *,
    maximum: int,
) -> list[str]:
    value = payload.get(name)
    if type(value) is not list or not value:
        raise ValueError(f"{name} must be a non-empty array")
    if len(value) > maximum:
        raise ValueError(f"{name} must not contain more than {maximum} items")
    if any(type(item) is not str or not item.strip() for item in value):
        raise ValueError(f"{name} must contain non-empty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if len(result) != len(value):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _require_user_principal(principal: Any) -> None:
    if str(getattr(principal, "subject_type", "")) != "user":
        raise PermissionError("user principal required")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _delivery_command(
    job: Any,
    tenant_id: str,
    agent_id: str,
    channel_account_id: str,
) -> DeliveryCommand:
    payload = dict(job.payload_json or {})
    return DeliveryCommand(
        tenant_id=tenant_id,
        agent_id=agent_id,
        channel_account_id=channel_account_id,
        delivery_id=str(job.id),
        lease_token=_required_text({"lease_token": job.lease_token}, "lease_token"),
        conversation_external_id=_required_text(
            payload,
            "conversation_external_id",
        ),
        content=_required_text(payload, "content"),
        status="claimed",
        attempt=int(job.attempts),
    )


def _operation_command(
    job: Any,
    tenant_id: str,
    agent_id: str,
    channel_account_id: str,
) -> OperationCommand:
    payload = dict(getattr(job, "payload_json", None) or {})
    return OperationCommand(
        tenant_id=tenant_id,
        agent_id=agent_id,
        channel_account_id=channel_account_id,
        operation_id=str(job.id),
        lease_token=_required_text({"lease_token": job.lease_token}, "lease_token"),
        operation_type=str(job.operation_type),
        attempt=int(job.attempts),
        payload=payload,
    )


def _normalize_wechat_identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("wechat_identity must be an object")
    unknown = set(value) - {"account_id", "nickname"}
    if unknown:
        raise ValueError("wechat_identity contains unsupported fields")
    result: dict[str, str] = {}
    for field, maximum in (("account_id", 255), ("nickname", 200)):
        item = value.get(field, "")
        if not isinstance(item, str):
            raise ValueError(f"wechat_identity.{field} must be a string")
        normalized = item.strip()
        if "\r" in normalized or "\n" in normalized:
            raise ValueError(f"wechat_identity.{field} must be single-line text")
        if len(normalized) > maximum:
            raise ValueError(
                f"wechat_identity.{field} cannot exceed {maximum} characters"
            )
        if normalized:
            result[field] = normalized
    return result


def _canonical_wechat_identity(identity: Mapping[str, str]) -> str:
    if identity.get("account_id"):
        canonical = {"account_id": identity["account_id"]}
    elif identity.get("nickname"):
        canonical = {"nickname": identity["nickname"]}
    else:
        raise ValueError("wechat_identity must include account_id or nickname")
    return json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _channel_account_public(
    account: Any, wechat_identity: Mapping[str, Any]
) -> dict[str, Any]:
    result = dict(_jsonable(account))
    config = result.get("config_json")
    if isinstance(config, Mapping):
        sanitized_config = dict(config)
        sanitized_config.pop("_center_wechat_identity_v1", None)
        result["config_json"] = sanitized_config
    result["wechat_identity"] = _jsonable(wechat_identity)
    return result


def _contact_public(contact: Any, policy: Any | None) -> dict[str, Any]:
    if isinstance(contact, Mapping):
        result = dict(_jsonable(contact))
    else:
        result = {
            "id": str(contact.id),
            "tenant_id": str(contact.tenant_id),
            "channel_account_id": str(contact.channel_account_id),
            "external_contact_id": str(contact.external_contact_id),
            "kind": str(contact.kind),
            "display_name": str(contact.display_name or ""),
            "remark": str(contact.remark or ""),
            "status": str(contact.status),
            "metadata": _jsonable(contact.metadata_json),
            "first_observed_at": _jsonable(contact.first_observed_at),
            "last_observed_at": _jsonable(contact.last_observed_at),
            "created_at": _jsonable(contact.created_at),
            "updated_at": _jsonable(contact.updated_at),
        }
    result["policy"] = str(getattr(policy, "mode", "manual"))
    result["welcome_enabled"] = bool(getattr(policy, "welcome_enabled", False))
    if "metadata_json" in result:
        result["metadata"] = result.pop("metadata_json")
    return result


def _operation_public(job: Any) -> dict[str, Any]:
    if isinstance(job, Mapping):
        result = dict(_jsonable(job))
    else:
        result = {
            "id": str(job.id),
            "tenant_id": str(job.tenant_id),
            "channel_account_id": str(job.channel_account_id),
            "friend_request_id": job.friend_request_id,
            "depends_on_job_id": job.depends_on_job_id,
            "operation_type": str(job.operation_type),
            "status": str(job.status),
            "payload": _jsonable(job.payload_json),
            "attempts": int(job.attempts),
            "available_at": _jsonable(job.available_at),
            "claimed_at": _jsonable(job.claimed_at),
            "started_at": _jsonable(job.started_at),
            "completed_at": _jsonable(job.completed_at),
            "error": str(job.error or ""),
            "created_at": _jsonable(job.created_at),
            "updated_at": _jsonable(job.updated_at),
        }
    for secret in (
        "lease_token",
        "lease_expires_at",
        "claimed_by_agent_id",
        "idempotency_key",
        "resolution_key",
        "actor_user_id",
    ):
        result.pop(secret, None)
    if "payload_json" in result:
        result["payload"] = result.pop("payload_json")
    return result


def _reply_job_public(job: Any) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "tenant_id": str(job.tenant_id),
        "source_message_id": str(job.source_message_id),
        "status": str(job.status),
        "attempts": int(job.attempts),
        "generated_delivery_id": job.generated_delivery_id,
        "error": str(job.error or ""),
        "available_at": _jsonable(job.available_at),
        "created_at": _jsonable(job.created_at),
        "updated_at": _jsonable(job.updated_at),
    }


def _delivery_public(job: Any) -> dict[str, Any]:
    if isinstance(job, Mapping):
        result = dict(_jsonable(job))
        result.pop("lease_token", None)
        result.pop("claimed_by_agent_id", None)
        result.pop("lease_expires_at", None)
        result.pop("resolution_key", None)
        if "payload_json" in result:
            result["payload"] = result.pop("payload_json")
        return result
    return {
        "id": str(job.id),
        "tenant_id": str(job.tenant_id),
        "channel_account_id": str(job.channel_account_id),
        "conversation_id": job.conversation_id,
        "source_message_id": job.source_message_id,
        "dedupe_key": str(job.dedupe_key),
        "payload": _jsonable(job.payload_json),
        "status": str(job.status),
        "attempts": int(job.attempts),
        "available_at": _jsonable(job.available_at),
        "completed_at": _jsonable(job.completed_at),
        "error": str(job.error or ""),
        "created_at": _jsonable(job.created_at),
        "updated_at": _jsonable(job.updated_at),
    }


def _activation_public(activation: Any) -> dict[str, Any]:
    consumed_at = getattr(activation, "consumed_at", None)
    expires_at = getattr(activation, "expires_at", None)
    status = "consumed" if consumed_at is not None else "pending"
    if (
        status == "pending"
        and isinstance(expires_at, datetime)
        and expires_at <= datetime.now(timezone.utc)
    ):
        status = "expired"
    return {
        "id": str(activation.id),
        "tenant_id": str(activation.tenant_id),
        "agent_id": str(activation.agent_id),
        "channel_account_id": str(activation.channel_account_id),
        "expires_in_seconds": int(activation.expires_in_seconds),
        "status": status,
        "expires_at": _jsonable(expires_at),
        "consumed_at": _jsonable(consumed_at),
        "created_at": _jsonable(activation.created_at),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    table = getattr(value, "__table__", None)
    if table is not None:
        return {column.name: _jsonable(getattr(value, column.name)) for column in table.columns}
    raise TypeError(f"Unsupported response type: {type(value).__name__}")
