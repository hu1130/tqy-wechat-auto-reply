from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wechat_ai.windows_agent import (
    TASK_NAMESPACE,
    TaskSchedulerError,
    WindowsAgentTask,
    WindowsTaskScheduler,
    backup_sqlite_database,
    build_install_command,
    build_status_command,
    build_task_xml,
    build_uninstall_command,
)


NS = {"task": TASK_NAMESPACE}
ROOT = Path(__file__).resolve().parents[1]


def _result(returncode: int = 0, stdout: str = "ok", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class WindowsAgentTaskTests(unittest.TestCase):
    def make_task(self, root: Path) -> WindowsAgentTask:
        project = root / "Project & Files"
        project.mkdir()
        (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
        env_path = project / "config files" / "agent.env"
        env_path.parent.mkdir()
        env_path.write_text("STATE_DB_FILE=data/state.sqlite3\n", encoding="utf-8")
        pythonw = root / "Python Runtime" / "pythonw.exe"
        pythonw.parent.mkdir()
        pythonw.write_bytes(b"")
        pythonw.with_name("python.exe").write_bytes(b"")
        return WindowsAgentTask(
            project_dir=project,
            env_path=env_path,
            pythonw_path=pythonw,
            task_name="WeChat AI & Sales",
            user_id="ACME\\agent user",
            restart_interval_minutes=2,
            restart_count=20,
        )

    def test_task_xml_uses_interactive_logon_and_restarts_without_parallel_instances(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            task = self.make_task(Path(raw_root))
            root = ET.fromstring(build_task_xml(task))

        self.assertEqual(root.findtext("task:Triggers/task:LogonTrigger/task:UserId", namespaces=NS), task.user_id)
        self.assertEqual(root.findtext("task:Principals/task:Principal/task:LogonType", namespaces=NS), "InteractiveToken")
        self.assertEqual(root.findtext("task:Settings/task:MultipleInstancesPolicy", namespaces=NS), "IgnoreNew")
        self.assertEqual(root.findtext("task:Settings/task:RestartOnFailure/task:Interval", namespaces=NS), "PT2M")
        self.assertEqual(root.findtext("task:Settings/task:RestartOnFailure/task:Count", namespaces=NS), "20")
        self.assertEqual(root.findtext("task:Settings/task:Hidden", namespaces=NS), "true")
        self.assertEqual(root.findtext("task:Settings/task:ExecutionTimeLimit", namespaces=NS), "PT0S")

    def test_task_xml_has_explicit_executable_arguments_env_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            task = self.make_task(Path(raw_root))
            root = ET.fromstring(build_task_xml(task))

        executable = root.find("task:Actions/task:Exec", NS)
        assert executable is not None
        self.assertEqual(executable.findtext("task:Command", namespaces=NS), str(task.pythonw_path))
        self.assertEqual(executable.findtext("task:WorkingDirectory", namespaces=NS), str(task.project_dir))
        arguments = executable.findtext("task:Arguments", namespaces=NS) or ""
        self.assertIn(f'"{task.main_path}"', arguments)
        self.assertIn(f'"{task.env_path}"', arguments)
        self.assertIn("--env", arguments)
        self.assertTrue(arguments.endswith(" run"))

    def test_task_can_launch_hidden_watchdog_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            task = self.make_task(Path(raw_root))
            watchdog = task.project_dir / "run_watchdog.ps1"
            watchdog.write_text("param([switch]$Live)\n", encoding="utf-8")
            powershell = Path(raw_root) / "Windows PowerShell" / "powershell.exe"
            powershell.parent.mkdir()
            powershell.write_bytes(b"")
            task = replace(task, watchdog_path=watchdog, powershell_path=powershell)
            task.validate_files()
            root = ET.fromstring(build_task_xml(task))

        executable = root.find("task:Actions/task:Exec", NS)
        assert executable is not None
        self.assertEqual(executable.findtext("task:Command", namespaces=NS), str(powershell))
        arguments = executable.findtext("task:Arguments", namespaces=NS) or ""
        self.assertIn("-WindowStyle Hidden", arguments)
        self.assertIn(str(watchdog), arguments)
        self.assertIn("-Live", arguments)
        self.assertIn(str(task.env_path), arguments)
        self.assertIn(str(task.python_path), arguments)

    def test_center_task_launches_center_connector_through_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            task = self.make_task(Path(raw_root))
            watchdog = task.project_dir / "run_watchdog.ps1"
            watchdog.write_text("param([string]$Mode)\n", encoding="utf-8")
            powershell = Path(raw_root) / "Windows PowerShell" / "powershell.exe"
            powershell.parent.mkdir()
            powershell.write_bytes(b"")
            task = replace(
                task,
                watchdog_path=watchdog,
                powershell_path=powershell,
                mode="center",
            )
            root = ET.fromstring(build_task_xml(task))

        executable = root.find("task:Actions/task:Exec", NS)
        assert executable is not None
        arguments = executable.findtext("task:Arguments", namespaces=NS) or ""
        self.assertIn("-Mode center", arguments)
        self.assertNotIn("-Live", arguments)

    def test_packaged_task_runs_stable_launcher_without_source_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            install_root = root / "Installed Agent"
            install_root.mkdir()
            launcher = install_root / "WeChatAIAgentLauncher.exe"
            launcher.write_bytes(b"launcher")
            profile_dir = root / "User Profile" / "DeepAwake" / "WeChatAgent"
            profile_dir.mkdir(parents=True)
            task = WindowsAgentTask(
                project_dir=install_root,
                executable_path=launcher,
                profile_dir=profile_dir,
                mode="center",
                task_name="DeepAwake WeChat Agent",
                user_id="ACME\\agent user",
            )
            task.validate_files()
            document = ET.fromstring(build_task_xml(task))

        executable = document.find("task:Actions/task:Exec", NS)
        assert executable is not None
        self.assertEqual(executable.findtext("task:Command", namespaces=NS), str(launcher))
        self.assertEqual(
            executable.findtext("task:WorkingDirectory", namespaces=NS),
            str(install_root),
        )
        arguments = executable.findtext("task:Arguments", namespaces=NS) or ""
        self.assertEqual(arguments, f'run --profile "{profile_dir}"')
        self.assertNotIn("main.py", arguments)
        self.assertNotIn(".env", arguments)
        self.assertNotIn("python", arguments.lower())
        self.assertNotIn("watchdog", arguments.lower())
        self.assertEqual(
            document.findtext(
                "task:Principals/task:Principal/task:LogonType",
                namespaces=NS,
            ),
            "InteractiveToken",
        )

    def test_packaged_task_rejects_legacy_runtime_and_non_center_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            launcher = root / "launcher.exe"
            launcher.write_bytes(b"")
            profile = root / "profile"
            profile.mkdir()
            with self.assertRaisesRegex(ValueError, "only support center mode"):
                WindowsAgentTask(
                    project_dir=root,
                    executable_path=launcher,
                    profile_dir=profile,
                )
            with self.assertRaisesRegex(ValueError, "cannot use env_path"):
                WindowsAgentTask(
                    project_dir=root,
                    env_path=root / "agent.env",
                    executable_path=launcher,
                    profile_dir=profile,
                    mode="center",
                )

    def test_task_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            with self.assertRaisesRegex(ValueError, "mode must be local or center"):
                replace(self.make_task(Path(raw_root)), mode="unknown")

    def test_commands_are_argument_vectors_and_preserve_special_characters(self) -> None:
        xml_path = Path(tempfile.gettempdir()) / "Task Files & Data" / "agent.xml"
        install = build_install_command("Agent & Sales", xml_path)

        self.assertEqual(install, ("schtasks.exe", "/Create", "/TN", "Agent & Sales", "/XML", str(xml_path), "/F"))
        self.assertEqual(build_uninstall_command("Agent & Sales"), ("schtasks.exe", "/Delete", "/TN", "Agent & Sales", "/F"))
        self.assertEqual(
            build_status_command("Agent & Sales"),
            ("schtasks.exe", "/Query", "/TN", "Agent & Sales", "/V", "/FO", "LIST"),
        )

    def test_install_uses_injected_runner_and_removes_temporary_xml(self) -> None:
        calls: list[tuple[tuple[str, ...], bytes]] = []

        def runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
            xml_path = Path(command[command.index("/XML") + 1])
            calls.append((tuple(command), xml_path.read_bytes()))
            return _result()

        with tempfile.TemporaryDirectory() as raw_root:
            task = self.make_task(Path(raw_root))
            result = WindowsTaskScheduler(runner).install(task)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        command, payload = calls[0]
        self.assertEqual(command[0:2], ("schtasks.exe", "/Create"))
        self.assertFalse(Path(command[command.index("/XML") + 1]).exists())
        parsed = ET.fromstring(payload.decode("utf-16"))
        self.assertEqual(parsed.findtext("task:Principals/task:Principal/task:LogonType", namespaces=NS), "InteractiveToken")

    def test_uninstall_and_status_use_injected_runner(self) -> None:
        commands: list[tuple[str, ...]] = []

        def runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
            commands.append(tuple(command))
            return _result()

        scheduler = WindowsTaskScheduler(runner)
        scheduler.status("My Agent")
        scheduler.uninstall("My Agent")

        self.assertEqual(commands, [build_status_command("My Agent"), build_uninstall_command("My Agent")])

    def test_runner_failure_is_reported_with_command_output(self) -> None:
        scheduler = WindowsTaskScheduler(lambda command: _result(1, stderr="access denied"))
        with self.assertRaisesRegex(TaskSchedulerError, "access denied"):
            scheduler.status("My Agent")

    def test_task_rejects_relative_paths_and_missing_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_dir must be an absolute path"):
            WindowsAgentTask(Path("relative"), Path("C:/agent.env"), Path("C:/pythonw.exe"))

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            task = WindowsAgentTask(root / "missing", root / "missing.env", root / "pythonw.exe")
            with self.assertRaises(FileNotFoundError):
                task.validate_files()


class SQLiteBackupTests(unittest.TestCase):
    def create_database(self, path: Path, values: list[str]) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE events (value TEXT NOT NULL)")
            connection.executemany("INSERT INTO events (value) VALUES (?)", [(value,) for value in values])
            connection.commit()

    def read_values(self, path: Path) -> list[str]:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            return [row[0] for row in connection.execute("SELECT value FROM events ORDER BY rowid")]

    def test_backup_is_timestamped_valid_and_independent_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "state.sqlite3"
            self.create_database(source, ["first", "second"])

            backup = backup_sqlite_database(
                source,
                root / "backup files",
                now=datetime(2026, 7, 21, 12, 34, 56, 123456, tzinfo=timezone.utc),
            )
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("INSERT INTO events (value) VALUES ('third')")
                connection.commit()

            self.assertIn("20260721-123456-123456", backup.name)
            self.assertEqual(self.read_values(backup), ["first", "second"])
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertFalse(list(backup.parent.glob("*.tmp")))

    def test_backup_includes_committed_wal_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "wal.sqlite3"
            self.create_database(source, ["initial"])
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("INSERT INTO events (value) VALUES ('from-wal')")
                connection.commit()
                backup = backup_sqlite_database(source, root / "backups")

            self.assertEqual(self.read_values(backup), ["initial", "from-wal"])

    def test_backup_retention_keeps_only_requested_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "state.sqlite3"
            backups = root / "backups"
            self.create_database(source, ["value"])

            for second in range(5):
                with patch("wechat_ai.windows_agent.uuid.uuid4") as make_uuid:
                    make_uuid.return_value.hex = f"{second:032d}"
                    path = backup_sqlite_database(
                        source,
                        backups,
                        keep=2,
                        now=datetime(2026, 7, 21, 12, 0, second, tzinfo=timezone.utc),
                    )
                os.utime(path, ns=(second + 1, second + 1))

            remaining = sorted(backups.glob("state-backup-*.sqlite3"))
            self.assertEqual(len(remaining), 2)
            self.assertTrue(any("120003" in path.name for path in remaining))
            self.assertTrue(any("120004" in path.name for path in remaining))

    def test_backup_failure_cleans_temporary_and_final_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "state.sqlite3"
            backups = root / "backups"
            self.create_database(source, ["value"])

            with patch("wechat_ai.windows_agent._verify_sqlite", side_effect=RuntimeError("bad backup")):
                with self.assertRaisesRegex(RuntimeError, "bad backup"):
                    backup_sqlite_database(source, backups)

            self.assertFalse(list(backups.iterdir()))

    def test_backup_rejects_invalid_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            with self.assertRaises(FileNotFoundError):
                backup_sqlite_database(root / "missing.sqlite3")

            source = root / "state.sqlite3"
            self.create_database(source, ["value"])
            with self.assertRaisesRegex(ValueError, "keep must be at least 1"):
                backup_sqlite_database(source, keep=0)


class PackagingContractTests(unittest.TestCase):
    def test_inno_installer_is_current_user_activation_first_and_data_explicit(self) -> None:
        installer_path = ROOT / "packaging" / "agent-installer.iss"
        self.assertTrue(installer_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        script = installer_path.read_text(encoding="utf-8-sig")

        self.assertTrue((ROOT / "packaging" / "ChineseSimplified.isl").is_file())
        self.assertIn('MessagesFile: "{#SourcePath}\\ChineseSimplified.isl"', script)
        self.assertNotIn("compiler:Languages\\ChineseSimplified.isl", script)
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}", script)
        self.assertIn("DisableDirPage=yes", script)
        self.assertIn("ActivationPage.Add('一次性激活码：', False)", script)
        self.assertNotIn("CenterUrl", script)
        post_install = script[script.index("procedure CurStepChanged"):]
        self.assertLess(post_install.index("ActivateAgent();"), post_install.index("InstallLogonTask();"))
        self.assertIn("StopAndRemoveLogonTask();", post_install)
        self.assertIn("Type: files; Name: \"{localappdata}\\DeepAwake\\WeChatAgent\\profile.json\"", script)
        self.assertIn("同时删除本地消息队列、日志和报表", script)
        self.assertIn("DeleteRuntimeDataCheck.Checked := False", script)
        prepare = script[script.index("function PrepareToInstall"):script.index("procedure ActivateAgent")]
        self.assertIn("StopAndRemoveLogonTask();", prepare)
        stop_task = script[script.index("procedure StopAndRemoveLogonTask"):script.index("function PrepareToInstall")]
        self.assertLess(stop_task.index("/End /TN"), stop_task.index("/Delete /TN"))
        self.assertIn("/F /T /IM WeChatAIAgentLauncher.exe", stop_task)
        self.assertIn("/F /T /IM WeChatAIAgent.exe", stop_task)
        uninstall = script[script.index("[UninstallRun]"):script.index("[UninstallDelete]")]
        self.assertLess(uninstall.index("/End /TN"), uninstall.index("/Delete /TN"))
        self.assertEqual(uninstall.count("RunOnceId:"), 4)

    def test_build_script_fails_fast_and_builds_signed_x64_onedir(self) -> None:
        script = (ROOT / "build_agent.ps1").read_text(encoding="utf-8")
        agent_entry = (ROOT / "packaging" / "agent_entry.py").read_text(encoding="utf-8")

        self.assertIn("Python runtime must be native x64", script)
        self.assertIn('handle.read(4) != b"PE\\0\\0"', script)
        self.assertIn("0x8664", script)
        self.assertIn('"wxauto4": "41.1.2"', script)
        self.assertIn('"uiautomation": "2.0.29"', script)
        self.assertIn('"PyInstaller"', script)
        self.assertIn("Assert-HttpsUrl $CenterUrl", script)
        self.assertIn("Assert-HttpsUrl $UpdateManifestUrl", script)
        self.assertIn("Assert-TimestampUrl $TimestampUrl", script)
        self.assertIn("http://timestamp.digicert.com", script)
        self.assertIn("'dependency-check.py'", script)
        self.assertIn("Invoke-Checked $PythonPath @($DependencyCheckPath)", script)
        self.assertNotIn("@('-c', $DependencyCheck)", script)
        self.assertIn("--onedir", script)
        self.assertIn('find_spec("wxauto4")', script)
        self.assertNotIn('find_spec("wxauto")', script)
        self.assertIn("--collect-all', 'wxauto4'", script)
        self.assertIn("@('.exe', '.dll', '.pyd')", script)
        self.assertIn("@('verify', '/pa', '/q', $NativeFile.FullName)", script)
        self.assertIn(
            "Invoke-PackagedAgentCheck $AgentExe 'self-test' $GeneratedRoot",
            script,
        )
        self.assertIn("[Diagnostics.ProcessStartInfo]::new()", script)
        self.assertIn("$startInfo.UseShellExecute = $false", script)
        self.assertIn("$process.StandardOutput.ReadToEndAsync()", script)
        self.assertIn("$process.WaitForExit($TimeoutSeconds * 1000)", script)
        self.assertIn("$process.Kill()", script)
        self.assertIn("timed out after $TimeoutSeconds seconds", script)
        self.assertGreater(
            script.index(
                "Invoke-PackagedAgentCheck $AgentExe 'self-test' $GeneratedRoot"
            ),
            script.index("Invoke-Checked $SignToolPath @('verify', '/pa', '/v', $LauncherExe)"),
        )
        self.assertIn('"--center-url",', agent_entry)
        self.assertIn("build_config.center_url", agent_entry)
        self.assertIn("CertificateThumbprint", script)
        self.assertIn("ISCCPath", script)
        self.assertIn("signtool.exe", script)
        self.assertIn('Filter "*-$Version-x64.exe"', script)
        self.assertNotIn("微信客服Agent安装程序-$Version-x64.exe", script)
        self.assertIn("@('verify', '/pa', '/v', $InstallerPath)", script)


if __name__ == "__main__":
    unittest.main()
