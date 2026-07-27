from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from build_config import load_package_config
from wechat_ai.agent_updater import (
    AgentUpdater,
    UpdateError,
    UpdateNotNewerError,
    UpdateStateStore,
    download_release,
    fetch_release_manifest,
)
from wechat_ai.agent_profile import AgentProfileStore, default_agent_data_dir
from wechat_ai.windows_agent import (
    DEFAULT_TASK_NAME,
    WindowsAgentTask,
    WindowsTaskScheduler,
)


UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
RESTART_FOR_UPDATE_EXIT_CODE = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="WeChatAIAgent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    activate = subparsers.add_parser("activate")
    activate.add_argument("--activation-file", required=True)
    activate.add_argument("--profile", required=True)

    install_task = subparsers.add_parser("install-task")
    install_task.add_argument("--launcher", required=True)
    install_task.add_argument("--profile", required=True)
    install_task.add_argument("--task-name", default=DEFAULT_TASK_NAME)

    uninstall_task = subparsers.add_parser("uninstall-task")
    uninstall_task.add_argument("--task-name", default=DEFAULT_TASK_NAME)

    status = subparsers.add_parser("status")
    status.add_argument("--task-name", default=DEFAULT_TASK_NAME)

    run = subparsers.add_parser("run")
    run.add_argument("--profile", required=True)

    subparsers.add_parser("version")
    subparsers.add_parser("self-test")
    return parser


