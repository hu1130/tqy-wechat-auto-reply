from __future__ import annotations

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
SCRIPT = ROOT / "deploy" / "coordinated_restore.sh"


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


class CoordinatedRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()
        if cls.bash is None:
            raise unittest.SkipTest("no runnable Bash is installed")

    def setUp(self) -> None:
        controlled_root = os.environ.get("COORDINATED_TEST_TEMP_ROOT")
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="coordinated-restore-test-",
            dir=controlled_root or None,
        )
        self.base = Path(self.temp_dir.name)
        self.ai_project = self.base / "ai-cs"
        self.center_project = self.base / "center"
        self.fake_bin = self.base / "bin"
        self.backup_set = self.base / "coordinated-20260723T070000Z-abcdef123456"
        self.ai_backup = self.backup_set / "ai-cs" / "ai-cs-20260723T070000Z"
        self.center_backup = (
            self.backup_set / "center" / "wechat-ai-postgres-20260723T070000Z.dump"
        )
        for path in (
            self.ai_project,
            self.center_project,
            self.fake_bin,
            self.ai_backup,
            self.center_backup.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.center_backup.write_bytes(b"PGDMP")
        (self.backup_set / "manifest.json").write_text(
            json.dumps(
                {
                    "services": {
                        "ai_cs": {
                            "backup": {
                                "path": "ai-cs/ai-cs-20260723T070000Z"
                            }
                        },
                        "wechat_center": {
                            "backup": {
                                "path": (
                                    "center/"
                                    "wechat-ai-postgres-20260723T070000Z.dump"
                                )
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        self.ai_env = self.ai_project / ".env"
        self.center_env = self.center_project / ".env.production"
        self.ai_compose = self.ai_project / "docker-compose.yml"
        self.center_compose = self.center_project / "docker-compose.production.yml"
        self.ai_env.write_text("APP_ENV=production\n", encoding="utf-8")
        self.center_env.write_text("CENTER_ENV=production\n", encoding="utf-8")
        self.ai_compose.write_text("services:\n  api: {}\n", encoding="utf-8")
        self.center_compose.write_text(
            "services:\n  center: {}\n", encoding="utf-8"
        )

        self.operation_log = self.base / "operations.log"
        self.service_state = self.base / "services.state"
        self.service_state.write_text("nginx\ncaddy\n", encoding="utf-8")
        self.fake_docker = self.fake_bin / "docker"
        self.fake_docker.write_text(
            """#!/usr/bin/env python
from pathlib import Path
import os
import sys

args = sys.argv[1:]
log = Path(os.environ["FAKE_OPERATION_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write("docker " + " ".join(args) + "\\n")
commands = {"config", "stop", "up", "exec"}
command_index, command = next(
    (index, value) for index, value in enumerate(args) if value in commands
)
state_path = Path(os.environ["FAKE_SERVICE_STATE"])
services = state_path.read_text(encoding="utf-8").splitlines()
if command == "config":
    raise SystemExit(0)
if command == "stop":
    stopped = set(args[command_index + 1:])
    state_path.write_text(
        "".join(f"{name}\\n" for name in services if name not in stopped),
        encoding="utf-8",
    )
    raise SystemExit(0)
if command == "up":
    for name in ("nginx", "caddy"):
        if name in args and name not in services:
            services.append(name)
    state_path.write_text(
        "".join(f"{name}\\n" for name in services),
        encoding="utf-8",
    )
    raise SystemExit(0)
if "mysql" in args:
    print("tenant-a\\tchannel-a")
elif "postgres" in args:
    print("tenant-a\\tchannel-a")
raise SystemExit(0)
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.fake_docker)

        self.verify_script = self.base / "verify.sh"
        self.verify_script.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
printf 'verify %s\\n' "$1" >>"$FAKE_OPERATION_LOG"
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.verify_script)

        self.center_restore = self.base / "restore-center.sh"
        self.center_restore.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$RESTORE_START_INGRESS" == "0" ]]
[[ "$2" == "--confirm-replace-database" ]]
printf 'restore center %s\\n' "$1" >>"$FAKE_OPERATION_LOG"
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.center_restore)

        self.ai_restore = self.base / "restore-ai.sh"
        self.ai_restore.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$RESTORE_START_INGRESS" == "0" ]]
[[ "$2" == "--confirm-replace-data" ]]
printf 'restore ai %s\\n' "$1" >>"$FAKE_OPERATION_LOG"
if [[ "${FAKE_AI_RESTORE_FAIL:-0}" == "1" ]]; then
  exit 47
fi
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.ai_restore)

        python_dir = self._bash_path(Path(sys.executable).resolve().parent)
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": (
                    f"{self._bash_path(self.fake_bin)}:{python_dir}:/usr/bin:/bin"
                ),
                "FAKE_OPERATION_LOG": str(self.operation_log),
                "FAKE_SERVICE_STATE": str(self.service_state),
                "AI_CS_PROJECT_DIR": self._bash_path(self.ai_project),
                "AI_CS_ENV_FILE": self._bash_path(self.ai_env),
                "AI_CS_COMPOSE_FILE": self._bash_path(self.ai_compose),
                "AI_CS_RESTORE_SCRIPT": self._bash_path(self.ai_restore),
                "CENTER_ENV_FILE": self._bash_path(self.center_env),
                "CENTER_COMPOSE_FILE": self._bash_path(self.center_compose),
                "CENTER_RESTORE_SCRIPT": self._bash_path(self.center_restore),
                "VERIFY_SCRIPT": self._bash_path(self.verify_script),
                "RESTORE_LOCK_DIR": self._bash_path(self.base / "restore.lock"),
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _make_executable(path: Path) -> None:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _bash_path(self, path: Path) -> str:
        if os.name != "nt":
            return str(path)
        resolved = path.resolve()
        drive = resolved.drive.rstrip(":").lower()
        suffix = resolved.as_posix().split(":", 1)[1]
        return f"/{drive}{suffix}"

    def _run(self, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self.bash,
                self._bash_path(SCRIPT),
                self._bash_path(self.backup_set),
                "--confirm-replace-both-systems",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env or self.env,
        )

    def test_success_keeps_ingress_down_until_both_restores_and_checks_finish(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        operations = self.operation_log.read_text(encoding="utf-8").splitlines()
        stop_nginx = next(
            index for index, line in enumerate(operations) if "stop nginx" in line
        )
        restore_center = next(
            index for index, line in enumerate(operations) if line.startswith("restore center")
        )
        restore_ai = next(
            index for index, line in enumerate(operations) if line.startswith("restore ai")
        )
        start_nginx = next(
            index for index, line in enumerate(operations) if "up -d --wait nginx" in line
        )
        self.assertLess(stop_nginx, restore_center)
        self.assertLess(restore_center, restore_ai)
        self.assertLess(restore_ai, start_nginx)
        self.assertEqual(
            self.service_state.read_text(encoding="utf-8").splitlines(),
            ["nginx"],
        )
        self.assertIn("Cross-system bindings verified: 1", result.stdout)
        self.assertFalse((self.base / "restore.lock").exists())

    def test_failure_leaves_all_public_ingress_stopped(self) -> None:
        env = self.env.copy()
        env["FAKE_AI_RESTORE_FAIL"] = "1"

        result = self._run(env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.service_state.read_text(encoding="utf-8").splitlines(),
            [],
        )
        self.assertIn("all public ingress remains stopped", result.stderr)
        self.assertFalse((self.base / "restore.lock").exists())

    def test_requires_explicit_dual_system_confirmation(self) -> None:
        result = subprocess.run(
            [
                self.bash,
                self._bash_path(SCRIPT),
                self._bash_path(self.backup_set),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--confirm-replace-both-systems", result.stderr)

    def test_script_parses_with_bash(self) -> None:
        result = subprocess.run(
            [self.bash, "-n", self._bash_path(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
