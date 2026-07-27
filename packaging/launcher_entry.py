from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from wechat_ai.agent_updater import AgentUpdater, UpdateError, UpdateStateStore


HEALTH_CONFIRM_TIMEOUT_SECONDS = 90
RESTART_FOR_UPDATE_EXIT_CODE = 75
UPDATE_FAILURE_EXIT_CODE = 76


def _fresh_running_health(profile_dir: Path, started_at: float) -> bool:
    path = profile_dir / "logs" / "health.json"
    try:
        if path.stat().st_mtime < started_at:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "running"


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _update_restart_requested(
    store: UpdateStateStore,
    launched_version: str,
    return_code: int,
) -> bool:
    if return_code != RESTART_FOR_UPDATE_EXIT_CODE:
        return False
    next_state = store.load()
    return (
        next_state.status == "pending_health"
        and next_state.active_version != launched_version
    )


def _emit_update_event(event: str, state: object) -> None:
    payload = {
        "event": event,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    for field_name in (
        "active_version",
        "target_version",
        "failed_version",
        "failure_code",
        "failure_summary",
        "failure_digest",
    ):
        value = getattr(state, field_name, "")
        if value:
            payload[field_name] = value
    print(json.dumps(payload), file=sys.stderr, flush=True)


def _handle_update_restart_exit(
    install_root: Path,
    store: UpdateStateStore,
    launched_version: str,
    return_code: int,
) -> bool | None:
    if return_code != RESTART_FOR_UPDATE_EXIT_CODE:
        return None
    if _update_restart_requested(store, launched_version, return_code):
        return True
    failed = AgentUpdater(install_root, launched_version).record_failure(
        "restart_without_pending_update",
        "Agent requested an update restart without a different pending release",
    )
    _emit_update_event("agent_update_failed", failed)
    return False


def run(profile_dir: Path) -> int:
    install_root = Path(sys.executable).resolve().parent
    store = UpdateStateStore(install_root)
    while True:
        state = store.recover_interrupted()
        if state.status == "rollback_failed":
            raise UpdateError(
                "Agent rollback is blocked because the retained release is unavailable"
            )
        executable = store.active_executable()
        started_at = time.time()
        try:
            process = subprocess.Popen(
                [str(executable), "run", "--profile", str(profile_dir)],
                cwd=executable.parent,
                shell=False,
            )
        except OSError as exc:
            if state.status != "pending_health":
                raise UpdateError("Agent process could not be started") from exc
            rolled_back = AgentUpdater(install_root, state.active_version).rollback(
                f"candidate process failed to start ({type(exc).__name__})"
            )
            _emit_update_event("agent_update_rolled_back", rolled_back)
            continue
        if state.status != "pending_health":
            return_code = process.wait()
            restart = _handle_update_restart_exit(
                install_root,
                store,
                state.active_version,
                return_code,
            )
            if restart is True:
                continue
            if restart is False:
                return UPDATE_FAILURE_EXIT_CODE
            return return_code

        deadline = time.monotonic() + HEALTH_CONFIRM_TIMEOUT_SECONDS
        restart_with_next_version = False
        rolled_back_state = None
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                rolled_back_state = AgentUpdater(
                    install_root,
                    state.active_version,
                ).rollback(
                    f"candidate exited before health confirmation with code {return_code}"
                )
                break
            if _fresh_running_health(profile_dir, started_at):
                confirmed = AgentUpdater(
                    install_root,
                    state.active_version,
                ).confirm_health(
                    state.active_version
                )
                if confirmed.status == "failed":
                    _emit_update_event("agent_update_cleanup_failed", confirmed)
                return_code = process.wait()
                restart = _handle_update_restart_exit(
                    install_root,
                    store,
                    state.active_version,
                    return_code,
                )
                if restart is True:
                    restart_with_next_version = True
                    break
                if restart is False:
                    return UPDATE_FAILURE_EXIT_CODE
                return return_code
            time.sleep(1)
        else:
            _stop_process(process)
            rolled_back_state = AgentUpdater(
                install_root,
                state.active_version,
            ).rollback(
                "candidate did not report healthy before the deadline"
            )
        if restart_with_next_version:
            continue
        if rolled_back_state is not None:
            _emit_update_event("agent_update_rolled_back", rolled_back_state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WeChatAIAgentLauncher")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    profile_dir = Path(args.profile).expanduser()
    if not profile_dir.is_absolute():
        raise RuntimeError("profile must be an absolute path")
    return run(profile_dir.resolve())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as exc:
        print(f"Launcher update state error: {exc}", file=sys.stderr)
        raise SystemExit(1)
