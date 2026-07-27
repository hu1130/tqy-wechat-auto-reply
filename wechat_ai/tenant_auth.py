from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from cryptography.fernet import Fernet, InvalidToken


RandomBytes = Callable[[int], bytes]


class PasswordHasher:
    """Versioned PBKDF2-HMAC-SHA256 password hashing."""

    SCHEME = "pbkdf2_sha256"
    VERSION = 1

    def __init__(
        self,
        *,
        iterations: int = 600_000,
        salt_size: int = 16,
        dklen: int = 32,
        random_bytes: RandomBytes = secrets.token_bytes,
    ) -> None:
        if iterations <= 0:
            raise ValueError("iterations must be positive")
        if salt_size < 16:
            raise ValueError("salt_size must be at least 16 bytes")
        if dklen < 32:
            raise ValueError("dklen must be at least 32 bytes")
        self._iterations = iterations
        self._salt_size = salt_size
        self._dklen = dklen
        self._random_bytes = random_bytes

    def hash(self, password: str) -> str:
        password_bytes = _password_bytes(password)
        salt = self._random_bytes(self._salt_size)
        if not isinstance(salt, bytes) or len(salt) != self._salt_size:
            raise ValueError("random_bytes returned an invalid salt")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password_bytes,
            salt,
            self._iterations,
            dklen=self._dklen,
        )
        return "$".join(
            (
                self.SCHEME,
                str(self.VERSION),
                str(self._iterations),
                str(self._dklen),
                _b64encode(salt),
                _b64encode(digest),
            )
        )

    def verify(self, password: str, encoded: str) -> bool:
        try:
            password_bytes = _password_bytes(password)
            scheme, version, iterations, dklen, salt_text, expected_text = encoded.split("$")
            if scheme != self.SCHEME or int(version) != self.VERSION:
                return False
            parsed_iterations = int(iterations)
            parsed_dklen = int(dklen)
            if parsed_iterations <= 0 or parsed_dklen < 32:
                return False
            salt = _b64decode(salt_text)
            expected = _b64decode(expected_text)
            if len(salt) < 16 or len(expected) != parsed_dklen:
                return False
            actual = hashlib.pbkdf2_hmac(
                "sha256",
                password_bytes,
                salt,
                parsed_iterations,
                dklen=parsed_dklen,
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class ApiKeyRecord:
    """The only API-key material that should be persisted."""

    key_id: str
    prefix: str
    key_hash: str


@dataclass(frozen=True)
class IssuedCredential:
    """One-time credential result; persist only ``record``."""

    secret: str = field(repr=False)
    record: ApiKeyRecord


class ApiKeyManager:
    TENANT_API_KEY_PREFIX = "tenant_api_"
    AGENT_TOKEN_PREFIX = "agent_token_"
    MIN_PEPPER_BYTES = 32
    MAX_PEPPER_BYTES = 256
    MAX_PREVIOUS_PEPPERS = 4
    MAX_CREDENTIAL_BYTES = 512
    MAX_DERIVATION_CONTEXT_BYTES = 512

    def __init__(
        self,
        pepper: str | bytes,
        *,
        previous_peppers: Iterable[str | bytes] | None = None,
        random_bytes: RandomBytes = secrets.token_bytes,
    ) -> None:
        current = self._normalize_pepper(pepper, "current pepper")
        previous = self._normalize_previous_peppers(previous_peppers)
        all_peppers = (current, *previous)
        if len(set(all_peppers)) != len(all_peppers):
            raise ValueError("current and previous peppers must be unique")
        self._peppers = all_peppers
        self._random_bytes = random_bytes

    def generate_tenant_api_key(self) -> IssuedCredential:
        return self._generate(self.TENANT_API_KEY_PREFIX)

    def generate_agent_token(self) -> IssuedCredential:
        return self._generate(self.AGENT_TOKEN_PREFIX)

    def derive_tenant_api_key(self, context: str) -> IssuedCredential:
        return self.derived_tenant_api_key_candidates(context)[0]

    def derived_tenant_api_key_candidates(
        self,
        context: str,
    ) -> tuple[IssuedCredential, ...]:
        """Return current first; previous candidates are for existing-record lookup only."""
        if not isinstance(context, str) or not context.strip():
            raise ValueError("credential derivation context is required")
        context_bytes = context.encode("utf-8")
        if len(context_bytes) > self.MAX_DERIVATION_CONTEXT_BYTES:
            raise ValueError(
                "credential derivation context cannot exceed "
                f"{self.MAX_DERIVATION_CONTEXT_BYTES} bytes"
            )
        credentials: list[IssuedCredential] = []
        for pepper in self._peppers:
            key_id = _b64encode(
                hmac.new(
                    pepper,
                    b"tenant-api-key-id-v1\x00" + context_bytes,
                    hashlib.sha256,
                ).digest()[:9]
            )
            secret_part = _b64encode(
                hmac.new(
                    pepper,
                    b"tenant-api-key-secret-v1\x00" + context_bytes,
                    hashlib.sha256,
                ).digest()
            )
            plaintext = f"{self.TENANT_API_KEY_PREFIX}{key_id}.{secret_part}"
            credentials.append(
                IssuedCredential(
                    secret=plaintext,
                    record=ApiKeyRecord(
                        key_id=key_id,
                        prefix=self.TENANT_API_KEY_PREFIX,
                        key_hash=hmac.new(
                            pepper,
                            plaintext.encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest(),
                    ),
                )
            )
        return tuple(credentials)

    def record_for_secret(
        self,
        plaintext: str,
        *,
        expected_prefix: str,
    ) -> ApiKeyRecord:
        if expected_prefix not in {
            self.TENANT_API_KEY_PREFIX,
            self.AGENT_TOKEN_PREFIX,
        }:
            raise ValueError("unsupported credential prefix")
        if not isinstance(plaintext, str) or not plaintext.startswith(expected_prefix):
            raise ValueError("invalid credential format")
        try:
            plaintext_bytes = plaintext.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("invalid credential format") from exc
        if len(plaintext_bytes) > self.MAX_CREDENTIAL_BYTES:
            raise ValueError("invalid credential format")
        key_id, separator, secret_part = plaintext[len(expected_prefix) :].partition(".")
        if (
            not separator
            or not key_id
            or len(key_id) > 64
            or not secret_part
            or "." in key_id
        ):
            raise ValueError("invalid credential format")
        return ApiKeyRecord(
            key_id=key_id,
            prefix=expected_prefix,
            key_hash=self._digest_current(plaintext_bytes),
        )

    def verify(self, presented_secret: str, record: ApiKeyRecord) -> bool:
        if not isinstance(presented_secret, str) or not isinstance(record, ApiKeyRecord):
            return False

        try:
            presented_bytes = presented_secret.encode("utf-8")
        except UnicodeError:
            presented_bytes = b""
        length_matches = len(presented_bytes) <= self.MAX_CREDENTIAL_BYTES
        digest_input = presented_bytes if length_matches else b""
        candidate_hashes = self.digest_hex_candidates(digest_input)
        dot_index = presented_secret.find(".")
        presented_key_id = (
            presented_secret[len(record.prefix) : dot_index]
            if dot_index >= len(record.prefix)
            else ""
        )
        presented_prefix = presented_secret[: len(record.prefix)]

        prefix_matches = hmac.compare_digest(presented_prefix, record.prefix)
        key_id_matches = hmac.compare_digest(presented_key_id, record.key_id)
        hash_matches = 0
        for candidate_hash in candidate_hashes:
            hash_matches |= hmac.compare_digest(candidate_hash, record.key_hash)
        return bool(length_matches & prefix_matches & key_id_matches & hash_matches)

    def current_hmac(self, value: bytes) -> bytes:
        if not isinstance(value, bytes):
            raise TypeError("HMAC input must be bytes")
        return hmac.new(self._peppers[0], value, hashlib.sha256).digest()

    def hmac_candidates(self, value: bytes) -> tuple[bytes, ...]:
        if not isinstance(value, bytes):
            raise TypeError("HMAC input must be bytes")
        return tuple(
            hmac.new(pepper, value, hashlib.sha256).digest()
            for pepper in self._peppers
        )

    def digest_hex_candidates(self, value: bytes) -> tuple[str, ...]:
        return tuple(digest.hex() for digest in self.hmac_candidates(value))

    def _generate(self, prefix: str) -> IssuedCredential:
        key_id = _urlsafe_random(self._random_bytes, 9)
        secret_part = _urlsafe_random(self._random_bytes, 32)
        plaintext = f"{prefix}{key_id}.{secret_part}"
        record = self.record_for_secret(
            plaintext,
            expected_prefix=prefix,
        )
        return IssuedCredential(secret=plaintext, record=record)

    def _digest_current(self, plaintext: bytes) -> str:
        return self.current_hmac(plaintext).hex()

    @classmethod
    def _normalize_pepper(cls, pepper: str | bytes, label: str) -> bytes:
        if isinstance(pepper, str):
            pepper_bytes = pepper.encode("utf-8")
        elif isinstance(pepper, bytes):
            pepper_bytes = pepper
        else:
            raise TypeError(f"{label} must be str or bytes")
        if len(pepper_bytes) < cls.MIN_PEPPER_BYTES:
            raise ValueError(
                f"{label} must be at least {cls.MIN_PEPPER_BYTES} bytes"
            )
        if len(pepper_bytes) > cls.MAX_PEPPER_BYTES:
            raise ValueError(
                f"{label} cannot exceed {cls.MAX_PEPPER_BYTES} bytes"
            )
        return pepper_bytes

    @classmethod
    def _normalize_previous_peppers(
        cls,
        previous_peppers: Iterable[str | bytes] | None,
    ) -> tuple[bytes, ...]:
        if previous_peppers is None:
            return ()
        if isinstance(previous_peppers, (str, bytes)):
            raise TypeError("previous_peppers must be an iterable of peppers")
        normalized: list[bytes] = []
        for index, pepper in enumerate(previous_peppers):
            if index >= cls.MAX_PREVIOUS_PEPPERS:
                raise ValueError(
                    "previous_peppers cannot contain more than "
                    f"{cls.MAX_PREVIOUS_PEPPERS} entries"
                )
            normalized.append(
                cls._normalize_pepper(pepper, f"previous pepper {index + 1}")
            )
        return tuple(normalized)

    def __repr__(self) -> str:
        return (
            "ApiKeyManager(configured=True, "
            f"previous_pepper_count={len(self._peppers) - 1})"
        )


class SecretCipherError(ValueError):
    pass


class SecretCipher:
    DEFAULT_ENV_VAR = "TENANT_SECRET_FERNET_KEY"

    def __init__(self, key: str | bytes) -> None:
        if isinstance(key, str):
            key_bytes = key.encode("ascii") if key else b""
        elif isinstance(key, bytes):
            key_bytes = key
        else:
            raise TypeError("Fernet key must be str or bytes")
        if not key_bytes:
            raise SecretCipherError("Fernet key is required")
        try:
            self._fernet = Fernet(key_bytes)
        except (TypeError, ValueError) as exc:
            raise SecretCipherError("invalid Fernet key") from exc

    @classmethod
    def from_env(
        cls,
        variable_name: str = DEFAULT_ENV_VAR,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> SecretCipher:
        source = os.environ if environ is None else environ
        return cls(source.get(variable_name, ""))

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be str")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str) or not ciphertext:
            raise SecretCipherError("ciphertext is required")
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except (InvalidToken, TypeError, ValueError, UnicodeError) as exc:
            raise SecretCipherError("ciphertext authentication failed") from exc

    def __repr__(self) -> str:
        return "SecretCipher(configured=True)"


TENANT_MANAGE = "tenant.manage"
MEMBERS_MANAGE = "members.manage"
CHANNELS_MANAGE = "channels.manage"
AGENTS_MANAGE = "agents.manage"
MESSAGES_READ = "messages.read"
DELIVERIES_MANAGE = "deliveries.manage"
AUDIT_READ = "audit.read"

PERMISSIONS = frozenset(
    {
        TENANT_MANAGE,
        MEMBERS_MANAGE,
        CHANNELS_MANAGE,
        AGENTS_MANAGE,
        MESSAGES_READ,
        DELIVERIES_MANAGE,
        AUDIT_READ,
    }
)

ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "owner": PERMISSIONS,
        "admin": frozenset(
            {
                MEMBERS_MANAGE,
                CHANNELS_MANAGE,
                AGENTS_MANAGE,
                MESSAGES_READ,
                DELIVERIES_MANAGE,
                AUDIT_READ,
            }
        ),
        "agent": frozenset({MESSAGES_READ, DELIVERIES_MANAGE}),
        "viewer": frozenset({MESSAGES_READ, AUDIT_READ}),
    }
)


@dataclass(frozen=True)
class AuthPrincipal:
    tenant_id: str
    subject_id: str
    subject_type: str
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
        object.__setattr__(self, "tenant_id", self.tenant_id.strip())


class PermissionDenied(PermissionError):
    pass


class AuthenticationFailed(PermissionError):
    pass


def require_permission(principal: AuthPrincipal, permission: str) -> AuthPrincipal:
    if not isinstance(principal, AuthPrincipal):
        raise PermissionDenied("authentication principal is required")
    allowed = ROLE_PERMISSIONS.get(principal.role)
    if allowed is None or permission not in PERMISSIONS or permission not in allowed:
        raise PermissionDenied("permission denied")
    return principal


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str):
        raise TypeError("password must be str")
    return password.encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _urlsafe_random(random_bytes: RandomBytes, size: int) -> str:
    value = random_bytes(size)
    if not isinstance(value, bytes) or len(value) != size:
        raise ValueError("random_bytes returned an invalid value")
    return _b64encode(value)
