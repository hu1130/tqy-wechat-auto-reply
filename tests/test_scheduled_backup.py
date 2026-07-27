from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scheduled_backup.py"


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="scheduled-backup-test-")
        self.base = Path(self.temp_dir.name)
        self.center = self.base / "center"
        self.ai_cs = self.base / "ai-cs"
        self.backups = self.base / "backups"
        self.health = self.base / "state" / "backup-health.json"
        self.invocation = self.base / "invocation.json"
        for path in (
            self.center / "deploy",
            self.ai_cs,
            self.backups,
            self.health.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.fake_backup = self.center / "deploy" / "coordinated_backup.sh"
        self.fake_backup.write_text(
            """from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

Path(os.environ["FAKE_INVOCATION"]).write_text(
    json.dumps(
        {
            "args": sys.argv[1:],
            "cwd": os.getcwd(),
            "ai_cs_project": os.environ["AI_CS_PROJECT_DIR"],
            "retention_days": os.environ["COORDINATED_BACKUP_RETENTION_DAYS"],
            "keep_latest": os.environ["COORDINATED_BACKUP_KEEP_LATEST"],
        }
    ),
    encoding="utf-8",
)
exit_code = int(os.environ.get("FAKE_BACKUP_EXIT", "0"))
if exit_code == 0 and os.environ.get("FAKE_SKIP_BACKUP_SET") != "1":
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"{timestamp}-{uuid.uuid4().hex[:12]}"
    backup = Path(sys.argv[1]) / f"coordinated-{batch_id}"
    backup.mkdir()
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "status": "complete",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
raise SystemExit(exit_code)
""",
            encoding="utf-8",
        )
        self.fake_backup.chmod(
            self.fake_backup.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        self.config = self.base / "backup.json"
        self.write_config()
        if os.name != "nt":
            self.config.chmod(0o600)
        self.env = os.environ.copy()
        self.env["FAKE_INVOCATION"] = str(self.invocation)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_config(self, **updates: object) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "center_project_dir": str(self.center.resolve()),
            "ai_cs_project_dir": str(self.ai_cs.resolve()),
            "backup_root": str(self.backups.resolve()),
            "health_file": str(self.health.resolve()),
            "bash_path": str(Path(sys.executable).resolve()),
            "retention_days": 30,
            "keep_latest": 7,
            "failure_webhook_url": "",
            "max_backup_age_hours": 36,
        }
        payload.update(updates)
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def run_backup(
        self,
        *,
        env: dict[str, str] | None = None,
        check_health: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args = [sys.executable, str(RUNNER), "--config", str(self.config.resolve())]
        if check_health:
            args.append("--check-health")
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env or self.env,
        )

    def test_success_persists_health_and_passes_validated_configuration(self) -> None:
        result = self.run_backup()

        self.assertEqual(result.returncode, 0, result.stderr)
        health = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "success")
        self.assertEqual(health["exit_code"], 0)
        self.assertTrue(health["last_success_at"])
        self.assertEqual(health["last_failure_at"], "")
        self.assertTrue(Path(health["latest_backup_set"]).is_dir())
        self.assertTrue(health["latest_backup_created_at"])
        invocation = json.loads(self.invocation.read_text(encoding="utf-8"))
        self.assertEqual(invocation["args"], [str(self.backups.resolve())])
        self.assertEqual(invocation["cwd"], str(self.center.resolve()))
        self.assertEqual(invocation["ai_cs_project"], str(self.ai_cs.resolve()))
        self.assertEqual(invocation["retention_days"], "30")
        self.assertEqual(invocation["keep_latest"], "7")
        self.assertEqual(list(self.health.parent.glob("*.tmp")), [])

    def test_failure_is_durable_and_preserves_last_success(self) -> None:
        first = self.run_backup()
        self.assertEqual(first.returncode, 0, first.stderr)
        previous_success = json.loads(
            self.health.read_text(encoding="utf-8")
        )["last_success_at"]
        env = self.env.copy()
        env["FAKE_BACKUP_EXIT"] = "47"

        failed = self.run_backup(env=env)

        self.assertEqual(failed.returncode, 47)
        health = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "failed")
        self.assertEqual(health["exit_code"], 47)
        self.assertEqual(health["last_success_at"], previous_success)
        self.assertTrue(health["last_failure_at"])
        self.assertTrue(Path(health["latest_backup_set"]).is_dir())

    def test_invalid_or_unknown_config_fails_before_invoking_backup(self) -> None:
        self.write_config(retention_days=0, unexpected=True)

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup config keys are invalid", result.stderr)
        self.assertFalse(self.invocation.exists())
        self.assertFalse(self.health.exists())

    def test_non_https_webhook_is_rejected_before_backup(self) -> None:
        self.write_config(failure_webhook_url="http://alerts.example.test/hook")

        result = self.run_backup()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an HTTPS URL", result.stderr)
        self.assertFalse(self.invocation.exists())

    def test_success_without_one_new_set_is_recorded_as_failure(self) -> None:
        env = self.env.copy()
        env["FAKE_SKIP_BACKUP_SET"] = "1"

        result = self.run_backup(env=env)

        self.assertEqual(result.returncode, 70)
        health = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertEqual(health["status"], "failed")
        self.assertEqual(health["exit_code"], 70)
        self.assertIn("must publish exactly one new", result.stderr)

    def test_health_check_detects_never_run_and_accepts_fresh_backup(self) -> None:
        self.health.write_text(
            json.dumps({"schema_version": 1, "status": "never_run"}),
            encoding="utf-8",
        )

        never_run = self.run_backup(check_health=True)
        self.assertEqual(never_run.returncode, 2)
        self.assertIn("backup_status_never_run", never_run.stderr)

        backed_up = self.run_backup()
        self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
        healthy = self.run_backup(check_health=True)
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        self.assertIn("Backup health is current", healthy.stdout)

    def test_health_check_detects_missing_latest_backup_set(self) -> None:
        backed_up = self.run_backup()
        self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
        health = json.loads(self.health.read_text(encoding="utf-8"))
        latest = Path(health["latest_backup_set"])
        (latest / "manifest.json").unlink()
        latest.rmdir()

        result = self.run_backup(check_health=True)

        self.assertEqual(result.returncode, 2)
        self.assertIn("latest_backup_set_missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
