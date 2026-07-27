from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import main
from wechat_ai.agent_profile import AgentProfile, AgentProfileStore


class XorCipher:
    def encrypt(self, plaintext: bytes) -> bytes:
        return bytes(value ^ 0x5A for value in plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return bytes(value ^ 0x5A for value in ciphertext)


class FakeConfig(SimpleNamespace):
    def with_overrides(self, **overrides: object) -> "FakeConfig":
        values = dict(vars(self))
        values.update(overrides)
        return FakeConfig(**values)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "agent_center_url": self.agent_center_url,
            "agent_token_set": bool(self.agent_token),
            "agent_spool_file": self.agent_spool_file,
            "health_file": self.health_file,
        }


def config(**overrides: object) -> FakeConfig:
    values: dict[str, object] = {
        "account_id": "",
        "agent_center_url": "",
        "agent_tenant_id": "",
        "agent_instance_id": "",
        "agent_channel_account_id": "",
        "agent_token": "",
        "agent_spool_file": "data/agent-spool.sqlite3",
        "state_db_file": "data/state.sqlite3",
        "state_db_backup_retention": 5,
        "log_file": "logs/agent.log",
        "audit_log_file": "logs/audit.jsonl",
        "alert_file": "logs/alerts.log",
        "health_file": "logs/health.json",
        "pending_replies_file": "logs/pending.jsonl",
        "reports_dir": "reports",
    }
    values.update(overrides)
    return FakeConfig(**values)


def save_profile(store: AgentProfileStore) -> AgentProfile:
    device_id = store.get_or_create_device_id()
    value = AgentProfile(
        center_url="https://center.example.test",
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        channel_account_id="channel-1",
        agent_token="agent-token-secret",
        device_id=device_id,
        device_name="QA-PC",
        agent_version="1.2.3",
        activated_at="2026-07-22T10:00:00+00:00",
    )
    store.save(value)
    return value


