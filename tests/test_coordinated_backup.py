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
COORDINATED_BACKUP = ROOT / "deploy" / "coordinated_backup.sh"
VERIFY_BACKUP_SET = ROOT / "deploy" / "verify_backup_set.sh"


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


class CoordinatedBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()
        if cls.bash is None:
            raise unittest.SkipTest("no runnable Bash is installed")

    def setUp(self) -> None:
        controlled_temp_root = os.environ.get("COORDINATED_TEST_TEMP_ROOT")
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="coordinated-backup-test-",
            dir=controlled_temp_root or None,
        )
        self.base = Path(self.temp_dir.name)
        self.ai_project = self.base / "ai-cs"
        self.center_project = self.base / "center"
        self.backup_root = self.base / "backups"
        self.fake_bin = self.base / "bin"
        for path in (
            self.ai_project,
            self.center_project,
            self.backup_root,
            self.fake_bin,
        ):
            path.mkdir(parents=True)

        self.ai_env = self.ai_project / ".env"
        self.center_env = self.center_project / ".env.production"
        self.ai_compose = self.ai_project / "docker-compose.yml"
        self.center_compose = self.center_project / "docker-compose.production.yml"
        self.ai_env.write_text("APP_ENV=production\n", encoding="utf-8")
        self.center_env.write_text("CENTER_ENV=production\n", encoding="utf-8")
        self.ai_compose.write_text("services:\n  api: {}\n", encoding="utf-8")
        self.center_compose.write_text("services:\n  center: {}\n", encoding="utf-8")

        self.docker_log = self.base / "docker.log"
        self.service_state = self.base / "services.state"
        self.service_state.write_text(
            "api\nnginx\nknowledge-worker\ncenter\ncaddy\n",
            encoding="utf-8",
        )
        self.fake_docker = self.fake_bin / "docker"
        self.fake_docker.write_text(
            """#!/usr/bin/env python
from pathlib import Path
import os
import sys

args = sys.argv[1:]
with Path(os.environ["FAKE_DOCKER_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
commands = {"config", "ps", "stop", "up", "images", "exec"}
try:
    command_index, command_name = next(
        (index, value) for index, value in enumerate(args) if value in commands
    )
except StopIteration:
    print(f"unexpected fake docker call: {' '.join(args)}", file=sys.stderr)
    raise SystemExit(90)

state_path = Path(os.environ["FAKE_SERVICE_STATE"])
services = state_path.read_text(encoding="utf-8").splitlines()
if command_name == "config":
    raise SystemExit(0)
if command_name == "ps":
    print("\\n".join(services))
    raise SystemExit(0)
if command_name == "stop":
    stopped = set(args[command_index + 1 :])
    state_path.write_text(
        "".join(f"{service}\\n" for service in services if service not in stopped),
        encoding="utf-8",
    )
    raise SystemExit(0)
if command_name == "up":
    for service in args:
        if service in {"api", "nginx", "knowledge-worker", "center", "caddy"} and service not in services:
            services.append(service)
    state_path.write_text(
        "".join(f"{service}\\n" for service in services),
        encoding="utf-8",
    )
    raise SystemExit(0)
if command_name == "images":
    print("sha256:" + (("a" if args[-1] == "api" else "b") * 64))
    raise SystemExit(0)
if "mysql" in args:
    print("20260723_0002")
elif any("pg_restore" in value for value in args):
    sys.stdin.buffer.read()
    print("restore catalog ok")
else:
    print("20260723_0003")
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.fake_docker)
        self.fake_flock = self.fake_bin / "flock"
        self.fake_flock.write_text(
            """#!/usr/bin/env python
import os
raise SystemExit(1 if os.environ.get("FAKE_FLOCK_FAIL") == "1" else 0)
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.fake_flock)

        self.ai_backup_script = self.base / "fake_backup_ai_cs.sh"
        self.ai_backup_script.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
