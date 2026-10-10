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


def spawn(root: Path, experiment_id: str | None = None) -> int:
    command = [sys.executable, "-m", "chaoslab.chaos.watchdog", "--root", str(root)]
    if experiment_id:
        command += ["--experiment-id", experiment_id]
    process = subprocess.Popen(
        command,
        cwd=str(PACKAGE_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
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
