from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechat_ai.agent_updater import (
    AGENT_EXECUTABLE,
    AgentUpdater,
    ReleaseManifest,
    UpdateError,
    UpdateNotNewerError,
    UpdateSecurityError,
    UpdateState,
    UpdateStateStore,
    canonical_manifest_payload,
    download_release,
    extract_release_archive,
    fetch_release_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def load_build_config_module() -> object:
    path = ROOT / "packaging" / "build_config.py"
    name = "agent_build_config_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BuildConfigTests(unittest.TestCase):
    def test_falls_back_to_onedir_internal_config(self) -> None:
        module = load_build_config_module()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            internal = root / "_internal"
            internal.mkdir()
            (internal / "agent-build.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.3",
                        "center_url": "https://center.example.test",
                        "update_manifest_url": "https://updates.example.test/stable.json",
                        "update_public_key": base64.b64encode(bytes(32)).decode("ascii"),
                    }
                ),
                encoding="utf-8-sig",
            )
            with patch.object(module.sys, "_MEIPASS", str(root / "missing"), create=True):
                with patch.object(module.sys, "executable", str(root / "WeChatAIAgent.exe")):
                    config = module.load_package_config()

        self.assertEqual(config.version, "1.2.3")

    def test_does_not_fallback_after_malformed_primary_config(self) -> None:
        module = load_build_config_module()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            primary = root / "primary"
            internal = root / "_internal"
            primary.mkdir()
            internal.mkdir()
            (primary / "agent-build.json").write_text("not-json", encoding="utf-8")
            (internal / "agent-build.json").write_text("{}", encoding="utf-8")
            with patch.object(module.sys, "_MEIPASS", str(primary), create=True):
                with patch.object(module.sys, "executable", str(root / "WeChatAIAgent.exe")):
                    with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                        module.load_package_config()


class FakeResponse:
    def __init__(self, content: bytes, url: str = "https://updates.example.test/file") -> None:
        self.stream = io.BytesIO(content)
        self.url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self.stream.read(amount)

    def geturl(self) -> str:
        return self.url


class SignedRelease:
    def __init__(self, version: str = "2.0.0", content: bytes | None = None) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = base64.b64encode(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")
        self.content = content if content is not None else self._archive_bytes()
        self.payload: dict[str, object] = {
            "schema_version": 1,
            "version": version,
            "package_url": f"https://updates.example.test/{version}/agent.zip",
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "size": len(self.content),
        }

    @staticmethod
    def _archive_bytes(name: str = AGENT_EXECUTABLE, content: bytes = b"agent") -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, content)
        return output.getvalue()

    def raw_manifest(self, **changes: object) -> bytes:
        payload = {**self.payload, **changes}
        signature = self.private_key.sign(canonical_manifest_payload(payload))
        payload["signature"] = base64.b64encode(signature).decode("ascii")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def manifest(self, current_version: str = "1.0.0") -> ReleaseManifest:
        return ReleaseManifest.verify(
            self.raw_manifest(),
            self.public_key,
            current_version=current_version,
        )


