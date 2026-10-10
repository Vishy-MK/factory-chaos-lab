from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def record(self, experiment_id: str | None, event: str, **details) -> None:
        entry = {"ts": now_iso(), "pid": os.getpid(), "experiment_id": experiment_id, "event": event}
        entry.update(details)
        line = json.dumps(entry, default=str, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read(self, experiment_id: str | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if experiment_id is None or entry.get("experiment_id") == experiment_id:
                entries.append(entry)
        return entries


class Lease:
    def __init__(self, path: Path, stop_flag: Path):
        self.path = Path(path)
        self.stop_flag = Path(stop_flag)
        self.lock_path = self.path.with_suffix(".lock")

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, default=str), encoding="utf-8")
        os.replace(temporary, self.path)

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def acquire(self, experiment_id: str, name: str, deadline_epoch: float) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        if existing and _pid_alive(existing.get("controller_pid")) and existing.get("state") != "released":
            return False
        if self.stop_flag.exists():
            self.stop_flag.unlink()
        self._write({
            "experiment_id": experiment_id, "name": name, "controller_pid": os.getpid(),
            "started": time.time(), "deadline": deadline_epoch, "state": "pending", "records": [],
            "watchdog_pid": None,
        })
        return True

    def update(self, **fields) -> dict:
        current = self.read() or {}
        current.update(fields)
        self._write(current)
        return current

    def add_record(self, record: dict) -> None:
        current = self.read() or {}
        records = current.get("records", [])
        records.append(record)
        current["records"] = records
        self._write(current)

    def clear_records(self) -> None:
        self.update(records=[])

    def release(self) -> None:
        current = self.read()
        if current:
            current["state"] = "released"
            current["records"] = []
            self._write(current)

    def stop_requested(self) -> bool:
        return self.stop_flag.exists()

    def request_stop(self) -> None:
        self.stop_flag.parent.mkdir(parents=True, exist_ok=True)
        self.stop_flag.write_text(now_iso(), encoding="utf-8")

    def active(self) -> dict | None:
        current = self.read()
        if current and current.get("state") != "released":
            return current
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True