def _absolute(value: str, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{field_name} must be an absolute path")
    return path.resolve()


def _install_root_from_runtime() -> Path:
    executable = Path(sys.executable).resolve()
    if not getattr(sys, "frozen", False) or len(executable.parents) < 3:
        raise RuntimeError("The packaged Agent runtime is required")
    if executable.parent.parent.name.lower() != "versions":
        raise RuntimeError("The packaged Agent must run from the versioned install directory")
    return executable.parent.parent.parent


def _require_default_profile(profile_dir: Path) -> AgentProfileStore:
    expected = default_agent_data_dir().resolve()
    if profile_dir != expected:
        raise RuntimeError(f"Packaged profile must use the current-user data directory: {expected}")
    return AgentProfileStore(profile_dir)


def _redirect_output(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _check_for_update(install_root: Path) -> bool:
    build_config = load_package_config()
    updater = AgentUpdater(install_root, build_config.version)
    state = updater.state_store.initialize(build_config.version)
    if state.active_version != build_config.version or state.status in {
        "pending_health",
        "confirming",
        "rolling_back",
        "rollback_failed",
    }:
        return False
    updater.begin_check()
    try:
        manifest = fetch_release_manifest(
            build_config.update_manifest_url,
            build_config.update_public_key,
            current_version=build_config.version,
        )
    except UpdateNotNewerError:
        updater.finish_check_without_update()
        return False
    except Exception as exc:
        updater.record_failure("check_failed", exc)
        raise

    updater.begin_download(manifest.version)
    archive: Path | None = None
    try:
        archive = download_release(manifest, updater.download_dir)
        updater.mark_downloaded(manifest.version)
        updater.apply(manifest, archive)
    except Exception as exc:
        failure_state = updater.state_store.load()
        if failure_state.status != "failed":
            updater.record_failure(
                "download_failed"
                if failure_state.status == "downloading"
                else "update_attempt_failed",
                exc,
                target_version=manifest.version,
            )
        raise
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
    return True


def _update_failure_event(
    install_root: Path,
    event: str,
    exc: BaseException,
) -> dict[str, str]:
    payload = {
        "event": event,
        "error_type": type(exc).__name__,
    }
    try:
        state = UpdateStateStore(install_root).load()
    except UpdateError:
        return payload
    if state.failure_code:
        payload["failure_code"] = state.failure_code
    if state.failure_summary:
        payload["failure_summary"] = state.failure_summary
    if state.failure_digest:
        payload["failure_digest"] = state.failure_digest
    if state.target_version:
        payload["target_version"] = state.target_version
    return payload


def _periodic_update_loop(install_root: Path) -> None:
    while True:
        time.sleep(UPDATE_CHECK_INTERVAL_SECONDS)
        try:
            updated = _check_for_update(install_root)
        except UpdateError as exc:
            print(
                json.dumps(
                    _update_failure_event(install_root, "agent_update_rejected", exc)
                ),
                flush=True,
            )
            continue
        except Exception as exc:
            print(
                json.dumps(
                    _update_failure_event(install_root, "agent_update_failed", exc)
                ),
                flush=True,
            )
            continue
        if updated:
            print(json.dumps({"event": "agent_update_staged"}), flush=True)
            os._exit(RESTART_FOR_UPDATE_EXIT_CODE)


def _run_agent(profile_dir: Path) -> int:
    store = _require_default_profile(profile_dir)
    store.load()
    bootstrap_log = store.paths.log.parent / "bootstrap.log"
    _redirect_output(bootstrap_log)
    install_root = _install_root_from_runtime()
    os.environ["WECHAT_AGENT_INSTALL_ROOT"] = str(install_root)
    os.environ["WECHAT_AGENT_RUNNING_VERSION"] = load_package_config().version
    try:
        if _check_for_update(install_root):
            return RESTART_FOR_UPDATE_EXIT_CODE
    except UpdateError as exc:
        print(
            json.dumps(
                _update_failure_event(install_root, "agent_update_rejected", exc)
            ),
            flush=True,
        )
    except Exception as exc:
        print(
            json.dumps(
                _update_failure_event(install_root, "agent_update_failed", exc)
            ),
            flush=True,
        )

    update_thread = threading.Thread(
        target=_periodic_update_loop,
        args=(install_root,),
        name="agent-updater",
        daemon=True,
    )
    update_thread.start()

    from main import main as application_main

    return application_main(
        ["--env", str(profile_dir / ".packaged-no-env"), "agent", "run"]
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    build_config = load_package_config()

    if args.command == "version":
        print(build_config.version)
        return 0
    if args.command == "self-test":
        from wxauto4 import WeChat

        if WeChat is None:
            raise RuntimeError("wxauto4 WeChat class is unavailable")
        return 0
    if args.command == "activate":
        activation_file = _absolute(args.activation_file, "activation_file")
        try:
            if activation_file.stat().st_size > 4096:
                raise RuntimeError("Activation code file is too large")
            activation_code = activation_file.read_text(encoding="utf-8").strip()
        finally:
            activation_file.unlink(missing_ok=True)
        profile_dir = _absolute(args.profile, "profile")
        _require_default_profile(profile_dir)
        from main import main as application_main

        return application_main(
            [
                "agent",
                "activate",
                "--code",
                activation_code,
                "--center-url",
                build_config.center_url,
            ]
        )
    if args.command == "install-task":
        launcher = _absolute(args.launcher, "launcher")
        profile_dir = _absolute(args.profile, "profile")
        _require_default_profile(profile_dir).load()
        install_root = launcher.parent
        initial_agent = install_root / "versions" / build_config.version / "WeChatAIAgent.exe"
        if not initial_agent.is_file():
            raise RuntimeError(f"Initial Agent release is missing: {initial_agent}")
        AgentUpdater(install_root, build_config.version).state_store.initialize(build_config.version)
        task = WindowsAgentTask(
            project_dir=install_root,
            executable_path=launcher,
            profile_dir=profile_dir,
            mode="center",
            task_name=args.task_name,
        )
        WindowsTaskScheduler().install(task)
        return 0
    if args.command == "uninstall-task":
        WindowsTaskScheduler().uninstall(args.task_name)
        return 0
    if args.command == "status":
        result = WindowsTaskScheduler().status(args.task_name)
        print(result.stdout or "")
        return 0
    if args.command == "run":
        return _run_agent(_absolute(args.profile, "profile"))
    raise RuntimeError("Unknown Agent command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Agent error: {exc}", file=sys.stderr)
        raise SystemExit(1)
