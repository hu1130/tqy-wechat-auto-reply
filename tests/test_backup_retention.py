from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "prune_coordinated_backups.py"


def find_bash() -> str | None:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates.extend(
            (
                program_files / "Git/bin/bash.exe",
                program_files / "Git/usr/bin/bash.exe",
            )
        )
    for candidate in dict.fromkeys(str(path) for path in candidates if path):
        result = subprocess.run(
            [candidate, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return candidate
    return None


class BackupRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()
        if cls.bash is None:
            raise unittest.SkipTest("no runnable Bash is installed")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="backup-retention-test-")
        self.root = Path(self.temp_dir.name) / "backups"
        self.root.mkdir()
        self.verify_log = Path(self.temp_dir.name) / "verified.log"
        self.verify_script = Path(self.temp_dir.name) / "verify.sh"
        self.verify_script.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\\n' "$1" >>"$VERIFY_LOG"
[[ "${FAIL_VERIFY_NAME:-}" != "$(basename "$1")" ]]
""",
            encoding="utf-8",
            newline="\n",
        )
        self.verify_script.chmod(
            self.verify_script.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        self.env = os.environ.copy()
        self.env["VERIFY_LOG"] = str(self.verify_log)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_backup(self, timestamp: str, nonce: str) -> Path:
        name = f"coordinated-{timestamp}-{nonce}"
        path = self.root / name
        path.mkdir()
        created_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        (path / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "batch_id": name.removeprefix("coordinated-"),
                    "status": "complete",
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                }
            ),
            encoding="utf-8",
        )
        return path

    def run_prune(
        self,
        *,
        confirm: bool,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = [
            sys.executable,
            str(SCRIPT),
            str(self.root.resolve()),
            "--retention-days",
            "1",
            "--keep-latest",
            "2",
            "--verify-script",
            str(self.verify_script.resolve()),
            "--bash-bin",
            self.bash,
        ]
        if confirm:
            args.append("--confirm-delete-old-backups")
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env or self.env,
        )

    def test_prunes_only_old_verified_sets_and_keeps_latest_two(self) -> None:
        old_a = self.create_backup("20260101T000000Z", "000000000001")
        old_b = self.create_backup("20260201T000000Z", "000000000002")
        keep_a = self.create_backup("20260301T000000Z", "000000000003")
        keep_b = self.create_backup("20260401T000000Z", "000000000004")
        unrelated = self.root / "manual-export"
        unrelated.mkdir()

        result = self.run_prune(confirm=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old_a.exists())
        self.assertFalse(old_b.exists())
        self.assertTrue(keep_a.exists())
        self.assertTrue(keep_b.exists())
        self.assertTrue(unrelated.exists())
        verified = self.verify_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual({Path(item).name for item in verified}, {old_a.name, old_b.name})

    def test_without_confirmation_is_a_verified_dry_run(self) -> None:
        old = self.create_backup("20260101T000000Z", "000000000001")
        self.create_backup("20260301T000000Z", "000000000002")
        self.create_backup("20260401T000000Z", "000000000003")

        result = self.run_prune(confirm=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(old.exists())
        self.assertIn("No backups deleted", result.stdout)

    def test_verification_failure_aborts_before_any_deletion(self) -> None:
        old_a = self.create_backup("20260101T000000Z", "000000000001")
        old_b = self.create_backup("20260201T000000Z", "000000000002")
        self.create_backup("20260301T000000Z", "000000000003")
        self.create_backup("20260401T000000Z", "000000000004")
        env = self.env.copy()
        env["FAIL_VERIFY_NAME"] = old_b.name

        result = self.run_prune(confirm=True, env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old_a.exists())
        self.assertTrue(old_b.exists())
        self.assertIn("refusing to prune", result.stderr)

    def test_rejects_symlink_backup_set(self) -> None:
        if os.name == "nt":
            self.skipTest("creating symlinks requires optional Windows privileges")
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        link = self.root / "coordinated-20260101T000000Z-000000000001"
        link.symlink_to(outside, target_is_directory=True)

        result = self.run_prune(confirm=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(outside.exists())
        self.assertIn("symbolic link", result.stderr)


if __name__ == "__main__":
    unittest.main()
