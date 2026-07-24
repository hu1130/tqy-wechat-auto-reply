from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from wechat_ai.agent_activation import (
    ActivationError,
    ActivationResult,
    AgentActivationClient,
    activate_and_save,
    resolve_center_url,
    validate_center_url,
)
import wechat_ai.agent_activation as agent_activation
from wechat_ai.agent_profile import (
    AgentProfile,
    AgentProfileStore,
    WindowsDPAPICipher,
    default_agent_data_dir,
    public_profile_status,
)


class XorCipher:
    def encrypt(self, plaintext: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return bytes(value ^ 0xA5 for value in ciphertext)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        status: int = 200,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.raw = json.dumps(payload).encode("utf-8")
        self.closed = False
        self.read_error = read_error

    def read(self, amount: int = -1) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.raw if amount < 0 else self.raw[:amount]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[Request, float]] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def profile(token: str = "agent_token_top_secret") -> AgentProfile:
    return AgentProfile(
        center_url="https://center.example.test",
        tenant_id="tenant-12345678",
        agent_instance_id="agent-12345678",
        channel_account_id="channel-12345678",
        agent_token=token,
        device_id="device-12345678",
        device_name="QA-PC",
        agent_version="1.2.3",
        activated_at="2026-07-22T10:00:00+00:00",
    )


class AgentProfileTests(unittest.TestCase):
    def make_store(self, root: Path) -> AgentProfileStore:
        store = AgentProfileStore(root, cipher=XorCipher())
        with patch("wechat_ai.agent_profile.uuid.uuid4", return_value="device-12345678"):
            self.assertEqual(store.get_or_create_device_id(), "device-12345678")
        return store

    def test_profile_encrypts_token_persists_device_and_uses_user_data_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = self.make_store(root)
            original = profile()

            store.save(original)
            loaded = store.load()
            raw_profile = store.paths.profile.read_text(encoding="utf-8")
            overrides = store.config_overrides(loaded)

            self.assertEqual(loaded, original)
            self.assertNotIn(original.agent_token, raw_profile)
            self.assertNotIn("activation", raw_profile.lower())
            self.assertEqual(store.get_or_create_device_id(), original.device_id)
            self.assertEqual(overrides["agent_spool_file"], str(root / "data" / "agent-spool.sqlite3"))
            self.assertEqual(overrides["log_file"], str(root / "logs" / "agent.log"))
            self.assertEqual(overrides["health_file"], str(root / "logs" / "health.json"))
            self.assertTrue(store.paths.runtime_marker.is_file())

    def test_failed_atomic_replace_preserves_existing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            store.save(profile("first-secret"))
            original_bytes = store.paths.profile.read_bytes()

            with patch("wechat_ai.agent_profile.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    store.save(profile("second-secret"))

            self.assertEqual(store.paths.profile.read_bytes(), original_bytes)
            self.assertFalse(list(store.paths.root.glob("*.tmp")))

    def test_reset_removes_binding_but_preserves_device_identity_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            store.save(profile())
            store.paths.spool.parent.mkdir(parents=True, exist_ok=True)
            store.paths.spool.write_bytes(b"state")

            self.assertTrue(store.reset_binding())

            self.assertFalse(store.is_activated())
            self.assertTrue(store.paths.device.is_file())
            self.assertEqual(store.get_or_create_device_id(), "device-12345678")
            self.assertEqual(store.paths.spool.read_bytes(), b"state")

    def test_public_status_never_contains_plaintext_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            store.save(profile())

            rendered = json.dumps(public_profile_status(store))

            self.assertNotIn("agent_token_top_secret", rendered)
            self.assertIn('"agent_token": "protected"', rendered)
            self.assertNotIn("tenant-12345678", rendered)

    def test_non_windows_default_cipher_refuses_plaintext_fallback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "inject a TokenCipher"):
            WindowsDPAPICipher(platform_name="linux")

    def test_dpapi_decryption_failure_is_reported_without_cipher_details(self) -> None:
        class FailingCipher(XorCipher):
            def decrypt(self, ciphertext: bytes) -> bytes:
                del ciphertext
                raise OSError("sensitive provider detail")

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store = self.make_store(root)
            store.save(profile())
            failing_store = AgentProfileStore(root, cipher=FailingCipher())

            with self.assertRaises(RuntimeError) as raised:
                failing_store.load()

            self.assertEqual(str(raised.exception), "Cannot decrypt the Agent credential.")
            self.assertNotIn("sensitive provider detail", str(raised.exception))

    def test_default_data_directory_is_local_app_data_scoped(self) -> None:
        path = default_agent_data_dir({"LOCALAPPDATA": r"C:\Users\qa\AppData\Local"})
        self.assertEqual(path, Path(r"C:\Users\qa\AppData\Local") / "DeepAwake" / "WeChatAgent")


