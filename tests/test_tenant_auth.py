from __future__ import annotations

import dataclasses
import unittest

from cryptography.fernet import Fernet

from wechat_ai.tenant_auth import (
    AGENTS_MANAGE,
    AUDIT_READ,
    CHANNELS_MANAGE,
    DELIVERIES_MANAGE,
    MEMBERS_MANAGE,
    MESSAGES_READ,
    PERMISSIONS,
    ROLE_PERMISSIONS,
    TENANT_MANAGE,
    ApiKeyManager,
    AuthPrincipal,
    PasswordHasher,
    PermissionDenied,
    SecretCipher,
    SecretCipherError,
    require_permission,
)


class PasswordHasherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hasher = PasswordHasher(iterations=1_000)

    def test_hashes_are_salted_and_do_not_contain_plaintext(self) -> None:
        first = self.hasher.hash("correct horse battery staple")
        second = self.hasher.hash("correct horse battery staple")

        self.assertNotEqual(first, second)
        self.assertNotIn("correct horse battery staple", first)
        self.assertTrue(first.startswith("pbkdf2_sha256$1$1000$"))

    def test_correct_password_verifies_and_wrong_password_does_not(self) -> None:
        encoded = self.hasher.hash("secret")

        self.assertTrue(self.hasher.verify("secret", encoded))
        self.assertFalse(self.hasher.verify("wrong", encoded))

    def test_parameters_are_encoded_and_verifiable_by_another_instance(self) -> None:
        encoded = PasswordHasher(iterations=1_111, dklen=48).hash("secret")

        self.assertTrue(PasswordHasher(iterations=2_222).verify("secret", encoded))

    def test_tampered_and_malformed_hashes_fail_closed(self) -> None:
        encoded = self.hasher.hash("secret")
        parts = encoded.split("$")
        parts[-1] = ("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]
        tampered = "$".join(parts)

        self.assertFalse(self.hasher.verify("secret", tampered))
        self.assertFalse(self.hasher.verify("secret", "not-a-password-hash"))
        self.assertFalse(self.hasher.verify("secret", encoded.replace("$1$", "$2$", 1)))


class ApiKeyManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ApiKeyManager("p" * 32)

    def test_generates_recognizable_tenant_and_agent_prefixes(self) -> None:
        tenant_key = self.manager.generate_tenant_api_key()
        agent_token = self.manager.generate_agent_token()

        self.assertTrue(tenant_key.secret.startswith("tenant_api_"))
        self.assertTrue(agent_token.secret.startswith("agent_token_"))
        self.assertTrue(self.manager.verify(tenant_key.secret, tenant_key.record))
        self.assertTrue(self.manager.verify(agent_token.secret, agent_token.record))

    def test_persisted_record_contains_only_identifier_prefix_and_digest(self) -> None:
        issued = self.manager.generate_tenant_api_key()
        stored = dataclasses.asdict(issued.record)

        self.assertEqual(set(stored), {"key_id", "prefix", "key_hash"})
        self.assertNotIn(issued.secret, stored.values())
        self.assertEqual(len(stored["key_hash"]), 64)
        self.assertNotIn(issued.secret, repr(issued))

    def test_modified_and_cross_key_credentials_fail(self) -> None:
        first = self.manager.generate_tenant_api_key()
        second = self.manager.generate_tenant_api_key()

        self.assertFalse(self.manager.verify(first.secret + "x", first.record))
        self.assertFalse(self.manager.verify(first.secret, second.record))

    def test_different_pepper_cannot_verify(self) -> None:
        issued = self.manager.generate_agent_token()

        self.assertFalse(ApiKeyManager("q" * 32).verify(issued.secret, issued.record))

    def test_previous_pepper_keeps_old_tokens_valid_until_removed(self) -> None:
        old_manager = ApiKeyManager("o" * 32)
        old_tenant_key = old_manager.generate_tenant_api_key()
        old_agent_token = old_manager.generate_agent_token()
        rotated = ApiKeyManager(
            "n" * 32,
            previous_peppers=["o" * 32],
        )

        self.assertTrue(rotated.verify(old_tenant_key.secret, old_tenant_key.record))
        self.assertTrue(rotated.verify(old_agent_token.secret, old_agent_token.record))

        after_removal = ApiKeyManager("n" * 32)
        self.assertFalse(
            after_removal.verify(old_tenant_key.secret, old_tenant_key.record)
        )
        self.assertFalse(
            after_removal.verify(old_agent_token.secret, old_agent_token.record)
        )

    def test_new_tokens_and_derived_keys_use_only_the_current_pepper(self) -> None:
        old_manager = ApiKeyManager("o" * 32)
        rotated = ApiKeyManager(
            "n" * 32,
            previous_peppers=["o" * 32],
        )

        new_agent_token = rotated.generate_agent_token()
        new_tenant_key = rotated.derive_tenant_api_key(
            "personal-wechat-v1:tenant-a"
        )

        self.assertTrue(rotated.verify(new_agent_token.secret, new_agent_token.record))
        self.assertTrue(rotated.verify(new_tenant_key.secret, new_tenant_key.record))
        self.assertFalse(
            old_manager.verify(new_agent_token.secret, new_agent_token.record)
        )
        self.assertFalse(old_manager.verify(new_tenant_key.secret, new_tenant_key.record))

    def test_pepper_key_ring_rejects_unsafe_configuration(self) -> None:
        invalid_configurations = (
            {"previous_peppers": ["short"]},
            {"previous_peppers": ["p" * 32]},
            {"previous_peppers": ["a" * 32, "a" * 32]},
            {"previous_peppers": ["a" * 32] * 5},
            {"previous_peppers": "a" * 32},
            {"previous_peppers": ["a" * 257]},
        )
        for kwargs in invalid_configurations:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    ApiKeyManager("p" * 32, **kwargs)
        with self.assertRaises(ValueError):
            ApiKeyManager("p" * 257)

    def test_key_ring_repr_and_errors_do_not_contain_peppers(self) -> None:
        current = "current-secret-" + "c" * 32
        previous = "previous-secret-" + "p" * 32
        manager = ApiKeyManager(current, previous_peppers=[previous])

        rendered = repr(manager)
        self.assertNotIn(current, rendered)
        self.assertNotIn(previous, rendered)

    def test_derived_tenant_key_is_stable_per_context_and_pepper(self) -> None:
        first = self.manager.derive_tenant_api_key("personal-wechat-v1:tenant-a")
        repeated = ApiKeyManager("p" * 32).derive_tenant_api_key(
            "personal-wechat-v1:tenant-a"
        )
        another_tenant = self.manager.derive_tenant_api_key(
            "personal-wechat-v1:tenant-b"
        )
        another_pepper = ApiKeyManager("q" * 32).derive_tenant_api_key(
            "personal-wechat-v1:tenant-a"
        )

        self.assertEqual(first.secret, repeated.secret)
        self.assertNotEqual(first.secret, another_tenant.secret)
        self.assertNotEqual(first.secret, another_pepper.secret)
        self.assertTrue(self.manager.verify(first.secret, first.record))

    def test_derived_tenant_key_requires_a_nonempty_context(self) -> None:
        for context in ("", "   "):
            with self.subTest(context=context):
                with self.assertRaises(ValueError):
                    self.manager.derive_tenant_api_key(context)

    def test_empty_or_short_pepper_fails_closed(self) -> None:
        for pepper in ("", b"", "short", b"short"):
            with self.subTest(pepper=pepper):
                with self.assertRaises(ValueError):
                    ApiKeyManager(pepper)


class SecretCipherTests(unittest.TestCase):
    def test_constructs_from_environment_and_round_trips(self) -> None:
        key = Fernet.generate_key().decode("ascii")
        cipher = SecretCipher.from_env(environ={"TENANT_SECRET_FERNET_KEY": key})
        ciphertext = cipher.encrypt("tenant database password")

        self.assertNotIn("tenant database password", ciphertext)
        self.assertEqual(cipher.decrypt(ciphertext), "tenant database password")
        self.assertNotIn("tenant database password", repr(cipher))
        self.assertNotIn(key, repr(cipher))

    def test_empty_and_invalid_keys_fail_closed(self) -> None:
        for key in ("", "not-a-fernet-key"):
            with self.subTest(key=key):
                with self.assertRaises(SecretCipherError):
                    SecretCipher(key)
        with self.assertRaises(SecretCipherError):
            SecretCipher.from_env(environ={})

    def test_tampered_ciphertext_fails(self) -> None:
        cipher = SecretCipher(Fernet.generate_key())
        ciphertext = cipher.encrypt("secret")
        tampered = ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B")

        with self.assertRaises(SecretCipherError):
            cipher.decrypt(tampered)

    def test_ciphertext_cannot_be_decrypted_with_another_key(self) -> None:
        first = SecretCipher(Fernet.generate_key())
        second = SecretCipher(Fernet.generate_key())

        with self.assertRaises(SecretCipherError):
            second.decrypt(first.encrypt("secret"))


class RbacTests(unittest.TestCase):
    EXPECTED = {
        "owner": {
            TENANT_MANAGE,
            MEMBERS_MANAGE,
            CHANNELS_MANAGE,
            AGENTS_MANAGE,
            MESSAGES_READ,
            DELIVERIES_MANAGE,
            AUDIT_READ,
        },
        "admin": {
            MEMBERS_MANAGE,
            CHANNELS_MANAGE,
            AGENTS_MANAGE,
            MESSAGES_READ,
            DELIVERIES_MANAGE,
            AUDIT_READ,
        },
        "agent": {MESSAGES_READ, DELIVERIES_MANAGE},
        "viewer": {MESSAGES_READ, AUDIT_READ},
    }

    def test_complete_role_permission_matrix(self) -> None:
        self.assertEqual(set(ROLE_PERMISSIONS), set(self.EXPECTED))
        for role, expected_permissions in self.EXPECTED.items():
            principal = AuthPrincipal("tenant-1", f"user-{role}", "user", role)
            self.assertEqual(set(ROLE_PERMISSIONS[role]), expected_permissions)
            for permission in PERMISSIONS:
                with self.subTest(role=role, permission=permission):
                    if permission in expected_permissions:
                        self.assertIs(require_permission(principal, permission), principal)
                    else:
                        with self.assertRaises(PermissionDenied):
                            require_permission(principal, permission)

    def test_unknown_role_and_permission_are_denied(self) -> None:
        unknown = AuthPrincipal("tenant-1", "user-1", "user", "superadmin")
        owner = AuthPrincipal("tenant-1", "owner-1", "user", "owner")

        with self.assertRaises(PermissionDenied):
            require_permission(unknown, MESSAGES_READ)
        with self.assertRaises(PermissionDenied):
            require_permission(owner, "unknown.permission")

    def test_empty_tenant_id_is_rejected(self) -> None:
        for tenant_id in ("", "   "):
            with self.subTest(tenant_id=tenant_id):
                with self.assertRaises(ValueError):
                    AuthPrincipal(tenant_id, "user-1", "user", "owner")


if __name__ == "__main__":
    unittest.main()
