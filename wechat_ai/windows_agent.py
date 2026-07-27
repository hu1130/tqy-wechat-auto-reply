from __future__ import annotations

import getpass
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence


TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DEFAULT_TASK_NAME = "WeChat AI Agent"


class RunnerResult(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


Runner = Callable[[Sequence[str]], RunnerResult]


class TaskSchedulerError(RuntimeError):
    pass


def _current_user_id() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "").strip()
    if domain and "\\" not in username:
        return f"{domain}\\{username}"
    return username


def _absolute_path(value: str | os.PathLike[str], field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute path: {path}")
    return path


def _validate_text(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field_name} contains an invalid control character")
    return value


@dataclass(frozen=True)
class WindowsAgentTask:
    project_dir: Path
    env_path: Path | None = None
    pythonw_path: Path | None = None
    watchdog_path: Path | None = None
    powershell_path: Path | None = None
    executable_path: Path | None = None
    profile_dir: Path | None = None
    mode: str = "local"
    task_name: str = DEFAULT_TASK_NAME
    user_id: str = field(default_factory=_current_user_id)
    restart_interval_minutes: int = 1
    restart_count: int = 999

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_dir", _absolute_path(self.project_dir, "project_dir"))
        if self.env_path is not None:
            object.__setattr__(self, "env_path", _absolute_path(self.env_path, "env_path"))
        if self.pythonw_path is not None:
            object.__setattr__(self, "pythonw_path", _absolute_path(self.pythonw_path, "pythonw_path"))
        if self.executable_path is not None:
            object.__setattr__(
                self,
                "executable_path",
                _absolute_path(self.executable_path, "executable_path"),
            )
        if self.profile_dir is not None:
            object.__setattr__(self, "profile_dir", _absolute_path(self.profile_dir, "profile_dir"))
        packaged = self.executable_path is not None or self.profile_dir is not None
        if packaged:
            if self.executable_path is None or self.profile_dir is None:
                raise ValueError("executable_path and profile_dir must be configured together")
            if self.env_path is not None or self.pythonw_path is not None:
                raise ValueError("packaged tasks cannot use env_path or pythonw_path")
            if self.watchdog_path is not None or self.powershell_path is not None:
                raise ValueError("packaged tasks cannot use watchdog_path or powershell_path")
            if self.mode != "center":
                raise ValueError("packaged tasks only support center mode")
        elif self.env_path is None or self.pythonw_path is None:
            raise ValueError("legacy tasks require env_path and pythonw_path")
        if (self.watchdog_path is None) != (self.powershell_path is None):
            raise ValueError("watchdog_path and powershell_path must be configured together")
        if self.watchdog_path is not None:
            object.__setattr__(self, "watchdog_path", _absolute_path(self.watchdog_path, "watchdog_path"))
            object.__setattr__(self, "powershell_path", _absolute_path(self.powershell_path, "powershell_path"))
        object.__setattr__(self, "task_name", _validate_text(self.task_name, "task_name"))
        object.__setattr__(self, "user_id", _validate_text(self.user_id, "user_id"))
        if self.mode not in {"local", "center"}:
            raise ValueError("mode must be local or center")
        if self.restart_interval_minutes < 1:
            raise ValueError("restart_interval_minutes must be at least 1")
        if not 1 <= self.restart_count <= 999:
            raise ValueError("restart_count must be between 1 and 999")

    @property
    def main_path(self) -> Path:
        return self.project_dir / "main.py"

    @property
    def python_path(self) -> Path:
        if self.pythonw_path is None:
            raise RuntimeError("packaged tasks do not have a Python runtime path")
        return self.pythonw_path.with_name("python.exe")

    @property
    def packaged(self) -> bool:
        return self.executable_path is not None

    def validate_files(self) -> None:
        if not self.project_dir.is_dir():
            raise FileNotFoundError(f"Project directory does not exist: {self.project_dir}")
        if self.packaged:
            assert self.executable_path is not None
            assert self.profile_dir is not None
            if not self.executable_path.is_file():
                raise FileNotFoundError(f"packaged executable does not exist: {self.executable_path}")
            if not self.profile_dir.is_dir():
                raise FileNotFoundError(f"profile directory does not exist: {self.profile_dir}")
            return
        assert self.env_path is not None
        assert self.pythonw_path is not None
        for label, path in (
            ("main.py", self.main_path),
            ("environment file", self.env_path),
            ("pythonw.exe", self.pythonw_path),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"{label} does not exist: {path}")
        if self.watchdog_path is not None and not self.watchdog_path.is_file():
            raise FileNotFoundError(f"watchdog script does not exist: {self.watchdog_path}")
        if self.powershell_path is not None and not self.powershell_path.is_file():
            raise FileNotFoundError(f"powershell.exe does not exist: {self.powershell_path}")
        if self.watchdog_path is not None and not self.python_path.is_file():
            raise FileNotFoundError(f"python.exe does not exist: {self.python_path}")


def _task_element(parent: ET.Element, name: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.SubElement(parent, f"{{{TASK_NAMESPACE}}}{name}", attributes)
    element.text = text
    return element


def build_task_xml(task: WindowsAgentTask) -> str:
    """Build a current-user, interactive Task Scheduler definition.

    InteractiveToken is intentional: wxauto needs the signed-in desktop and
    cannot automate WeChat from a Session 0 Windows service.
    """

    ET.register_namespace("", TASK_NAMESPACE)
    root = ET.Element(f"{{{TASK_NAMESPACE}}}Task", {"version": "1.4"})

    registration = _task_element(root, "RegistrationInfo")
    _task_element(
        registration,
        "Description",
        "Starts the WeChat AI desktop agent when its Windows user signs in.",
    )

    triggers = _task_element(root, "Triggers")
    logon_trigger = _task_element(triggers, "LogonTrigger")
    _task_element(logon_trigger, "Enabled", "true")
    _task_element(logon_trigger, "UserId", task.user_id)

    principals = _task_element(root, "Principals")
    principal = _task_element(principals, "Principal", id="Author")
    _task_element(principal, "UserId", task.user_id)
    _task_element(principal, "LogonType", "InteractiveToken")
    _task_element(principal, "RunLevel", "LeastPrivilege")

    settings = _task_element(root, "Settings")
    _task_element(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _task_element(settings, "DisallowStartIfOnBatteries", "false")
    _task_element(settings, "StopIfGoingOnBatteries", "false")
    _task_element(settings, "AllowHardTerminate", "true")
    _task_element(settings, "StartWhenAvailable", "true")
    _task_element(settings, "RunOnlyIfNetworkAvailable", "false")
    _task_element(settings, "Enabled", "true")
    _task_element(settings, "Hidden", "true")
    _task_element(settings, "ExecutionTimeLimit", "PT0S")
    _task_element(settings, "Priority", "7")
    restart = _task_element(settings, "RestartOnFailure")
    _task_element(restart, "Interval", f"PT{task.restart_interval_minutes}M")
    _task_element(restart, "Count", str(task.restart_count))

    actions = _task_element(root, "Actions", Context="Author")
    executable = _task_element(actions, "Exec")
    if task.packaged:
        assert task.executable_path is not None
        assert task.profile_dir is not None
        _task_element(executable, "Command", str(task.executable_path))
        arguments = subprocess.list2cmdline(["run", "--profile", str(task.profile_dir)])
        working_directory = task.executable_path.parent
    elif task.watchdog_path is not None and task.powershell_path is not None:
        _task_element(executable, "Command", str(task.powershell_path))
        watchdog_arguments = [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(task.watchdog_path),
                "-Mode",
                task.mode,
                "-Env",
                str(task.env_path),
                "-PythonPath",
                str(task.python_path),
        ]
        if task.mode == "local":
            watchdog_arguments.append("-Live")
        arguments = subprocess.list2cmdline(watchdog_arguments)
        working_directory = task.project_dir
    else:
        assert task.pythonw_path is not None
        assert task.env_path is not None
        _task_element(executable, "Command", str(task.pythonw_path))
        command = (
            [str(task.main_path), "--env", str(task.env_path), "connector", "run-center"]
            if task.mode == "center"
            else [str(task.main_path), "--env", str(task.env_path), "run"]
        )
        arguments = subprocess.list2cmdline(command)
        working_directory = task.project_dir
    _task_element(executable, "Arguments", arguments)
    _task_element(executable, "WorkingDirectory", str(working_directory))

    return ET.tostring(root, encoding="unicode")


def build_install_command(task_name: str, xml_path: str | os.PathLike[str]) -> tuple[str, ...]:
    task_name = _validate_text(task_name, "task_name")
    xml_path = _absolute_path(xml_path, "xml_path")
    return ("schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F")


def build_uninstall_command(task_name: str) -> tuple[str, ...]:
    task_name = _validate_text(task_name, "task_name")
    return ("schtasks.exe", "/Delete", "/TN", task_name, "/F")


def build_status_command(task_name: str) -> tuple[str, ...]:
    task_name = _validate_text(task_name, "task_name")
    return ("schtasks.exe", "/Query", "/TN", task_name, "/V", "/FO", "LIST")


def subprocess_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


def _raise_for_failure(operation: str, result: RunnerResult) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "no command output").strip()
    raise TaskSchedulerError(f"Task Scheduler {operation} failed ({result.returncode}): {detail}")


class WindowsTaskScheduler:
    def __init__(self, runner: Runner = subprocess_runner) -> None:
        self._runner = runner

    def install(self, task: WindowsAgentTask) -> RunnerResult:
        task.validate_files()
        xml = build_task_xml(task)
        descriptor, raw_path = tempfile.mkstemp(prefix="wechat-ai-task-", suffix=".xml")
        xml_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                payload = ('<?xml version="1.0" encoding="utf-16"?>\r\n' + xml).encode("utf-16")
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            result = self._runner(build_install_command(task.task_name, xml_path))
            _raise_for_failure("install", result)
            return result
        finally:
            xml_path.unlink(missing_ok=True)

    def uninstall(self, task_name: str = DEFAULT_TASK_NAME) -> RunnerResult:
        result = self._runner(build_uninstall_command(task_name))
        _raise_for_failure("uninstall", result)
        return result

    def status(self, task_name: str = DEFAULT_TASK_NAME) -> RunnerResult:
        result = self._runner(build_status_command(task_name))
        _raise_for_failure("status", result)
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_sqlite(path: Path) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    if row != ("ok",):
        detail = row[0] if row else "no integrity result"
        raise RuntimeError(f"SQLite backup integrity check failed: {detail}")


def backup_sqlite_database(
    database_path: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str] | None = None,
    *,
    keep: int = 5,
    now: datetime | None = None,
) -> Path:
    """Create and verify an atomic SQLite snapshot, then prune old snapshots."""

    source = _absolute_path(database_path, "database_path")
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source}")
    if keep < 1:
        raise ValueError("keep must be at least 1")

    destination_dir = (
        _absolute_path(backup_dir, "backup_dir")
        if backup_dir is not None
        else source.parent / "backups"
    )
    destination_dir.mkdir(parents=True, exist_ok=True)

    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S-%f")
    prefix = f"{source.stem}-backup-"
    target = destination_dir / f"{prefix}{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{prefix}", suffix=".tmp", dir=destination_dir
    )
    os.close(descriptor)
    temp_path = Path(raw_temp_path)
    replaced = False

    try:
        with closing(sqlite3.connect(str(source))) as source_connection:
            with closing(sqlite3.connect(str(temp_path))) as backup_connection:
                source_connection.backup(backup_connection)
                backup_connection.commit()

        _fsync_file(temp_path)
        _verify_sqlite(temp_path)
        expected_checksum = _sha256(temp_path)

        os.replace(temp_path, target)
        replaced = True
        _fsync_file(target)
        _fsync_directory(destination_dir)

        if _sha256(target) != expected_checksum:
            raise RuntimeError("SQLite backup checksum changed during atomic installation")
        _verify_sqlite(target)

        backups = sorted(
            destination_dir.glob(f"{prefix}*.sqlite3"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for expired in backups[keep:]:
            expired.unlink()
        _fsync_directory(destination_dir)
        return target
    except Exception:
        if replaced:
            target.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_TASK_NAME",
    "TaskSchedulerError",
    "WindowsAgentTask",
    "WindowsTaskScheduler",
    "backup_sqlite_database",
    "build_install_command",
    "build_status_command",
    "build_task_xml",
    "build_uninstall_command",
    "subprocess_runner",
]