class AgentActivationClientTests(unittest.TestCase):
    def test_https_is_required_except_for_loopback(self) -> None:
        self.assertEqual(validate_center_url("http://localhost:8793/"), "http://localhost:8793")
        self.assertEqual(validate_center_url("http://127.0.0.1:8793"), "http://127.0.0.1:8793")
        self.assertEqual(validate_center_url("http://[::1]:8793"), "http://[::1]:8793")
        with self.assertRaisesRegex(ActivationError, "HTTPS is required"):
            validate_center_url("http://center.example.test")

    def test_center_url_resolves_explicit_then_environment(self) -> None:
        environ = {"WECHAT_AGENT_CENTER_URL": "https://environment.example.test/"}
        self.assertEqual(
            resolve_center_url("https://explicit.example.test", environ),
            "https://explicit.example.test",
        )
        self.assertEqual(
            resolve_center_url(None, environ),
            "https://environment.example.test",
        )

    def test_center_url_reads_pyinstaller_bundle_config_before_constant(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "agent-build.json").write_text(
                json.dumps({"center_url": "https://bundled.example.test/"}),
                encoding="utf-8-sig",
            )
            with patch.object(agent_activation.sys, "_MEIPASS", str(root), create=True):
                with patch.object(agent_activation, "BUILTIN_AGENT_CENTER_URL", "https://constant.test"):
                    self.assertEqual(
                        resolve_center_url(None, {}),
                        "https://bundled.example.test",
                    )

    def test_center_url_reads_frozen_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "agent-build.json").write_text(
                json.dumps({"center_url": "https://exe-bundle.example.test"}),
                encoding="utf-8",
            )
            with patch.object(agent_activation.sys, "frozen", True, create=True):
                with patch.object(agent_activation.sys, "executable", str(root / "wechat-agent.exe")):
                    self.assertEqual(
                        resolve_center_url(None, {}),
                        "https://exe-bundle.example.test",
                    )

    def test_bundled_center_url_is_https_validated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "agent-build.json").write_text(
                json.dumps({"center_url": "http://production.example.test"}),
                encoding="utf-8",
            )
            with patch.object(agent_activation.sys, "_MEIPASS", str(root), create=True):
                with self.assertRaisesRegex(ActivationError, "HTTPS is required"):
                    resolve_center_url(None, {})

    def test_activation_posts_exact_contract_and_parses_credentials(self) -> None:
        response = FakeResponse(
            {
                "tenant_id": "tenant-1",
                "agent_instance_id": "agent-1",
                "channel_account_id": "channel-1",
                "agent_token": "agent-token-1",
            }
        )
        opener = FakeOpener(response)
        client = AgentActivationClient(
            "https://center.example.test/root/",
            timeout_seconds=7,
            opener=opener,
        )

        result = client.activate(
            activation_code="one-time-code",
            device_id="device-1",
            device_name="QA-PC",
            agent_version="1.2.3",
        )

        self.assertEqual(result.agent_token, "agent-token-1")
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://center.example.test/root/v1/agent/activate")
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data or b"{}"),
            {
                "activation_code": "one-time-code",
                "device_id": "device-1",
                "device_name": "QA-PC",
                "agent_version": "1.2.3",
            },
        )
        self.assertTrue(response.closed)

    def test_http_failure_never_echoes_code_or_response_secret(self) -> None:
        secret_code = "activation-code-that-must-not-leak"
        error = HTTPError(
            "https://center.example.test/v1/agent/activate",
            403,
            "agent-token-that-must-not-leak",
            None,
            None,
        )
        client = AgentActivationClient(
            "https://center.example.test",
            opener=FakeOpener(error),
        )

        with self.assertRaises(ActivationError) as raised:
            client.activate(
                activation_code=secret_code,
                device_id="device-1",
                device_name="QA-PC",
                agent_version="1.2.3",
            )

        message = str(raised.exception)
        self.assertNotIn(secret_code, message)
        self.assertNotIn("agent-token-that-must-not-leak", message)
        self.assertEqual(message, "Agent activation was rejected (HTTP 403).")

    def test_response_read_failure_is_redacted_and_closes_response(self) -> None:
        secret_code = "activation-code-that-must-not-leak"
        response = FakeResponse({}, read_error=OSError("transport failed"))
        client = AgentActivationClient(
            "https://center.example.test",
            opener=FakeOpener(response),
        )

        with self.assertRaises(ActivationError) as raised:
            client.activate(
                activation_code=secret_code,
                device_id="device-1",
                device_name="QA-PC",
                agent_version="1.2.3",
            )

        self.assertEqual(str(raised.exception), "Cannot read the Agent center response.")
        self.assertNotIn(secret_code, str(raised.exception))
        self.assertTrue(response.closed)

    def test_activate_and_save_does_not_persist_activation_code(self) -> None:
        class Client:
            def activate(self, **payload: str) -> ActivationResult:
                self.payload = payload
                return ActivationResult("tenant-1", "agent-1", "channel-1", "token-1")

        with tempfile.TemporaryDirectory() as raw_root:
            store = AgentProfileStore(Path(raw_root), cipher=XorCipher())
            client = Client()
            with patch("wechat_ai.agent_profile.socket.gethostname", return_value="QA-PC"):
                saved = activate_and_save(
                    activation_code="one-time-code",
                    center_url="https://center.example.test",
                    store=store,
                    client=client,  # type: ignore[arg-type]
                    agent_version="1.2.3",
                )

            self.assertEqual(saved.agent_token, "token-1")
            self.assertEqual(client.payload["activation_code"], "one-time-code")
            self.assertNotIn("one-time-code", store.paths.profile.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
