from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from chaoslab import proctitle, settings as settings_module
from chaoslab.audit import AuditLog, Lease, _pid_alive
from chaoslab.chaos.cleanup import cleanup_records
from chaoslab.settings import PACKAGE_ROOT


def run(root: Path, poll_s: float = 1.0, experiment_id: str | None = None) -> int:
    settings = settings_module.load(root)
    lease = Lease(settings.lease_file, settings.stop_flag)
    audit = AuditLog(settings.audit_file)
    audit.record(None, "watchdog_started", watchdog_pid=os.getpid())
    while True:
        current = lease.read()
        if current is None or current.get("state") == "released":
            audit.record(current.get("experiment_id") if current else None, "watchdog_exit",
                         reason="lease released")
            return 0
        if experiment_id is not None and current.get("experiment_id") != experiment_id:
            audit.record(experiment_id, "watchdog_exit", reason="lease belongs to a different experiment")
            return 0
        controller_dead = not _pid_alive(current.get("controller_pid"))
        overdue = time.time() > float(current.get("deadline", 0))
        if controller_dead or overdue:
            reason = "controller process is gone" if controller_dead else "experiment deadline exceeded"
            experiment_id = current.get("experiment_id")
            audit.record(experiment_id, "watchdog_cleanup_begin", reason=reason)
            outcomes = cleanup_records(settings, current.get("records", []), audit, experiment_id, "watchdog")
            ok = all(o["ok"] for o in outcomes)
            latest = lease.read() or current
            if latest.get("state") != "released":
                lease.update(state="released", cleaned_by="watchdog", cleanup_ok=ok, records=[])
            audit.record(experiment_id, "watchdog_cleanup_done", ok=ok, outcomes=outcomes)
            return 0 if ok else 2
        time.sleep(poll_s)


#: Environment the watchdog needs from the controller (which deployment, where the lab lives, the reader secret).
PASS_ENV = ("CHAOSLAB_DEPLOYMENT", "CHAOSLAB_ROOT", "CHAOSLAB_VENV", "FSMES_ANALYST_PASSWORD", "PATH", "HOME")


def _command(root: Path, experiment_id: str | None) -> list[str]:
    command = [sys.executable, "-m", "chaoslab.chaos.watchdog", "--root", str(root)]
    if experiment_id:
        command += ["--experiment-id", experiment_id]
    return command


def _unit(experiment_id: str | None) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (experiment_id or "active"))[-80:]
    return f"chaoslab-watchdog-{safe}"


def spawn(root: Path, experiment_id: str | None = None, runner=subprocess.run) -> int:
    """Start the watchdog outside the controller's process tree.

    As a transient user-systemd service it belongs to the user's systemd manager, not to the controller, its
    terminal or its session, so killing any of those cannot take the watchdog with it. Without a usable user
    manager it falls back to a detached child process in its own session.
    """
    command = _command(root, experiment_id)
    unit = _unit(experiment_id)
    systemd = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
               f"--working-directory={PACKAGE_ROOT}", "-p", "KillMode=control-group"]
    for name in PASS_ENV:
        if os.environ.get(name):
            systemd.append(f"--setenv={name}={os.environ[name]}")
    try:
        started = runner([*systemd, "--", *command], capture_output=True, text=True, timeout=15, check=False)
        if started.returncode == 0:
            shown = runner(["systemctl", "--user", "show", "-p", "MainPID", "--value", f"{unit}.service"],
                           capture_output=True, text=True, timeout=10, check=False)
            pid = int((shown.stdout or "0").strip() or 0)
            if pid > 0:
                return pid
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    process = subprocess.Popen(command, cwd=str(PACKAGE_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    return process.pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chaoslab.chaos.watchdog")
    parser.add_argument("--root", required=True)
    parser.add_argument("--experiment-id")
    args = parser.parse_args(argv)
    proctitle.enable()
    proctitle.set_title(f"cl:watchdog guarding {args.experiment_id or 'the active experiment'}", "cl:watchdog")
    return run(Path(args.root), experiment_id=args.experiment_id)


if __name__ == "__main__":
    raise SystemExit(main())
