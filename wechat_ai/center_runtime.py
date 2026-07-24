from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import hmac
import json
import threading
from typing import Any

from sqlalchemy import inspect, select, text, update

from wechat_ai.center_server import CenterApplication
from wechat_ai.tenant_auth import (
    ApiKeyManager,
    ApiKeyRecord,
    AuthPrincipal,
    AuthenticationFailed,
    IssuedCredential,
    SecretCipher,
    require_permission,
)
from wechat_ai.tenant_store import (
    WECHAT_IDENTITY_CONFIG_KEY,
    Base,
    ChannelAccount,
    TenantStore,
    utc_now,
)


class AgentActivationSecurity:
    """Derive current hashes and resolve legacy activation hashes during rotation."""

    MAX_ACTIVATION_CODE_BYTES = 128
    MAX_DEVICE_ID_BYTES = 255
    MAX_WECHAT_IDENTITY_BYTES = 2048

    def __init__(
        self,
        pepper: str | bytes,
        key_manager: ApiKeyManager | None = None,
        *,
        previous_peppers: Iterable[str | bytes] | None = None,
        store: TenantStore | None = None,
    ) -> None:
        if key_manager is None:
            key_manager = ApiKeyManager(
                pepper,
                previous_peppers=previous_peppers,
            )
        else:
            if previous_peppers is not None:
                raise ValueError(
                    "previous_peppers must be configured on the key_manager"
                )
            current_only = ApiKeyManager(pepper)
            binding_input = b"center-api-key-pepper-binding-v1"
            if not hmac.compare_digest(
                current_only.current_hmac(binding_input),
                key_manager.current_hmac(binding_input),
            ):
                raise ValueError("pepper does not match key_manager current pepper")
        self._key_manager = key_manager
        self._store = store
        self._identity_lock = threading.RLock()
        self._known_wechat_identity_hashes = (
            self._load_wechat_identity_hashes() if store is not None else set()
        )

    def derive_activation_code(
        self,
        tenant_id: str,
        idempotency_key: str,
        agent_id: str,
        channel_account_id: str,
        expires_in_seconds: int,
    ) -> str:
        fields = {
            "tenant_id": (tenant_id, 36),
            "idempotency_key": (idempotency_key, 200),
            "agent_id": (agent_id, 36),
            "channel_account_id": (channel_account_id, 36),
        }
        for name, (value, maximum) in fields.items():
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > maximum
            ):
                raise ValueError(f"{name} is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 300 <= expires_in_seconds <= 86400
        ):
            raise ValueError("expires_in_seconds is invalid")
        canonical = json.dumps(
            {
                "agent_id": agent_id,
                "channel_account_id": channel_account_id,
                "expires_in_seconds": expires_in_seconds,
                "idempotency_key": idempotency_key,
                "tenant_id": tenant_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        codes = tuple(
            "wxact_"
            + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            for digest in self._key_manager.hmac_candidates(
                b"agent-activation-code-v1\x00" + canonical
            )
        )
        hash_to_code: dict[str, str] = {}
        for code in codes:
            for candidate_hash in self._activation_hash_candidates(code):
                hash_to_code.setdefault(candidate_hash, code)
        stored_hash = self._find_stored_activation_hash(tuple(hash_to_code))
        return hash_to_code.get(stored_hash, codes[0])

    def hash_activation_code(self, activation_code: str) -> str:
        if (
            not isinstance(activation_code, str)
            or not activation_code.startswith("wxact_")
            or len(activation_code.encode("utf-8")) > self.MAX_ACTIVATION_CODE_BYTES
        ):
            raise ValueError("activation_failed")
        candidates = self._activation_hash_candidates(activation_code)
        return self._find_stored_activation_hash(candidates) or candidates[0]

    def hash_device_id(self, device_id: str) -> str:
        if (
            not isinstance(device_id, str)
            or not device_id
            or len(device_id.encode("utf-8")) > self.MAX_DEVICE_ID_BYTES
        ):
            raise ValueError("activation_failed")
        return self._key_manager.current_hmac(
            b"agent-device-v1\x00" + device_id.encode("utf-8")
        ).hex()

    def hash_wechat_identity(self, canonical_identity: str) -> str:
        if (
            not isinstance(canonical_identity, str)
            or not canonical_identity.strip()
            or len(canonical_identity.encode("utf-8"))
            > self.MAX_WECHAT_IDENTITY_BYTES
        ):
            raise ValueError("wechat identity is required")
        candidates = self._key_manager.digest_hex_candidates(
            b"wechat-identity-v1\x00" + canonical_identity.encode("utf-8")
        )
        current = candidates[0]
        with self._identity_lock:
            matches = [
                candidate
                for candidate in candidates
                if candidate in self._known_wechat_identity_hashes
            ]
            if matches and matches[0] != current:
                self._replace_wechat_identity_hash(matches[0], current)
                self._known_wechat_identity_hashes.discard(matches[0])
                self._known_wechat_identity_hashes.add(current)
        return current

    def issue_agent_token(self):
        return self._key_manager.generate_agent_token()

    def _activation_hash_candidates(self, activation_code: str) -> tuple[str, ...]:
        value = (
            b"agent-activation-lookup-v1\x00"
            + activation_code.encode("utf-8")
        )
        return self._key_manager.digest_hex_candidates(value)

    def _find_stored_activation_hash(
        self,
        candidates: tuple[str, ...],
    ) -> str | None:
        if self._store is None or not candidates:
            return None
        parameters = {
            f"candidate_{index}": candidate
            for index, candidate in enumerate(candidates)
        }
        placeholders = ", ".join(f":{name}" for name in parameters)
        statement = text(
            "SELECT code_hash FROM agent_activations "
            f"WHERE code_hash IN ({placeholders})"
        )
        with self._store.engine.connect() as connection:
            matches = set(connection.execute(statement, parameters).scalars())
        for candidate in candidates:
            if candidate in matches:
                return candidate
        return None

    def _load_wechat_identity_hashes(self) -> set[str]:
        if self._store is None:
            return set()
        fingerprints: set[str] = set()
        with self._store.engine.connect() as connection:
            configurations = connection.execute(
                select(ChannelAccount.config_json)
            ).scalars()
            for config in configurations:
                if not isinstance(config, dict):
                    continue
                state = config.get(WECHAT_IDENTITY_CONFIG_KEY)
                if not isinstance(state, dict):
                    continue
                fingerprint = state.get("verified_fingerprint")
                if isinstance(fingerprint, str) and fingerprint:
                    fingerprints.add(fingerprint)
        return fingerprints

    def _replace_wechat_identity_hash(
        self,
        previous_hash: str,
        current_hash: str,
    ) -> None:
        if self._store is None:
            return
        with self._store.engine.begin() as connection:
            rows = connection.execute(
                select(ChannelAccount.id, ChannelAccount.config_json)
            ).all()
            for account_id, config in rows:
                if not isinstance(config, dict):
                    continue
                state = config.get(WECHAT_IDENTITY_CONFIG_KEY)
                if (
                    not isinstance(state, dict)
                    or state.get("verified_fingerprint") != previous_hash
                ):
                    continue
                migrated_state = dict(state)
                migrated_state["verified_fingerprint"] = current_hash
                if migrated_state.get("observed_fingerprint") == previous_hash:
                    migrated_state["observed_fingerprint"] = current_hash
                migrated_config = dict(config)
                migrated_config[WECHAT_IDENTITY_CONFIG_KEY] = migrated_state
                connection.execute(
                    update(ChannelAccount)
                    .where(ChannelAccount.id == account_id)
                    .values(config_json=migrated_config, updated_at=utc_now())
                )

    def __repr__(self) -> str:
        return "AgentActivationSecurity(configured=True)"


@dataclass(frozen=True)
class PersonalWechatProvisioningMaterial:
    tenant_slug: str
    channel_external_account_id: str
    agent_external_id: str
    tenant_api_key: str
    tenant_key_record: ApiKeyRecord


class CenterProvisioningSecurity:
    """Authenticate the trusted provisioner and derive stable tenant material."""

    def __init__(
        self,
        token: str | bytes,
        key_manager: ApiKeyManager,
        *,
        store: TenantStore | None = None,
    ) -> None:
        if isinstance(token, str):
            token_bytes = token.encode("utf-8")
        elif isinstance(token, bytes):
            token_bytes = token
        else:
            raise TypeError("provisioning token must be str or bytes")
        if len(token_bytes) < ApiKeyManager.MIN_PEPPER_BYTES:
            raise ValueError(
                f"provisioning token must be at least {ApiKeyManager.MIN_PEPPER_BYTES} bytes"
            )
        self._token_digest = hashlib.sha256(token_bytes).digest()
        self._key_manager = key_manager
        self._store = store

    def authenticate(self, presented_token: str) -> bool:
        if not isinstance(presented_token, str):
            candidate = b""
        else:
            candidate = presented_token.encode("utf-8")
        return hmac.compare_digest(
            hashlib.sha256(candidate).digest(), self._token_digest
        )

    def derive(self, external_tenant_id: str) -> PersonalWechatProvisioningMaterial:
        if not isinstance(external_tenant_id, str) or not external_tenant_id:
            raise ValueError("external_tenant_id is required")
        external_bytes = external_tenant_id.encode("utf-8")
        external_digest = hashlib.sha256(external_bytes).hexdigest()
        tenant_slug = f"personal-wechat-{external_digest}"
        candidates = self._key_manager.derived_tenant_api_key_candidates(
            "personal-wechat-v1:" + external_tenant_id
        )
        tenant_key = self._find_existing_tenant_key(tenant_slug, candidates)
        return PersonalWechatProvisioningMaterial(
            tenant_slug=tenant_slug,
            channel_external_account_id=f"pending-wechat-{external_digest}",
            agent_external_id=f"personal-wechat-agent-{external_digest}",
            tenant_api_key=tenant_key.secret,
            tenant_key_record=tenant_key.record,
        )

    def _find_existing_tenant_key(
        self,
        tenant_slug: str,
        candidates: tuple[IssuedCredential, ...],
    ) -> IssuedCredential:
        if self._store is None:
            return candidates[0]
        parameters: dict[str, str] = {"tenant_slug": tenant_slug}
        placeholders: list[str] = []
        for index, candidate in enumerate(candidates):
            name = f"key_id_{index}"
            parameters[name] = candidate.record.key_id
            placeholders.append(f":{name}")
        statement = text(
            "SELECT access_credentials.key_id, access_credentials.prefix, "
            "access_credentials.key_hash "
            "FROM access_credentials "
            "JOIN tenants ON tenants.id = access_credentials.tenant_id "
            "WHERE tenants.slug = :tenant_slug "
            "AND access_credentials.status = 'active' "
            "AND access_credentials.key_id IN ("
            + ", ".join(placeholders)
            + ")"
        )
        with self._store.engine.connect() as connection:
            stored = {
                (row.key_id, row.prefix, row.key_hash)
                for row in connection.execute(statement, parameters)
            }
        for candidate in candidates:
            record = candidate.record
            if (record.key_id, record.prefix, record.key_hash) in stored:
                return candidate
        return candidates[0]

    def __repr__(self) -> str:
        return "CenterProvisioningSecurity(configured=True)"


class CenterAuthenticator:
    def __init__(self, store: TenantStore, key_manager: ApiKeyManager) -> None:
        self.store = store
        self.key_manager = key_manager

    def authenticate_tenant(
        self,
        tenant_id: str,
        token: str,
        permission: str,
    ) -> AuthPrincipal:
        credential = self._verified_credential(
            tenant_id,
            token,
            ApiKeyManager.TENANT_API_KEY_PREFIX,
        )
        if credential.subject_type != "user":
            raise AuthenticationFailed("invalid tenant credential")
        membership = self.store.get_membership(tenant_id, credential.subject_id)
        if membership is None:
            raise AuthenticationFailed("tenant membership not found")
        principal = AuthPrincipal(
            tenant_id=tenant_id,
            subject_id=credential.subject_id,
            subject_type="user",
            role=membership.role,
        )
        require_permission(principal, permission)
        self.store.mark_access_credential_used(tenant_id, credential.id)
        return principal

    def authenticate_agent(
        self,
        tenant_id: str,
        agent_id: str,
        channel_account_id: str,
        token: str,
    ) -> AuthPrincipal:
        credential = self._verified_credential(
            tenant_id,
            token,
            ApiKeyManager.AGENT_TOKEN_PREFIX,
        )
        if credential.subject_type != "agent" or credential.subject_id != agent_id:
            raise AuthenticationFailed("invalid Agent credential")
        agent = self.store.get_agent(tenant_id, agent_id)
        if agent.channel_account_id != channel_account_id:
            raise AuthenticationFailed("Agent is not bound to channel account")
        principal = AuthPrincipal(
            tenant_id=tenant_id,
            subject_id=agent_id,
            subject_type="agent",
            role="agent",
        )
        self.store.mark_access_credential_used(tenant_id, credential.id)
        return principal

    def _verified_credential(self, tenant_id: str, token: str, prefix: str):
        key_id = _credential_key_id(token, prefix)
        credential = self.store.get_access_credential(tenant_id, key_id, prefix)
        if credential is None:
            raise AuthenticationFailed("credential not found")
        record = ApiKeyRecord(
            key_id=credential.key_id,
            prefix=credential.prefix,
            key_hash=credential.key_hash,
        )
        if not self.key_manager.verify(token, record):
            raise AuthenticationFailed("credential verification failed")
        return credential


@dataclass(frozen=True)
class BootstrapResult:
    tenant_id: str
    owner_user_id: str
    channel_account_id: str
    agent_id: str
    tenant_api_key: str
    agent_token: str

    def to_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "channel_account_id": self.channel_account_id,
            "agent_id": self.agent_id,
            "tenant_api_key": self.tenant_api_key,
            "agent_token": self.agent_token,
        }


def bootstrap_tenant(
    store: TenantStore,
    key_manager: ApiKeyManager,
    *,
    tenant_slug: str,
    tenant_name: str,
    owner_email: str,
    channel: str,
    external_account_id: str,
    agent_external_id: str,
    agent_name: str = "",
) -> BootstrapResult:
    tenant_key = key_manager.generate_tenant_api_key()
    agent_token = key_manager.generate_agent_token()
    tenant, owner, channel_account, agent = store.bootstrap_tenant_bundle(
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
        owner_email=owner_email,
        channel=channel,
        external_account_id=external_account_id,
        agent_external_id=agent_external_id,
        agent_name=agent_name,
        tenant_key_id=tenant_key.record.key_id,
        tenant_key_prefix=tenant_key.record.prefix,
        tenant_key_hash=tenant_key.record.key_hash,
        agent_key_id=agent_token.record.key_id,
        agent_key_prefix=agent_token.record.prefix,
        agent_key_hash=agent_token.record.key_hash,
    )
    return BootstrapResult(
        tenant_id=tenant.id,
        owner_user_id=owner.id,
        channel_account_id=channel_account.id,
        agent_id=agent.id,
        tenant_api_key=tenant_key.secret,
        agent_token=agent_token.secret,
    )


def build_center_application(
    database_url: str,
    api_key_pepper: str,
    secret_key: str,
    *,
    create_schema: bool = True,
    provisioning_token: str = "",
    previous_api_key_peppers: Iterable[str | bytes] | None = None,
) -> tuple[TenantStore, CenterApplication]:
    previous_peppers = _configured_previous_api_key_peppers(
        previous_api_key_peppers
    )
    key_manager = ApiKeyManager(
        api_key_pepper,
        previous_peppers=previous_peppers,
    )
    secret_cipher = SecretCipher(secret_key)
    store = TenantStore(database_url)
    if create_schema:
        store.create_schema()
    authenticator = CenterAuthenticator(store, key_manager)
    application = CenterApplication(
        store,
        authenticator.authenticate_tenant,
        authenticator.authenticate_agent,
        secret_cipher,
        activation_security=AgentActivationSecurity(
            api_key_pepper,
            key_manager,
            store=store,
        ),
        provisioning_security=(
            CenterProvisioningSecurity(
                provisioning_token,
                key_manager,
                store=store,
            )
            if provisioning_token
            else None
        ),
    )
    return store, application


def center_diagnostics(
    database_url: str,
    api_key_pepper: str,
    secret_key: str,
    provisioning_token: str | None = None,
    *,
    previous_api_key_peppers: Iterable[str | bytes] | None = None,
) -> dict[str, object]:
    checks: dict[str, object] = {
        "database": False,
        "schema": False,
        "api_key_pepper": False,
        "secret_key": False,
    }
    errors: list[str] = []
    key_manager: ApiKeyManager | None = None
    try:
        key_manager = ApiKeyManager(
            api_key_pepper,
            previous_peppers=_configured_previous_api_key_peppers(
                previous_api_key_peppers
            ),
        )
        checks["api_key_pepper"] = True
    except (TypeError, ValueError) as exc:
        errors.append(f"api_key_pepper: {exc}")
    try:
        SecretCipher(secret_key)
        checks["secret_key"] = True
    except (TypeError, ValueError) as exc:
        errors.append(f"secret_key: {exc}")
    if provisioning_token is not None:
        checks["provisioning_token"] = False
        try:
            if key_manager is None:
                raise ValueError("API key pepper configuration is invalid")
            CenterProvisioningSecurity(
                provisioning_token,
                key_manager,
            )
            checks["provisioning_token"] = True
        except (TypeError, ValueError) as exc:
            errors.append(f"provisioning_token: {exc}")

    try:
        from wechat_ai.center_migrations import migration_status

        store = TenantStore(database_url)
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
        expected = set(Base.metadata.tables)
        actual = set(inspect(store.engine).get_table_names())
        missing = sorted(expected - actual)
        status = migration_status(store.engine)
        checks["schema"] = not missing and status.at_head and status.schema_valid
        checks["missing_tables"] = missing
        checks["migrations"] = status.to_dict()
        store.engine.dispose()
    except Exception as exc:
        errors.append(f"database: {exc}")

    required_checks = ["database", "schema", "api_key_pepper", "secret_key"]
    if provisioning_token is not None:
        required_checks.append("provisioning_token")
    return {
        "ok": all(checks[name] for name in required_checks),
        "checks": checks,
        "errors": errors,
    }


def center_service_readiness(store: TenantStore, worker: Any) -> dict[str, object]:
    checks: dict[str, object] = {
        "database": False,
        "migrations": {"at_head": False, "schema_valid": False},
        "worker": {"ok": False},
    }
    try:
        checks["database"] = bool(store.healthcheck())
    except Exception:
        checks["database"] = False
    try:
        from wechat_ai.center_migrations import migration_status

        checks["migrations"] = migration_status(store.engine).to_dict()
    except Exception:
        pass
    try:
        checks["worker"] = worker.health_report()
    except Exception:
        pass
    migration_check = checks["migrations"]
    worker_check = checks["worker"]
    ready = bool(
        checks["database"]
        and isinstance(migration_check, dict)
        and migration_check.get("at_head")
        and migration_check.get("schema_valid")
        and isinstance(worker_check, dict)
        and worker_check.get("ok")
    )
    return {"ok": ready, "checks": checks}


def _credential_key_id(token: str, prefix: str) -> str:
    if not isinstance(token, str) or not token.startswith(prefix):
        raise AuthenticationFailed("invalid credential format")
    key_id, separator, secret = token[len(prefix) :].partition(".")
    if not separator or not key_id or not secret:
        raise AuthenticationFailed("invalid credential format")
    return key_id


def _configured_previous_api_key_peppers(
    explicit: Iterable[str | bytes] | None,
) -> Iterable[str | bytes]:
    if explicit is not None:
        return explicit
    from wechat_ai.config import center_api_key_previous_peppers_from_env

    return center_api_key_previous_peppers_from_env()


__all__ = [
    "AgentActivationSecurity",
    "BootstrapResult",
    "CenterAuthenticator",
    "CenterProvisioningSecurity",
    "PersonalWechatProvisioningMaterial",
    "bootstrap_tenant",
    "build_center_application",
    "center_diagnostics",
    "center_service_readiness",
]