class ManifestVerificationTests(unittest.TestCase):
    def test_valid_ed25519_signature_and_sha256_are_accepted(self) -> None:
        release = SignedRelease()
        manifest = release.manifest()

        self.assertEqual(manifest.version, "2.0.0")
        self.assertEqual(manifest.sha256, hashlib.sha256(release.content).hexdigest())
        self.assertTrue(manifest.package_url.startswith("https://"))

    def test_missing_or_tampered_signature_is_rejected(self) -> None:
        release = SignedRelease()
        signed = json.loads(release.raw_manifest())
        signed["version"] = "3.0.0"
        with self.assertRaisesRegex(UpdateSecurityError, "signature is invalid"):
            ReleaseManifest.verify(
                json.dumps(signed).encode("utf-8"),
                release.public_key,
                current_version="1.0.0",
            )

        del signed["signature"]
        with self.assertRaisesRegex(UpdateSecurityError, "schema"):
            ReleaseManifest.verify(
                json.dumps(signed).encode("utf-8"),
                release.public_key,
                current_version="1.0.0",
            )

    def test_equal_or_lower_version_is_rejected(self) -> None:
        for candidate in ("1.2.3", "1.2.2", "0.9.9"):
            release = SignedRelease(candidate)
            with self.subTest(candidate=candidate):
                with self.assertRaises(UpdateNotNewerError):
                    ReleaseManifest.verify(
                        release.raw_manifest(),
                        release.public_key,
                        current_version="1.2.3",
                    )

    def test_manifest_and_package_urls_must_use_https(self) -> None:
        release = SignedRelease()
        called = False

        def opener(*args: object, **kwargs: object) -> FakeResponse:
            nonlocal called
            called = True
            return FakeResponse(release.raw_manifest())

        with self.assertRaisesRegex(UpdateSecurityError, "https"):
            fetch_release_manifest(
                "http://updates.example.test/manifest.json",
                release.public_key,
                current_version="1.0.0",
                opener=opener,
            )
        self.assertFalse(called)

        with self.assertRaisesRegex(UpdateSecurityError, "package_url"):
            ReleaseManifest.verify(
                release.raw_manifest(package_url="http://updates.example.test/agent.zip"),
                release.public_key,
                current_version="1.0.0",
            )

        with self.assertRaisesRegex(UpdateSecurityError, "response URL"):
            fetch_release_manifest(
                "https://updates.example.test/manifest.json",
                release.public_key,
                current_version="1.0.0",
                opener=lambda *args, **kwargs: FakeResponse(
                    release.raw_manifest(),
                    "http://downgrade.example.test/manifest.json",
                ),
            )

    def test_download_rechecks_signed_size_and_hash(self) -> None:
        release = SignedRelease()
        manifest = release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            downloaded = download_release(
                manifest,
                root,
                opener=lambda *args, **kwargs: FakeResponse(release.content),
            )
            self.assertEqual(downloaded.read_bytes(), release.content)

            downloaded.unlink()
            with self.assertRaisesRegex(UpdateSecurityError, "SHA-256"):
                download_release(
                    manifest,
                    root,
                    opener=lambda *args, **kwargs: FakeResponse(
                        b"X" + release.content[1:]
                    ),
                )
            self.assertFalse(list(root.glob("*.download")))


