"""Lab-owned database latency hook for the plant's own SQLAlchemy engine, without changing the MES.

The MES opens one SQLAlchemy engine on the stdlib `pysqlite` driver (`fsmes.db.make_engine`). The lab starts its
plant with this directory on PYTHONPATH and `CHAOSLAB_DB_HOOK_*` in the environment; `sitecustomize` then calls
`install_from_environment` in the components named in `CHAOSLAB_DB_HOOK_COMPONENTS` (the plant's database clients).
Every other process, and every plant started without those variables, is left alone.

What it delays: statement execution. A class-level `before_cursor_execute` listener sleeps `delay_ms` before each
SQL statement SQLAlchemy sends through a DBAPI cursor to the plant database: SELECT, INSERT/UPDATE/DELETE,
`BEGIN`/`BEGIN IMMEDIATE`, SAVEPOINT/RELEASE and PRAGMA statements issued with `exec_driver_sql`. It does not
delay connection creation or the connect-time PRAGMAs (raw DBAPI calls), COMMIT/ROLLBACK (DBAPI `commit()` and
`rollback()`, not cursor statements), or code that opens `sqlite3` directly. Statements after `BEGIN IMMEDIATE`
are delayed while the transaction holds SQLite's single write lock, so other writers wait longer too, as they
would if the database itself were slow.

When it delays: only while `<hook dir>/control.json` names this plant and database, carries this boot's id, and
its CLOCK_MONOTONIC deadline (shared by every process on the host) is in the future and at most
HARD_MAX_WINDOW_S away. Expiry needs nobody: if the controller and watchdog both die, the delay stops by itself.
A statement already sleeping when the window ends finishes its sleep, so the overrun is at most one delay.

What it measures: per process, cumulative counts, errors, "database is locked" errors, injected delay, and
histograms of observed duration (driver time plus injected delay) and driver time alone, written atomically to
`<hook dir>/stats-<pid>.json`. The lab reads those files; the plant never reads them.

Standard library plus SQLAlchemy (already loaded by the MES). Nothing here may raise into the application.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
import uuid

VERSION = 1
ENV_DIR = "CHAOSLAB_DB_HOOK_DIR"
ENV_DATABASE = "CHAOSLAB_DB_HOOK_DATABASE"
ENV_COMPONENTS = "CHAOSLAB_DB_HOOK_COMPONENTS"
CONTROL_FILE = "control.json"
STATS_PREFIX = "stats-"
HARD_MAX_DELAY_MS = 2000
HARD_MAX_WINDOW_S = 120
CONTROL_POLL_S = 0.1
FLUSH_INTERVAL_S = 0.5
#: Upper bounds of the histogram buckets in milliseconds; one more bucket counts everything above the last bound.
BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 2000, 5000)
KINDS = ("read", "write", "transaction", "other")
_WRITES = {"INSERT", "UPDATE", "DELETE", "REPLACE", "UPSERT"}
_TRANSACTION = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "END"}


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def classify(statement: str) -> str:
    word = statement.lstrip().split(None, 1)[0].upper() if statement and statement.strip() else ""
    if word == "SELECT":
        return "read"
    if word in _WRITES:
        return "write"
    if word in _TRANSACTION:
        return "transaction"
    return "other"


def bucket_index(milliseconds: float) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if milliseconds <= bound:
            return index
    return len(BUCKETS_MS)


def _write_json(path: str, payload: dict) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(temporary, path)


def control_path(directory: str) -> str:
    return os.path.join(directory, CONTROL_FILE)


def write_control(directory: str, plant: str, database: str, delay_ms: int, duration_s: float,
                  clock=time.monotonic) -> dict:
    """Activate the delay for duration_s from now. Called by the lab, never by the plant."""
    if not 1 <= int(delay_ms) <= HARD_MAX_DELAY_MS:
        raise ValueError(f"delay_ms must be within 1..{HARD_MAX_DELAY_MS}")
    if not 0 < float(duration_s) <= HARD_MAX_WINDOW_S:
        raise ValueError(f"duration_s must be within 0..{HARD_MAX_WINDOW_S}")
    os.makedirs(directory, exist_ok=True)
    now = clock()
    control = {"version": VERSION, "token": uuid.uuid4().hex, "plant": plant,
               "database": os.path.realpath(database), "delay_ms": int(delay_ms), "boot_id": boot_id(),
               "activated_monotonic": now, "expires_monotonic": now + float(duration_s),
               "activated_at": time.time()}
    _write_json(control_path(directory), control)
    return control


def read_control(directory: str) -> dict | None:
    try:
        with open(control_path(directory), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def remove_control(directory: str) -> bool:
    """Remove the activation; True if a file was removed. Idempotent."""
    try:
        os.unlink(control_path(directory))
        return True
    except FileNotFoundError:
        return False


def control_problem(control: dict | None, plant: str, database: str, now: float, boot: str) -> str | None:
    """None when the control activates a delay for this plant and database right now, else why it does not."""
    if control is None:
        return "no activation"
    if control.get("unreadable"):
        return "activation file unreadable"
    if control.get("version") != VERSION:
        return "unknown activation version"
    if control.get("plant") != plant:
        return "activation names another plant"
    if control.get("database") != os.path.realpath(database):
        return "activation names another database"
    if control.get("boot_id") != boot:
        return "activation is from another boot"
    delay = control.get("delay_ms")
    if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= HARD_MAX_DELAY_MS:
        return "delay out of bounds"
    try:
        activated = float(control["activated_monotonic"])
        expires = float(control["expires_monotonic"])
    except (KeyError, TypeError, ValueError):
        return "activation has no deadline"
    if expires - activated > HARD_MAX_WINDOW_S or expires < activated or activated > now + 1.0:
        return "activation window out of bounds"
    if now >= expires:
        return "activation expired"
    return None


def read_stats(directory: str, live_only: bool = True) -> list[dict]:
    """Every hooked process's latest counters. With live_only, files of processes that have exited are skipped."""
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not (name.startswith(STATS_PREFIX) and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("version") != VERSION:
            continue
        if live_only and not _pid_alive(data.get("pid")):
            continue
        found.append(data)
    return found


def remove_dead_stats(directory: str) -> list[int]:
    removed = []
    for data in read_stats(directory, live_only=False):
        if not _pid_alive(data.get("pid")):
            try:
                os.unlink(os.path.join(directory, f"{STATS_PREFIX}{int(data['pid'])}.json"))
                removed.append(int(data["pid"]))
            except (OSError, KeyError, TypeError, ValueError):
                continue
    return removed


def _pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


class Hook:
    def __init__(self, directory: str, plant: str, database: str, component: str,
                 clock=time.monotonic, sleep=time.sleep):
        self.directory = directory
        self.plant = plant
        self.database = os.path.realpath(database)
        self.component = component
        self.clock = clock
        self.sleep = sleep
        self.pid = os.getpid()
        self.boot = boot_id()
        self._lock = threading.Lock()
        self._engines: dict[int, bool] = {}
        self._pending = threading.local()
        self._control: tuple[float, str | None, int | None] = (0.0, None, None)
        self._control_checked = -1.0
        self._control_signature = None
        self._control_view: dict = {"active": False, "problem": "no activation", "token": None}
        self._dirty = True
        self._last_flush = 0.0
        self._stop = threading.Event()
        self.counters = {
            "statements": {kind: 0 for kind in KINDS}, "statements_total": 0, "errors": 0, "lock_errors": 0,
            "observed_ms_total": 0.0, "db_ms_total": 0.0,
            "observed_histogram": [0] * (len(BUCKETS_MS) + 1), "db_histogram": [0] * (len(BUCKETS_MS) + 1),
            "injected": {"statements": 0, "ms_total": 0.0, "observed_ms_total": 0.0, "token": None,
                         "token_statements": 0,
                         "token_ms_total": 0.0, "token_first_monotonic": None, "token_last_monotonic": None},
        }
        self.installed_at = time.time()

    # -- activation -------------------------------------------------------------------------------------------

    def current_delay(self) -> tuple[float, str | None]:
        now = self.clock()
        if now - self._control_checked >= CONTROL_POLL_S:
            self._control_checked = now
            self._refresh_control(now)
        expires, token, delay_ms = self._control
        if token is None or delay_ms is None or now >= expires:
            if token is not None and now >= expires:
                self._control_view = {"active": False, "problem": "activation expired", "token": token}
            return 0.0, None
        return delay_ms / 1000.0, token

    def _refresh_control(self, now: float) -> None:
        path = control_path(self.directory)
        try:
            status = os.stat(path)
            signature = (status.st_ino, status.st_mtime_ns, status.st_size)
        except FileNotFoundError:
            signature = None
        except OSError:
            signature = ("error",)
        if signature == self._control_signature and signature is not None:
            return
        self._control_signature = signature
        control = None if signature is None else read_control(self.directory)
        problem = control_problem(control, self.plant, self.database, now, self.boot)
        if problem is None:
            self._control = (float(control["expires_monotonic"]), str(control["token"]), int(control["delay_ms"]))
            self._control_view = {"active": True, "problem": None, "token": control["token"]}
        else:
            self._control = (0.0, None, None)
            self._control_view = {"active": False, "problem": problem,
                                  "token": control.get("token") if isinstance(control, dict) else None}
        self._dirty = True

    # -- SQLAlchemy listeners ---------------------------------------------------------------------------------

    def _matches(self, engine) -> bool:
        key = id(engine)
        known = self._engines.get(key)
        if known is None:
            try:
                known = engine.dialect.name == "sqlite" and bool(engine.url.database) and \
                    os.path.realpath(engine.url.database) == self.database
            except Exception:
                known = False
            self._engines[key] = known
        return known

    def before_cursor_execute(self, conn, cursor, statement, parameters, context, executemany):
        try:
            if not self._matches(conn.engine):
                return
            delay, token = self.current_delay()
            started = time.perf_counter()
            if delay > 0:
                self.sleep(delay)
            self._pending.value = (started, time.perf_counter() - started if delay > 0 else 0.0, token, statement)
        except Exception:
            self._pending.value = None

    def after_cursor_execute(self, conn, cursor, statement, parameters, context, executemany):
        try:
            pending = getattr(self._pending, "value", None)
            self._pending.value = None
            if pending is not None:
                self._record(pending, failed=False, lock_error=False)
        except Exception:
            pass

    def handle_error(self, exception_context):
        try:
            pending = getattr(self._pending, "value", None)
            self._pending.value = None
            engine = getattr(exception_context, "engine", None)
            if pending is None and (engine is None or not self._matches(engine)):
                return
            message = str(getattr(exception_context, "original_exception", "") or "").lower()
            lock_error = "database is locked" in message or "database table is locked" in message \
                or "database is busy" in message
            if pending is None:
                with self._lock:
                    self.counters["errors"] += 1
                    self.counters["lock_errors"] += int(lock_error)
                    self._dirty = True
                return
            self._record(pending, failed=True, lock_error=lock_error)
        except Exception:
            pass

    def _record(self, pending, failed: bool, lock_error: bool) -> None:
        started, injected_s, token, statement = pending
        observed_ms = (time.perf_counter() - started) * 1000.0
        injected_ms = injected_s * 1000.0
        db_ms = max(0.0, observed_ms - injected_ms)
        kind = classify(statement)
        now = self.clock()
        with self._lock:
            counters = self.counters
            counters["statements"][kind] += 1
            counters["statements_total"] += 1
            counters["observed_ms_total"] += observed_ms
            counters["db_ms_total"] += db_ms
            counters["observed_histogram"][bucket_index(observed_ms)] += 1
            counters["db_histogram"][bucket_index(db_ms)] += 1
            if failed:
                counters["errors"] += 1
                counters["lock_errors"] += int(lock_error)
            if token is not None and injected_s > 0:
                injected = counters["injected"]
                injected["statements"] += 1
                injected["ms_total"] += injected_ms
                injected["observed_ms_total"] += observed_ms
                if injected["token"] != token:
                    injected.update(token=token, token_statements=0, token_ms_total=0.0,
                                    token_first_monotonic=now)
                injected["token_statements"] += 1
                injected["token_ms_total"] += injected_ms
                injected["token_last_monotonic"] = now
            self._dirty = True
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()

    # -- reporting --------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            counters = json.loads(json.dumps(self.counters))
            self._dirty = False
        return {"version": VERSION, "pid": self.pid, "plant": self.plant, "component": self.component,
                "database": self.database, "boot_id": self.boot, "installed_at": self.installed_at,
                "updated_at": time.time(), "updated_monotonic": self.clock(), "buckets_ms": list(BUCKETS_MS),
                "control": dict(self._control_view), **counters}

    def flush(self, force: bool = False) -> None:
        try:
            if not (force or self._dirty):
                return
            self._last_flush = self.clock()
            os.makedirs(self.directory, exist_ok=True)
            _write_json(os.path.join(self.directory, f"{STATS_PREFIX}{self.pid}.json"), self.snapshot())
        except Exception:
            pass

    def _flusher(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL_S):
            # Re-read the activation even without traffic so the stats show what the hook currently sees.
            try:
                self.current_delay()
            except Exception:
                pass
            self.flush()

    def start_flusher(self) -> None:
        thread = threading.Thread(target=self._flusher, name="chaoslab-dbhook", daemon=True)
        thread.start()

    def stop_flusher(self) -> None:
        self._stop.set()


_LISTENERS = ("before_cursor_execute", "after_cursor_execute", "handle_error")
_INSTALLED: Hook | None = None


def install(directory: str, plant: str, database: str, component: str, flusher: bool = True) -> Hook:
    global _INSTALLED
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    if _INSTALLED is not None:
        return _INSTALLED
    hook = Hook(directory, plant, database, component)
    for name in _LISTENERS:
        event.listen(Engine, name, getattr(hook, name))
    hook.flush(force=True)
    if flusher:
        hook.start_flusher()
    atexit.register(hook.flush, True)
    _INSTALLED = hook
    return hook


def uninstall() -> None:
    global _INSTALLED
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    hook, _INSTALLED = _INSTALLED, None
    if hook is None:
        return
    hook.stop_flusher()
    for name in _LISTENERS:
        if event.contains(Engine, name, getattr(hook, name)):
            event.remove(Engine, name, getattr(hook, name))


def install_from_environment(command: str) -> Hook | None:
    directory = os.environ.get(ENV_DIR)
    database = os.environ.get(ENV_DATABASE)
    plant = os.environ.get("MES_PLANT_NAME")
    components = [c for c in os.environ.get(ENV_COMPONENTS, "").split(",") if c]
    if not (directory and database and plant) or command not in components:
        return None
    return install(directory, plant, database, command)