class MainAgentActivationTests(unittest.TestCase):
    def make_store(self, root: Path) -> AgentProfileStore:
        store = AgentProfileStore(root, cipher=XorCipher())
        save_profile(store)
        return store

    def test_activate_cli_requires_code(self) -> None:
        parser = main.build_parser()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["agent", "activate", "--center-url", "https://center.test"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--code", stderr.getvalue())

    def test_center_doctor_receives_configured_previous_peppers(self) -> None:
        center_config = config(
            center_database_url="sqlite+pysqlite:///:memory:",
            center_api_key_pepper="n" * 32,
            center_api_key_previous_peppers=["o" * 32],
            center_secret_key="secret-key",
            center_provisioning_token="p" * 32,
        )
        args = Namespace(center_command="doctor")
        with patch("main.load_config", return_value=center_config):
            with patch(
                "main.center_diagnostics",
                return_value={"ok": True},
            ) as diagnostics:
                with redirect_stdout(io.StringIO()):
                    result = main.run_center_command("center.env", args)

        self.assertEqual(result, 0)
        self.assertEqual(
            diagnostics.call_args.kwargs["previous_api_key_peppers"],
            ["o" * 32],
        )

    def test_center_server_receives_configured_previous_peppers(self) -> None:
        center_config = config(
            center_database_url="sqlite+pysqlite:///:memory:",
            center_api_key_pepper="n" * 32,
            center_api_key_previous_peppers=["o" * 32],
            center_secret_key="secret-key",
            center_provisioning_token="p" * 32,
            center_host="127.0.0.1",
            center_port=8790,
        )
        args = Namespace(center_command="serve", production=False, threads=4)
        application = SimpleNamespace(readiness_probe=None)
        worker = SimpleNamespace(run_forever=lambda: None)
        with patch("main.load_config", return_value=center_config):
            with patch(
                "main.build_center_application",
                return_value=(object(), application),
            ) as build:
                with patch("main.CenterReplyWorker", return_value=worker):
                    with patch("main.SecretCipher", return_value=object()):
                        with patch("main.run_center_server") as serve:
                            result = main.run_center_command("center.env", args)

        self.assertEqual(result, 0)
        self.assertEqual(
            build.call_args.kwargs["previous_api_key_peppers"],
            ["o" * 32],
        )
        serve.assert_called_once()

    def test_secure_profile_is_injected_when_agent_env_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            with patch("main.load_config", return_value=config()):
                loaded = main.load_agent_runtime_config(
                    str(Path(raw_root) / "missing.env"),
                    require_binding=True,
                    store=store,
                )

            self.assertEqual(loaded.agent_center_url, "https://center.example.test")
            self.assertEqual(loaded.agent_token, "agent-token-secret")
            self.assertEqual(loaded.agent_spool_file, str(store.paths.spool))
            self.assertEqual(loaded.log_file, str(store.paths.log))
            self.assertEqual(loaded.health_file, str(store.paths.health))

    def test_complete_legacy_agent_env_keeps_precedence(self) -> None:
        legacy = config(
            agent_center_url="https://legacy.example.test",
            agent_tenant_id="legacy-tenant",
            agent_instance_id="legacy-agent",
            agent_channel_account_id="legacy-channel",
            agent_token="legacy-token",
        )
        with patch("main.load_config", return_value=legacy):
            loaded = main.load_agent_runtime_config("legacy.env", require_binding=True)
        self.assertIs(loaded, legacy)

    def test_connector_doctor_uses_secure_profile_without_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            args = Namespace(
                connector_command="doctor",
                id="desktop-1",
                channel="wechat_desktop",
            )
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=config()):
                    with patch(
                        "main.connector_compatibility",
                        return_value={"safe_to_run": True},
                    ) as compatibility:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            result = main.run_connector_command("missing.env", args)

            self.assertEqual(result, 0)
            passed_config = compatibility.call_args.args[0]
            self.assertEqual(passed_config.agent_tenant_id, "tenant-1")
            self.assertNotIn("agent-token-secret", output.getvalue())

    def test_connector_run_center_uses_secure_profile_without_env(self) -> None:
        class Lock:
            def __enter__(self) -> "Lock":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            args = Namespace(
                connector_command="run-center",
                id="desktop-1",
                channel="wechat_desktop",
                interval=2.0,
            )
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=config()):
                    with patch("main.SingleInstanceLock.from_config", return_value=Lock()):
                        with patch("main.run_center_connector_agent") as run_agent:
                            result = main.run_connector_command("missing.env", args)

            self.assertEqual(result, 0)
            passed_config = run_agent.call_args.args[0]
            self.assertEqual(passed_config.agent_instance_id, "agent-1")
            self.assertEqual(passed_config.agent_token, "agent-token-secret")

    def test_agent_run_uses_profile_and_center_connector(self) -> None:
        class Lock:
            def __enter__(self) -> "Lock":
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            args = Namespace(
                agent_command="run",
                id="desktop-1",
                channel="wechat_desktop",
                interval=2.0,
            )
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=config()):
                    with patch("main.SingleInstanceLock.from_config", return_value=Lock()):
                        with patch("main.run_center_connector_agent") as run_agent:
                            result = main.run_agent_command("missing.env", args)

            self.assertEqual(result, 0)
            passed_config = run_agent.call_args.args[0]
            self.assertEqual(passed_config.agent_token, "agent-token-secret")
            self.assertEqual(run_agent.call_args.kwargs["interval_seconds"], 2.0)

    def test_install_without_env_uses_secure_marker_and_center_mode(self) -> None:
        installed: list[object] = []

        class Scheduler:
            def install(self, task: object) -> None:
                installed.append(task)

        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            args = Namespace(agent_command="install", task_name="QA Agent", mode=None)
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=config()):
                    with patch("main.WindowsTaskScheduler", return_value=Scheduler()):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            result = main.run_agent_command(str(Path(raw_root) / "missing.env"), args)

            self.assertEqual(result, 0)
            self.assertEqual(len(installed), 1)
            task = installed[0]
            self.assertEqual(task.mode, "center")
            self.assertEqual(task.env_path, store.paths.runtime_marker)
            self.assertTrue(task.env_path.is_file())

    def test_install_rejects_temporary_agent_environment_without_profile_or_env_file(self) -> None:
        legacy = config(
            agent_center_url="https://legacy.example.test",
            agent_tenant_id="legacy-tenant",
            agent_instance_id="legacy-agent",
            agent_channel_account_id="legacy-channel",
            agent_token="legacy-token",
        )
        with tempfile.TemporaryDirectory() as raw_root:
            store = AgentProfileStore(Path(raw_root), cipher=XorCipher())
            args = Namespace(agent_command="install", task_name="QA Agent", mode="center")
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=legacy):
                    with patch("main.WindowsTaskScheduler") as scheduler:
                        with self.assertRaisesRegex(RuntimeError, "run `agent activate`"):
                            main.run_agent_command(str(Path(raw_root) / "missing.env"), args)
            scheduler.return_value.install.assert_not_called()

    def test_show_config_and_activate_output_do_not_leak_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            output = io.StringIO()
            with patch("main.AgentProfileStore", return_value=store):
                with redirect_stdout(output):
                    self.assertEqual(
                        main.run_agent_command("missing.env", Namespace(agent_command="show-config")),
                        0,
                    )
            rendered = output.getvalue()
            self.assertNotIn("agent-token-secret", rendered)
            self.assertIn('"agent_token": "protected"', rendered)

            output = io.StringIO()
            args = Namespace(
                agent_command="activate",
                code="one-time-secret-code",
                center_url="https://center.example.test",
            )
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.activate_and_save") as activate:
                    with redirect_stdout(output):
                        self.assertEqual(main.run_agent_command("missing.env", args), 0)
            self.assertEqual(activate.call_args.kwargs["activation_code"], "one-time-secret-code")
            self.assertNotIn("one-time-secret-code", output.getvalue())
            self.assertNotIn("agent-token-secret", output.getvalue())

    def test_reset_requires_confirmation_and_yes_supports_unattended_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            with patch("main.AgentProfileStore", return_value=store):
                with patch("builtins.input", return_value="NO"):
                    self.assertEqual(
                        main.run_agent_command(
                            "missing.env",
                            Namespace(agent_command="reset", yes=False),
                        ),
                        0,
                    )
            self.assertTrue(store.is_activated())

            with patch("main.AgentProfileStore", return_value=store):
                self.assertEqual(
                    main.run_agent_command(
                        "missing.env",
                        Namespace(agent_command="reset", yes=True),
                    ),
                    0,
                )
            self.assertFalse(store.is_activated())

    def test_runtime_marker_config_command_reports_profile_paths_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store = self.make_store(Path(raw_root))
            output = io.StringIO()
            with patch("main.AgentProfileStore", return_value=store):
                with patch("main.load_config", return_value=config()):
                    with redirect_stdout(output):
                        result = main.main(["--env", str(store.paths.runtime_marker), "config"])

            self.assertEqual(result, 0)
            rendered = json.loads(output.getvalue())
            self.assertTrue(rendered["agent_token_set"])
            self.assertEqual(rendered["agent_spool_file"], str(store.paths.spool))
            self.assertEqual(rendered["health_file"], str(store.paths.health))
            self.assertNotIn("agent-token-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
