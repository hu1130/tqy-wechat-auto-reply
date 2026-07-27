from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProductionDeploymentStaticTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        path = ROOT / relative_path
        self.assertTrue(path.is_file(), f"missing deployment file: {relative_path}")
        return path.read_text(encoding="utf-8")

    def service_block(self, compose: str, service: str, next_service: str | None) -> str:
        start_match = re.search(rf"(?m)^  {re.escape(service)}:\s*$", compose)
        self.assertIsNotNone(start_match, f"missing service {service}")
        start = start_match.start()
        if next_service:
            end_match = re.search(rf"(?m)^  {re.escape(next_service)}:\s*$", compose[start + 1 :])
            self.assertIsNotNone(end_match, f"missing service {next_service}")
            end = start + 1 + end_match.start()
        else:
            end = compose.index("\nnetworks:\n", start)
        return compose[start:end]

    def test_compose_keeps_caddy_in_standalone_profile_and_database_internal(self) -> None:
        compose = self.read("docker-compose.production.yml")
        postgres = self.service_block(compose, "postgres", "center")
        center = self.service_block(compose, "center", "migrate")
        migrate = self.service_block(compose, "migrate", "caddy")
        caddy = self.service_block(compose, "caddy", None)

        self.assertNotIn("\n    ports:", postgres)
        self.assertNotIn("\n    expose:", postgres)
        self.assertNotIn("\n    ports:", center)
        self.assertIn('      - "8790"', center)
        self.assertNotIn("\n    ports:", migrate)
        self.assertIn('      - "80:80"', caddy)
        self.assertIn('      - "443:443"', caddy)
        self.assertIn("profiles:\n      - standalone-edge", caddy)
        self.assertNotRegex(caddy, r'"(?:0\.0\.0\.0:)?(?!80:80|443:443(?:/udp)?)[0-9]+:[0-9]+')
        self.assertIn("  database:\n    internal: true", compose)
        self.assertIn("name: wechat-platform-edge", compose)
        self.assertIn("- wechat-center-api", center)
        self.assertIn("postgres_data:/var/lib/postgresql", compose)

    def test_compose_has_health_restart_and_secret_indirection(self) -> None:
        compose = self.read("docker-compose.production.yml")
        for service, next_service in (("postgres", "center"), ("center", "migrate"), ("caddy", None)):
            block = self.service_block(compose, service, next_service)
            self.assertIn("restart: unless-stopped", block)
            self.assertIn("healthcheck:", block)

        self.assertIn("${POSTGRES_PASSWORD:?", compose)
        self.assertIn("${CENTER_DOMAIN:?", compose)
        self.assertIn("${ACME_EMAIL:?", compose)
        self.assertIn("env_file:\n      - ${CENTER_ENV_FILE:-.env.production}", compose)
        self.assertIn("@postgres:5432/", compose)
        self.assertNotIn("latest", compose)
        migrate = self.service_block(compose, "migrate", "caddy")
        self.assertIn('restart: "no"', migrate)
        self.assertIn('command: ["--env", ".env.production", "center", "init-db"]', migrate)
        self.assertIn("condition: service_completed_successfully", compose)
        self.assertIn("/readyz", compose)

    def test_environment_example_contains_only_explicit_placeholders(self) -> None:
        env_text = self.read(".env.production.example")
        assignments = {
            key: value
            for key, value in re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", env_text, flags=re.MULTILINE)
        }
        expected = {
            "COMPOSE_PROJECT_NAME",
            "CENTER_DOMAIN",
            "ACME_EMAIL",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "CENTER_API_KEY_PEPPER",
            "CENTER_SECRET_KEY",
            "CENTER_PROVISIONING_TOKEN",
        }
        self.assertEqual(set(assignments), expected)
        for key, value in assignments.items():
            self.assertTrue(value, f"{key} must use an explicit placeholder")
            self.assertIn("change_me", value.lower(), f"{key} does not look like a placeholder")
        self.assertNotIn("AGENT_TOKEN=", env_text)
        self.assertNotIn("CENTER_DATABASE_URL=", env_text)

    def test_real_production_environment_file_is_gitignored(self) -> None:
        gitignore = self.read(".gitignore").splitlines()
        self.assertIn(".env.production", gitignore)

    def test_docker_build_context_excludes_secrets_and_runtime_data(self) -> None:
        dockerignore = self.read(".dockerignore").splitlines()

        self.assertIn(".env", dockerignore)
        self.assertIn(".env.*", dockerignore)
        self.assertIn("logs", dockerignore)
        self.assertIn("data", dockerignore)
        self.assertIn("*.sqlite3", dockerignore)

    def test_dockerfile_runs_as_non_root_and_has_healthcheck(self) -> None:
        dockerfile = self.read("Dockerfile.center")
        self.assertRegex(dockerfile, r"FROM python:[0-9]+\.[0-9]+\.[0-9]+-slim-bookworm")
        self.assertNotIn("FROM python:latest", dockerfile)
        self.assertIn("USER app", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn('["python", "main.py"]', dockerfile)
        self.assertIn('"center", "serve", "--production"', dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+\.\s")

    def test_caddy_terminates_https_and_proxies_only_to_center(self) -> None:
        caddyfile = self.read("Caddyfile")
        self.assertIn("{$CENTER_DOMAIN}", caddyfile)
        self.assertIn("email {$ACME_EMAIL}", caddyfile)
        self.assertIn("reverse_proxy center:8790", caddyfile)
        self.assertIn("health_uri /readyz", caddyfile)
        self.assertIn("http://127.0.0.1:2015", caddyfile)
        self.assertIn("reverse_proxy @health center:8790", caddyfile)
        self.assertIn("Strict-Transport-Security", caddyfile)
        self.assertNotIn("tls internal", caddyfile)

    def test_backup_and_restore_scripts_fail_fast_and_verify_archives(self) -> None:
        backup = self.read("deploy/backup_postgres.sh")
        restore = self.read("deploy/restore_postgres.sh")
        for script in (backup, restore):
            self.assertIn("set -Eeuo pipefail", script)
            self.assertIn("umask 077", script)
            self.assertIn("realpath", script)
            self.assertIn("sha256sum", script)
            self.assertIn("docker compose", script)
            self.assertIn("pg_restore --list", script)
            self.assertNotRegex(script, r"(?m)^\s*(?:source|\.)\s+.*\.env")

        self.assertIn("mktemp --tmpdir", backup)
        self.assertIn("--format=custom", backup)
        self.assertIn("--confirm-replace-database", restore)
        self.assertIn('""|postgres|template0|template1', restore)
        self.assertIn("center remains stopped", restore)
        self.assertIn("--exit-on-error", restore)
        self.assertIn("PGDATABASE=postgres createdb", restore)
        coordinated = self.read("deploy/coordinated_backup.sh")
        self.assertIn("COORDINATED_BACKUP_RETENTION_DAYS", coordinated)
        self.assertIn("COORDINATED_BACKUP_KEEP_LATEST", coordinated)
        self.assertIn("prune_coordinated_backups.py", coordinated)

    def test_coordinated_restore_is_fail_closed_and_checks_cross_system_bindings(
        self,
    ) -> None:
        restore = self.read("deploy/coordinated_restore.sh")

        self.assertIn("--confirm-replace-both-systems", restore)
        self.assertIn("verify_backup_set.sh", restore)
        self.assertIn("stop nginx", restore)
        self.assertIn("RESTORE_START_INGRESS=0", restore)
        self.assertIn("personal_wechat_bindings", restore)
        self.assertIn("channel_accounts", restore)
        self.assertIn('up -d --wait nginx', restore)
        self.assertIn("all public ingress remains stopped", restore)

    def test_unattended_backup_timer_is_locked_observable_and_hardened(self) -> None:
        backup = self.read("deploy/coordinated_backup.sh")
        runner = self.read("deploy/scheduled_backup.py")
        installer = self.read("deploy/install_backup_timer.sh")
        docs = self.read("PRODUCTION_DEPLOYMENT.md")

        self.assertIn(".coordinated-backup.lock", backup)
        self.assertIn("flock -n", backup)
        self.assertIn("another coordinated backup is already running", backup)
        self.assertIn('"status": "running"', runner)
        self.assertIn('"status": "success" if succeeded else "failed"', runner)
        self.assertIn("os.replace(temporary_name, path)", runner)
        self.assertIn("failure_webhook_url must be an HTTPS URL", runner)
        self.assertIn("wechat-platform-backup.timer", installer)
        self.assertIn("wechat-platform-backup-monitor.timer", installer)
        self.assertIn("--check-health", installer)
        self.assertIn("MAX_BACKUP_AGE_HOURS", installer)
        self.assertIn("Persistent=true", installer)
        self.assertIn("NoNewPrivileges=true", installer)
        self.assertIn("ProtectSystem=full", installer)
        self.assertIn("systemd-analyze verify", installer)
        self.assertIn("/var/lib/wechat-platform/backup-health.json", docs)

    def test_shell_scripts_parse_with_bash(self) -> None:
        candidates = [shutil.which("bash")]
        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            candidates.extend((program_files / "Git/bin/bash.exe", program_files / "Git/usr/bin/bash.exe"))

        bash = None
        for candidate in dict.fromkeys(str(path) for path in candidates if path):
            probe = subprocess.run(
                [candidate, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if probe.returncode == 0:
                bash = candidate
                break
        if bash is None:
            self.skipTest("no runnable Bash is installed")

        for relative_path in (
            "deploy/backup_postgres.sh",
            "deploy/restore_postgres.sh",
            "deploy/coordinated_backup.sh",
            "deploy/coordinated_restore.sh",
            "deploy/verify_backup_set.sh",
            "deploy/install_backup_timer.sh",
        ):
            result = subprocess.run(
                [bash, "-n"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=self.read(relative_path),
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