destination="$1"
name="ai-cs-$(date -u +'%Y%m%dT%H%M%SZ')"
target="${destination}/${name}"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
mkdir -p -- "$target" "${stage}/data/uploads"
printf 'CREATE TABLE example (id INT);\\n' | gzip -9 >"${target}/ai-cs-mysql.sql.gz"
printf 'upload\\n' >"${stage}/data/uploads/example.txt"
tar -C "$stage" -czf "${target}/ai-cs-data.tar.gz" data
(
  cd "$target"
  sha256sum ai-cs-mysql.sql.gz ai-cs-data.tar.gz | sed 's/ \\*/  /' >SHA256SUMS
)
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.ai_backup_script)

        self.center_backup_script = self.base / "fake_backup_center.sh"
        self.center_backup_script.write_text(
            """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${FAKE_CENTER_BACKUP_FAIL:-0}" == "1" ]]; then
  printf 'injected center backup failure\\n' >&2
  exit 55
fi
destination="$1"
name="wechat-ai-postgres-$(date -u +'%Y%m%dT%H%M%SZ').dump"
printf 'PGDMP test catalog\\n' >"${destination}/${name}"
(
  cd "$destination"
  sha256sum "$name" | sed 's/ \\*/  /' >"${name}.sha256"
)
""",
            encoding="utf-8",
            newline="\n",
        )
        self._make_executable(self.center_backup_script)

        python_dir = self._bash_path(Path(sys.executable).resolve().parent)
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self._bash_path(self.fake_bin)}:{python_dir}:/usr/bin:/bin",
                "FAKE_DOCKER_LOG": str(self.docker_log),
                "FAKE_SERVICE_STATE": str(self.service_state),
                "AI_CS_PROJECT_DIR": self._bash_path(self.ai_project),
                "AI_CS_ENV_FILE": self._bash_path(self.ai_env),
                "AI_CS_COMPOSE_FILE": self._bash_path(self.ai_compose),
                "AI_CS_BACKUP_SCRIPT": self._bash_path(self.ai_backup_script),
                "CENTER_ENV_FILE": self._bash_path(self.center_env),
                "CENTER_COMPOSE_FILE": self._bash_path(self.center_compose),
                "CENTER_BACKUP_SCRIPT": self._bash_path(self.center_backup_script),
                "VERIFY_SCRIPT": self._bash_path(VERIFY_BACKUP_SET),
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
        self.assertTrue(drive, f"expected a drive-qualified Windows path: {resolved}")
        suffix = resolved.as_posix().split(":", 1)[1]
        return f"/{drive}{suffix}"

    def _run(self, script: Path, argument: Path, *, env: dict[str, str] | None = None):
        return subprocess.run(
            [self.bash, self._bash_path(script), self._bash_path(argument)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env or self.env,
        )

    def _create_backup(self) -> Path:
        result = self._run(COORDINATED_BACKUP, self.backup_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        completed = list(self.backup_root.glob("coordinated-*"))
        self.assertEqual(len(completed), 1)
        self.assertEqual(list(self.backup_root.glob(".coordinated-*.partial.*")), [])
        return completed[0]

    def test_success_creates_one_verified_batch_and_restores_services(self) -> None:
        backup_set = self._create_backup()
        manifest = json.loads((backup_set / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(backup_set.name, f"coordinated-{manifest['batch_id']}")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["consistency"]["strategy"], "coordinated_write_stop")
        self.assertEqual(
            set(manifest["services"]),
            {"ai_cs", "wechat_center"},
        )
        for service in manifest["services"].values():
            self.assertEqual(service["batch_id"], manifest["batch_id"])
            self.assertRegex(service["backup"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(service["database"]["migration_revision"])
            self.assertRegex(
                service["service_version"]["image_id"],
                r"^sha256:[0-9a-f]{64}$",
            )

        verify_result = self._run(VERIFY_BACKUP_SET, backup_set)
        self.assertEqual(verify_result.returncode, 0, verify_result.stderr)
        self.assertEqual(
            set(self.service_state.read_text(encoding="utf-8").splitlines()),
            {"api", "nginx", "knowledge-worker", "center", "caddy"},
        )
        docker_log = self.docker_log.read_text(encoding="utf-8")
        for command in (
            "stop nginx",
            "stop api",
            "stop knowledge-worker",
            "stop caddy",
            "stop center",
            "up -d api",
            "up -d knowledge-worker",
            "up -d nginx",
            "up -d center",
            "up -d caddy",
        ):
            self.assertIn(command, docker_log)
        self.assertTrue((self.backup_root / ".coordinated-backup.lock").is_file())

    def test_verify_rejects_checksum_failure(self) -> None:
        backup_set = self._create_backup()
        manifest = json.loads((backup_set / "manifest.json").read_text(encoding="utf-8"))
        dump_path = backup_set / manifest["services"]["wechat_center"]["backup"]["path"]
        with dump_path.open("ab") as handle:
            handle.write(b"tampered")

        result = self._run(VERIFY_BACKUP_SET, backup_set)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("center child backup checksum failed", result.stderr)

    def test_child_backup_failure_removes_partial_batch_and_restores_services(self) -> None:
        env = self.env.copy()
        env["FAKE_CENTER_BACKUP_FAIL"] = "1"

        result = self._run(COORDINATED_BACKUP, self.backup_root, env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            [path.name for path in self.backup_root.iterdir()],
            [".coordinated-backup.lock"],
        )
        self.assertEqual(
            set(self.service_state.read_text(encoding="utf-8").splitlines()),
            {"api", "nginx", "knowledge-worker", "center", "caddy"},
        )
        docker_log = self.docker_log.read_text(encoding="utf-8")
        self.assertIn("up -d api", docker_log)
        self.assertIn("up -d knowledge-worker", docker_log)
        self.assertIn("up -d nginx", docker_log)
        self.assertIn("up -d center", docker_log)
        self.assertIn("up -d caddy", docker_log)
        self.assertTrue((self.backup_root / ".coordinated-backup.lock").is_file())

    def test_existing_lock_prevents_overlapping_backup(self) -> None:
        env = self.env.copy()
        env["FAKE_FLOCK_FAIL"] = "1"

        result = self._run(COORDINATED_BACKUP, self.backup_root, env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another coordinated backup is already running", result.stderr)
        self.assertTrue((self.backup_root / ".coordinated-backup.lock").is_file())
        self.assertFalse(self.docker_log.exists())

    def test_verify_rejects_path_traversal_and_partial_batch(self) -> None:
        backup_set = self._create_backup()
        manifest_path = backup_set / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["services"]["ai_cs"]["backup"]["path"] = "../outside"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        traversal_result = self._run(VERIFY_BACKUP_SET, backup_set)
        self.assertNotEqual(traversal_result.returncode, 0)
        self.assertIn("invalid path", traversal_result.stderr)

        manifest["services"]["ai_cs"]["backup"]["path"] = next(
            path.relative_to(backup_set).as_posix()
            for path in (backup_set / "ai-cs").iterdir()
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        partial_path = backup_set.with_name(f".{backup_set.name}.partial.test")
        backup_set.rename(partial_path)
        partial_result = self._run(VERIFY_BACKUP_SET, partial_path)
        self.assertNotEqual(partial_result.returncode, 0)
        self.assertIn("directory name and batch_id do not match", partial_result.stderr)

    def test_scripts_parse_with_bash(self) -> None:
        for script in (COORDINATED_BACKUP, VERIFY_BACKUP_SET):
            result = subprocess.run(
                [self.bash, "-n", self._bash_path(script)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