class ArchiveAndStateTests(unittest.TestCase):
    def test_archive_path_traversal_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape.exe", b"bad")
                archive.writestr(AGENT_EXECUTABLE, b"agent")
            with self.assertRaisesRegex(UpdateSecurityError, "path traversal"):
                extract_release_archive(traversal, root / "traversal-output")
            self.assertFalse((root / "escape.exe").exists())

            symlink = root / "symlink.zip"
            with zipfile.ZipFile(symlink, "w") as archive:
                item = zipfile.ZipInfo("link")
                item.external_attr = (0o120777 << 16)
                archive.writestr(item, "target")
                archive.writestr(AGENT_EXECUTABLE, b"agent")
            with self.assertRaisesRegex(UpdateSecurityError, "symbolic links"):
                extract_release_archive(symlink, root / "symlink-output")

    def _prepared_updater(self, root: Path) -> AgentUpdater:
        current = root / "versions" / "1.0.0"
        current.mkdir(parents=True)
        (current / AGENT_EXECUTABLE).write_bytes(b"old")
        updater = AgentUpdater(root, "1.0.0")
        updater.state_store.initialize("1.0.0")
        return updater

    def test_atomic_switch_health_confirmation_and_previous_release_retention(self) -> None:
        release = SignedRelease("2.0.0")
        manifest = release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            updater = self._prepared_updater(root)
            archive = root / "release.zip"
            archive.write_bytes(release.content)

            pending = updater.apply(manifest, archive)
            self.assertEqual(pending.active_version, "2.0.0")
            self.assertEqual(pending.previous_version, "1.0.0")
            self.assertEqual(pending.status, "pending_health")
            self.assertEqual(
                updater.state_store.active_executable(),
                root / "versions" / "2.0.0" / AGENT_EXECUTABLE,
            )

            healthy = AgentUpdater(root, "2.0.0").confirm_health("2.0.0")
            self.assertEqual(healthy.status, "healthy")
            self.assertTrue((root / "versions" / "1.0.0" / AGENT_EXECUTABLE).is_file())

    def test_failed_atomic_directory_switch_keeps_old_state(self) -> None:
        release = SignedRelease("2.0.0")
        manifest = release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            updater = self._prepared_updater(root)
            archive = root / "release.zip"
            archive.write_bytes(release.content)
            real_replace = os.replace

            def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
                if Path(target) == root / "versions" / "2.0.0":
                    raise OSError("simulated atomic switch failure")
                real_replace(source, target)

            with patch("wechat_ai.agent_updater.os.replace", side_effect=replace):
                with self.assertRaisesRegex(OSError, "atomic switch"):
                    updater.apply(manifest, archive)

            self.assertEqual(updater.state_store.load().active_version, "1.0.0")
            self.assertFalse((root / "versions" / "2.0.0").exists())
            self.assertFalse(list((root / "versions").glob("*.staging")))

    def test_health_timeout_rolls_back_to_retained_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0"):
                release = root / "versions" / version
                release.mkdir(parents=True)
                (release / AGENT_EXECUTABLE).write_bytes(version.encode("ascii"))
            store = UpdateStateStore(root)
            pending_since = datetime.now(timezone.utc) - timedelta(minutes=5)
            store.save(
                UpdateState(
                    active_version="2.0.0",
                    previous_version="1.0.0",
                    pending_version="2.0.0",
                    status="pending_health",
                    pending_since=pending_since.isoformat(),
                )
            )

            rolled_back = AgentUpdater(root, "2.0.0").rollback_if_health_timed_out(
                timeout_seconds=90,
                now=datetime.now(timezone.utc),
            )

            assert rolled_back is not None
            self.assertEqual(rolled_back.active_version, "1.0.0")
            self.assertEqual(rolled_back.failed_version, "2.0.0")
            self.assertEqual(rolled_back.status, "rolled_back")

    def test_attempt_timeline_is_persisted_and_cleanup_waits_for_confirmation(self) -> None:
        release = SignedRelease("2.0.0")
        manifest = release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("0.9.0", "1.0.0"):
                release_dir = root / "versions" / version
                release_dir.mkdir(parents=True)
                (release_dir / AGENT_EXECUTABLE).write_bytes(version.encode("ascii"))
            updater = AgentUpdater(root, "1.0.0")
            updater.state_store.initialize("1.0.0")
            archive = root / "release.zip"
            archive.write_bytes(release.content)

            checking = updater.begin_check()
            self.assertEqual(checking.status, "checking")
            self.assertTrue(checking.checked_at)
            downloading = updater.begin_download("2.0.0")
            self.assertEqual(downloading.target_version, "2.0.0")
            downloaded = updater.mark_downloaded("2.0.0")
            self.assertEqual(downloaded.status, "downloaded")
            self.assertTrue(downloaded.downloaded_at)

            pending = updater.apply(manifest, archive)
            self.assertEqual(pending.status, "pending_health")
            self.assertEqual(pending.target_version, "2.0.0")
            self.assertEqual(pending.rollback_version, "1.0.0")
            self.assertTrue(pending.staged_at)
            self.assertTrue(pending.switched_at)
            self.assertTrue((root / "versions" / "0.9.0").exists())

            healthy = AgentUpdater(root, "2.0.0").confirm_health("2.0.0")
            self.assertEqual(healthy.status, "healthy")
            self.assertEqual(healthy.previous_version, "1.0.0")
            self.assertTrue(healthy.confirmed_at)
            self.assertTrue(healthy.cleanup_at)
            self.assertFalse((root / "versions" / "0.9.0").exists())
            self.assertTrue((root / "versions" / "1.0.0").exists())
            self.assertTrue((root / "versions" / "2.0.0").exists())
            self.assertEqual(
                AgentUpdater(root, "2.0.0").cleanup_old_versions(),
                (),
            )
            payload = json.loads(
                (root / "state" / "update-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["schema_version"], 2)

    def test_failure_state_redacts_secrets_urls_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            updater = self._prepared_updater(root)

            failed = updater.record_failure(
                "download_failed",
                (
                    "token=very-secret-value "
                    "https://updates.example.test/file?api_key=also-secret "
                    r"C:\Users\Customer\private\agent.zip"
                ),
                target_version="2.0.0",
            )

            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.target_version, "2.0.0")
            self.assertNotIn("very-secret-value", failed.failure_summary)
            self.assertNotIn("also-secret", failed.failure_summary)
            self.assertNotIn("Customer", failed.failure_summary)
            self.assertIn("<redacted>", failed.failure_summary)
            self.assertIn("<url>", failed.failure_summary)
            self.assertRegex(failed.failure_digest, r"^[0-9a-f]{64}$")

    def test_rollback_retains_the_previous_confirmed_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0", "3.0.0"):
                release_dir = root / "versions" / version
                release_dir.mkdir(parents=True)
                (release_dir / AGENT_EXECUTABLE).write_bytes(b"exe")
            now = datetime.now(timezone.utc).isoformat()
            store = UpdateStateStore(root)
            store.save(
                UpdateState(
                    active_version="3.0.0",
                    previous_version="2.0.0",
                    pending_version="3.0.0",
                    rollback_version="2.0.0",
                    target_version="3.0.0",
                    status="pending_health",
                    pending_since=now,
                    switched_at=now,
                    confirmed_at=now,
                )
            )

            rolled_back = AgentUpdater(root, "3.0.0").rollback(
                "candidate failed its startup health check"
            )
            self.assertEqual(rolled_back.active_version, "2.0.0")
            self.assertEqual(rolled_back.previous_version, "1.0.0")
            updater = AgentUpdater(root, "2.0.0")
            updater.begin_check()
            updater.finish_check_without_update()
            self.assertTrue((root / "versions" / "1.0.0").exists())
            self.assertTrue((root / "versions" / "2.0.0").exists())

    def test_interrupted_and_corrupt_states_recover_to_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0"):
                release_dir = root / "versions" / version
                release_dir.mkdir(parents=True)
                (release_dir / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            store.save(
                UpdateState(
                    active_version="1.0.0",
                    pending_version="2.0.0",
                    rollback_version="1.0.0",
                    target_version="2.0.0",
                    status="switching",
                    attempt_id="a" * 32,
                    staged_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            staging = root / "versions" / f".2.0.0-{'c' * 32}.staging"
            staging.mkdir()
            (staging / "partial.bin").write_bytes(b"partial")

            recovered = store.initialize("1.0.0")
            self.assertEqual(recovered.status, "failed")
            self.assertEqual(recovered.active_version, "1.0.0")
            self.assertEqual(recovered.target_version, "2.0.0")
            self.assertEqual(recovered.failure_code, "interrupted_switching")
            self.assertEqual(store.initialize("1.0.0"), recovered)
            self.assertEqual(
                store.active_executable(),
                root / "versions" / "1.0.0" / AGENT_EXECUTABLE,
            )
            self.assertFalse(staging.exists())

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            release_dir = root / "versions" / "1.0.0"
            release_dir.mkdir(parents=True)
            (release_dir / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            store.save(UpdateState(active_version="1.0.0"))
            store.save(
                UpdateState(
                    active_version="1.0.0",
                    status="checking",
                    attempt_id="b" * 32,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            store.path.write_text("{broken", encoding="utf-8")

            recovered = store.load()
            self.assertEqual(recovered.status, "failed")
            self.assertEqual(recovered.active_version, "1.0.0")
            self.assertEqual(recovered.failure_code, "state_file_recovered")
            self.assertEqual(store.load(), recovered)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            release_dir = root / "versions" / "4.0.0"
            release_dir.mkdir(parents=True)
            (release_dir / AGENT_EXECUTABLE).write_bytes(b"exe")

            reconstructed = UpdateStateStore(root).recover_interrupted()
            self.assertEqual(reconstructed.status, "failed")
            self.assertEqual(reconstructed.active_version, "4.0.0")
            self.assertEqual(reconstructed.failure_code, "state_reconstructed")

    def test_cleanup_never_removes_pending_or_reparse_releases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0", "3.0.0"):
                release_dir = root / "versions" / version
                release_dir.mkdir(parents=True)
                (release_dir / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            now = datetime.now(timezone.utc).isoformat()
            store.save(
                UpdateState(
                    active_version="3.0.0",
                    previous_version="2.0.0",
                    pending_version="3.0.0",
                    rollback_version="2.0.0",
                    target_version="3.0.0",
                    status="pending_health",
                    pending_since=now,
                    switched_at=now,
                )
            )
            updater = AgentUpdater(root, "3.0.0")

            with self.assertRaisesRegex(UpdateError, "only be cleaned"):
                updater.cleanup_old_versions()
            for version in ("1.0.0", "2.0.0", "3.0.0"):
                self.assertTrue((root / "versions" / version).exists())

            store.save(
                UpdateState(
                    active_version="3.0.0",
                    previous_version="2.0.0",
                    target_version="3.0.0",
                    status="healthy",
                    confirmed_at=now,
                )
            )
            real_link_check = __import__(
                "wechat_ai.agent_updater",
                fromlist=["_is_link_or_reparse"],
            )._is_link_or_reparse

            def link_check(path: Path) -> bool:
                if path.name == "1.0.0":
                    return True
                return real_link_check(path)

            with patch(
                "wechat_ai.agent_updater._is_link_or_reparse",
                side_effect=link_check,
            ):
                with self.assertRaisesRegex(UpdateSecurityError, "symbolic link"):
                    updater.cleanup_old_versions()
            for version in ("1.0.0", "2.0.0", "3.0.0"):
                self.assertTrue((root / "versions" / version).exists())

            self.assertEqual(updater.cleanup_old_versions(), ("1.0.0",))
            self.assertTrue((root / "versions" / "2.0.0").exists())
            self.assertTrue((root / "versions" / "3.0.0").exists())


class AgentEntryUpdateFlowTests(unittest.TestCase):
    @staticmethod
    def _load_agent_entry_module() -> object:
        path = ROOT / "packaging" / "agent_entry.py"
        name = "agent_entry_under_test"
        packaging_dir = str(path.parent)
        sys.path.insert(0, packaging_dir)
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(packaging_dir)

    def test_agent_entry_persists_download_and_staging_transitions(self) -> None:
        agent_entry = self._load_agent_entry_module()
        signed_release = SignedRelease("2.0.0")
        manifest = signed_release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            current = root / "versions" / "1.0.0"
            current.mkdir(parents=True)
            (current / AGENT_EXECUTABLE).write_bytes(b"old")
            config = SimpleNamespace(
                version="1.0.0",
                update_manifest_url="https://updates.example.test/stable.json",
                update_public_key=signed_release.public_key,
            )

            def download(*args: object, **kwargs: object) -> Path:
                archive = root / "downloaded.zip"
                archive.write_bytes(signed_release.content)
                return archive

            with patch.object(agent_entry, "load_package_config", return_value=config):
                with patch.object(
                    agent_entry,
                    "fetch_release_manifest",
                    return_value=manifest,
                ):
                    with patch.object(
                        agent_entry,
                        "download_release",
                        side_effect=download,
                    ):
                        self.assertTrue(agent_entry._check_for_update(root))

            state = UpdateStateStore(root).load()
            self.assertEqual(state.status, "pending_health")
            self.assertEqual(state.target_version, "2.0.0")
            self.assertTrue(state.checked_at)
            self.assertTrue(state.downloaded_at)
            self.assertTrue(state.staged_at)
            self.assertTrue(state.switched_at)
            self.assertFalse((root / "downloaded.zip").exists())

    def test_agent_entry_persists_safe_download_failure(self) -> None:
        agent_entry = self._load_agent_entry_module()
        signed_release = SignedRelease("2.0.0")
        manifest = signed_release.manifest()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            current = root / "versions" / "1.0.0"
            current.mkdir(parents=True)
            (current / AGENT_EXECUTABLE).write_bytes(b"old")
            config = SimpleNamespace(
                version="1.0.0",
                update_manifest_url="https://updates.example.test/stable.json",
                update_public_key=signed_release.public_key,
            )
            error = UpdateError(
                "token=secret https://updates.example.test/private "
                r"C:\Users\Customer\download.zip"
            )

            with patch.object(agent_entry, "load_package_config", return_value=config):
                with patch.object(
                    agent_entry,
                    "fetch_release_manifest",
                    return_value=manifest,
                ):
                    with patch.object(
                        agent_entry,
                        "download_release",
                        side_effect=error,
                    ):
                        with self.assertRaises(UpdateError):
                            agent_entry._check_for_update(root)

            state = UpdateStateStore(root).load()
            self.assertEqual(state.status, "failed")
            self.assertEqual(state.failure_code, "download_failed")
            self.assertEqual(state.target_version, "2.0.0")
            self.assertNotIn("secret", state.failure_summary)
            self.assertNotIn("Customer", state.failure_summary)


class LauncherTransitionTests(unittest.TestCase):
    @staticmethod
    def _load_launcher_module() -> object:
        path = ROOT / "packaging" / "launcher_entry.py"
        spec = importlib.util.spec_from_file_location("agent_launcher_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_launcher_confirms_pending_release_after_fresh_health(self) -> None:
        launcher = self._load_launcher_module()

        class Process:
            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return 0

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0"):
                release = root / "versions" / version
                release.mkdir(parents=True)
                (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            UpdateStateStore(root).save(
                UpdateState(
                    active_version="2.0.0",
                    previous_version="1.0.0",
                    pending_version="2.0.0",
                    status="pending_health",
                    pending_since=datetime.now(timezone.utc).isoformat(),
                )
            )
            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(launcher.subprocess, "Popen", return_value=Process()):
                    with patch.object(launcher, "_fresh_running_health", return_value=True):
                        self.assertEqual(launcher.run(profile), 0)

            state = UpdateStateStore(root).load()
            self.assertEqual(state.status, "healthy")
            self.assertEqual(state.active_version, "2.0.0")

    def test_launcher_health_timeout_rolls_back_and_starts_previous_release(self) -> None:
        launcher = self._load_launcher_module()
        commands: list[list[str]] = []

        class Process:
            def __init__(self, command: list[str]) -> None:
                self.command = command
                self.terminated = False

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.terminated = True

        def popen(command: list[str], **kwargs: object) -> Process:
            commands.append(command)
            return Process(command)

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0"):
                release = root / "versions" / version
                release.mkdir(parents=True)
                (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            UpdateStateStore(root).save(
                UpdateState(
                    active_version="2.0.0",
                    previous_version="1.0.0",
                    pending_version="2.0.0",
                    status="pending_health",
                    pending_since=datetime.now(timezone.utc).isoformat(),
                )
            )
            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(launcher.subprocess, "Popen", side_effect=popen):
                    with patch.object(launcher, "HEALTH_CONFIRM_TIMEOUT_SECONDS", 0):
                        self.assertEqual(launcher.run(profile), 0)

            state = UpdateStateStore(root).load()
            self.assertEqual(state.status, "rolled_back")
            self.assertEqual(state.active_version, "1.0.0")
            self.assertEqual(len(commands), 2)
            self.assertIn(str(root / "versions" / "2.0.0" / AGENT_EXECUTABLE), commands[0])
            self.assertIn(str(root / "versions" / "1.0.0" / AGENT_EXECUTABLE), commands[1])

    def test_launcher_start_failure_rolls_back_and_persists_reason(self) -> None:
        launcher = self._load_launcher_module()
        commands: list[list[str]] = []

        class Process:
            def wait(self, timeout: float | None = None) -> int:
                return 0

        def popen(command: list[str], **kwargs: object) -> Process:
            commands.append(command)
            if len(commands) == 1:
                raise OSError("contains customer path and should not be persisted")
            return Process()

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0"):
                release = root / "versions" / version
                release.mkdir(parents=True)
                (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            now = datetime.now(timezone.utc).isoformat()
            UpdateStateStore(root).save(
                UpdateState(
                    active_version="2.0.0",
                    previous_version="1.0.0",
                    pending_version="2.0.0",
                    rollback_version="1.0.0",
                    target_version="2.0.0",
                    status="pending_health",
                    pending_since=now,
                    switched_at=now,
                )
            )
            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(launcher.subprocess, "Popen", side_effect=popen):
                    self.assertEqual(launcher.run(profile), 0)

            state = UpdateStateStore(root).load()
            self.assertEqual(state.status, "rolled_back")
            self.assertEqual(state.active_version, "1.0.0")
            self.assertEqual(state.failed_version, "2.0.0")
            self.assertEqual(state.failure_code, "candidate_health_failed")
            self.assertIn("failed to start", state.failure_summary)
            self.assertRegex(state.failure_digest, r"^[0-9a-f]{64}$")
            self.assertEqual(len(commands), 2)
            self.assertIn("2.0.0", commands[0][0])
            self.assertIn("1.0.0", commands[1][0])

    def test_launcher_distinguishes_invalid_update_restart_from_normal_exit(self) -> None:
        launcher = self._load_launcher_module()

        class Process:
            def __init__(self, return_code: int) -> None:
                self.return_code = return_code

            def wait(self, timeout: float | None = None) -> int:
                return self.return_code

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            release = root / "versions" / "1.0.0"
            release.mkdir(parents=True)
            (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            store.save(UpdateState(active_version="1.0.0"))
            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(
                    launcher.subprocess,
                    "Popen",
                    return_value=Process(launcher.RESTART_FOR_UPDATE_EXIT_CODE),
                ):
                    self.assertEqual(
                        launcher.run(profile),
                        launcher.UPDATE_FAILURE_EXIT_CODE,
                    )

            failed = store.load()
            self.assertEqual(failed.status, "failed")
            self.assertEqual(
                failed.failure_code,
                "restart_without_pending_update",
            )

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            release = root / "versions" / "1.0.0"
            release.mkdir(parents=True)
            (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            store.save(UpdateState(active_version="1.0.0"))
            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(
                    launcher.subprocess,
                    "Popen",
                    return_value=Process(7),
                ):
                    self.assertEqual(launcher.run(profile), 7)
            self.assertEqual(store.load().status, "healthy")

    def test_launcher_continues_when_confirmed_candidate_stages_next_update(self) -> None:
        launcher = self._load_launcher_module()
        commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            for version in ("1.0.0", "2.0.0", "3.0.0"):
                release = root / "versions" / version
                release.mkdir(parents=True)
                (release / AGENT_EXECUTABLE).write_bytes(b"exe")
            store = UpdateStateStore(root)
            store.save(
                UpdateState(
                    active_version="2.0.0",
                    previous_version="1.0.0",
                    pending_version="2.0.0",
                    status="pending_health",
                    pending_since=datetime.now(timezone.utc).isoformat(),
                )
            )

            class Process:
                def __init__(self, command: list[str]) -> None:
                    self.command = command

                def poll(self) -> None:
                    return None

                def wait(self, timeout: float | None = None) -> int:
                    if "2.0.0" in self.command[0]:
                        store.save(
                            UpdateState(
                                active_version="3.0.0",
                                previous_version="2.0.0",
                                pending_version="3.0.0",
                                status="pending_health",
                                pending_since=datetime.now(timezone.utc).isoformat(),
                            )
                        )
                        return 75
                    return 0

            def popen(command: list[str], **kwargs: object) -> Process:
                commands.append(command)
                return Process(command)

            profile = root / "profile"
            profile.mkdir()
            with patch.object(launcher.sys, "executable", str(root / "launcher.exe")):
                with patch.object(launcher.subprocess, "Popen", side_effect=popen):
                    with patch.object(launcher, "_fresh_running_health", return_value=True):
                        self.assertEqual(launcher.run(profile), 0)

            state = store.load()
            self.assertEqual(state.status, "healthy")
            self.assertEqual(state.active_version, "3.0.0")
            self.assertEqual(len(commands), 2)
            self.assertIn("2.0.0", commands[0][0])
            self.assertIn("3.0.0", commands[1][0])


if __name__ == "__main__":
    unittest.main()
