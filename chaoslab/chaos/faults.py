from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import psutil

from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter, PlantError
from chaoslab.settings import PACKAGE_ROOT, Settings


@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    default: Any = None
    required: bool = False
    choices: tuple = ()

    def describe(self) -> dict:
        return {
            "name": self.name, "type": self.kind, "description": self.description,
            "minimum": self.minimum, "maximum": self.maximum, "default": self.default,
            "required": self.required, "choices": list(self.choices) or None,
        }


@dataclass(frozen=True)
class FaultSpec:
    name: str
    layer: str
    summary: str
    params: tuple[Param, ...]
    target_keys: tuple[str, ...]
    expected_effects: tuple[str, ...]
    cleanup: str
    recovery_criteria: tuple[str, ...]
    preconditions: tuple[str, ...]
    supported: bool = True
    unsupported_reason: str | None = None

    def describe(self) -> dict:
        return {
            "name": self.name,
            "layer": self.layer,
            "summary": self.summary,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
            "parameters": [p.describe() for p in self.params],
            "target_keys": list(self.target_keys),
            "expected_effects": list(self.expected_effects),
            "cleanup": self.cleanup,
            "recovery_criteria": list(self.recovery_criteria),
            "preconditions": list(self.preconditions),
        }


@dataclass
class FaultContext:
    settings: Settings
    plant: FactorySemanticsAdapter
    stations: tuple[str, ...] = ()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class Fault:
    spec: FaultSpec

    def hold_seconds(self, params: dict) -> float:
        return 0.0

    def preconditions(self, ctx: FaultContext, target: dict, params: dict) -> list[Check]:
        return []

    def intent(self, target: dict, params: dict) -> dict:
        return {"type": self.spec.name, **{k: v for k, v in target.items() if k != "plant"}}

    def inject(self, ctx: FaultContext, target: dict, params: dict) -> dict:
        raise NotImplementedError

    def cleanup(self, ctx: FaultContext, record: dict) -> Check:
        raise NotImplementedError

    def removed(self, ctx: FaultContext, record: dict) -> Check:
        raise NotImplementedError


class NoFault(Fault):
    spec = FaultSpec(
        name="none",
        layer="control",
        summary="No fault: observe the system for the stated duration to establish a baseline.",
        params=(Param("duration_s", "int", "observation window in seconds", 1, 60, 15),),
        target_keys=("plant",),
        expected_effects=("no change from baseline",),
        cleanup="nothing to clean up",
        recovery_criteria=("application healthy", "all machines connected"),
        preconditions=("plant healthy",),
    )

    def hold_seconds(self, params: dict) -> float:
        return float(params.get("duration_s", 15))

    def inject(self, ctx, target, params):
        return {"type": "none"}

    def cleanup(self, ctx, record):
        return Check("cleanup", True, "nothing injected")

    def removed(self, ctx, record):
        return Check("fault_removed", True, "nothing injected")


def _component_precondition(ctx: FaultContext, component: str) -> list[Check]:
    found = ctx.plant.find_components()
    return [Check("component_running", component in found, f"{component} process present: {component in found}")]


COMPONENTS = ("api", "opc-sim", "opc-agent", "floor")


class ProcessOutage(Fault):
    spec = FaultSpec(
        name="process_outage",
        layer="infrastructure",
        summary=("Kill one plant component process (SIGKILL by default), keep it down for duration_s, then start it "
                 "again with its original configuration."),
        params=(
            Param("duration_s", "int", "seconds the component stays down", 1, 60, 10),
            Param("signal", "choice", "signal used to stop the process", default="KILL", choices=("KILL", "TERM")),
        ),
        target_keys=("plant", "component"),
        expected_effects=(
            "api: health endpoint refuses connections, floor simulator steps fail until restart",
            "opc-sim: machines report disconnected, tag ingestion pauses",
            "opc-agent: no machine data is booked while it is down",
            "floor: no simulated operator activity while it is down",
        ),
        cleanup="restart the component with `fsmes <command>` using the plant's own environment; re-register its pid",
        recovery_criteria=("component process running", "api healthy", "authenticated API operation succeeds",
                           "all machines connected", "tag ingestion advancing", "floor recording checks",
                           "database integrity check ok"),
        preconditions=("component process running", "plant healthy"),
    )

    def hold_seconds(self, params):
        return float(params.get("duration_s", 10))

    def preconditions(self, ctx, target, params):
        return _component_precondition(ctx, target["component"])

    def inject(self, ctx, target, params):
        component = target["component"]
        sig = signal.SIGKILL if params.get("signal", "KILL") == "KILL" else signal.SIGTERM
        pid = ctx.plant.kill_component(component, sig)
        if pid is None:
            raise PlantError(f"component {component} was not running")
        return {"type": "process_outage", "component": component, "killed_pid": pid,
                "signal": params.get("signal", "KILL")}

    def cleanup(self, ctx, record):
        component = record["component"]
        try:
            if component in ctx.plant.find_components():
                return Check("cleanup", True, f"{component} already running")
            pid = ctx.plant.spawn_component(component)
        except Exception as exc:
            return Check("cleanup", False, f"could not restart {component}: {exc}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if component in ctx.plant.find_components():
                return Check("cleanup", True, f"{component} restarted as pid {pid}")
            time.sleep(0.3)
        return Check("cleanup", False, f"{component} did not appear after restart (pid {pid})")

    def removed(self, ctx, record):
        running = record["component"] in ctx.plant.find_components()
        return Check("fault_removed", running, f"{record['component']} running: {running}")


class ProcessFreeze(Fault):
    spec = FaultSpec(
        name="process_freeze",
        layer="infrastructure",
        summary=("Suspend one plant component with SIGSTOP for duration_s, then resume it with SIGCONT. The process "
                 "stays alive but makes no progress and answers nothing."),
        params=(Param("duration_s", "int", "seconds the component stays suspended", 1, 60, 10),),
        target_keys=("plant", "component"),
        expected_effects=(
            "api: requests time out while connections stay open; floor simulator steps stall",
            "opc-sim: subscriptions go silent, agent may report machines disconnected",
            "opc-agent: no machine data booked until resumed",
            "floor: simulated operator activity pauses",
        ),
        cleanup="send SIGCONT to the recorded pid; verify the process state is no longer stopped",
        recovery_criteria=("component process running and not stopped", "api healthy",
                           "authenticated API operation succeeds", "all machines connected",
                           "tag ingestion advancing", "floor recording checks", "database integrity check ok"),
        preconditions=("component process running", "plant healthy"),
    )

    def hold_seconds(self, params):
        return float(params.get("duration_s", 10))

    def preconditions(self, ctx, target, params):
        return _component_precondition(ctx, target["component"])

    def inject(self, ctx, target, params):
        component = target["component"]
        pid = ctx.plant.signal_component(component, signal.SIGSTOP)
        if pid is None:
            raise PlantError(f"component {component} was not running")
        return {"type": "process_freeze", "component": component, "pid": pid}

    def cleanup(self, ctx, record):
        pid = record.get("pid")
        if pid is None:
            found = ctx.plant.find_components().get(record.get("component"))
            if found is None:
                return Check("cleanup", True, f"{record.get('component')} not running; nothing to resume")
            pid = found.pid
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return Check("cleanup", True, f"pid {pid} no longer exists")
        try:
            os.kill(pid, signal.SIGCONT)
        except OSError as exc:
            return Check("cleanup", False, f"SIGCONT to {pid} failed: {exc}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.status() != psutil.STATUS_STOPPED:
                return Check("cleanup", True, f"pid {pid} resumed")
            time.sleep(0.1)
        return Check("cleanup", False, f"pid {pid} still stopped after SIGCONT")

    def removed(self, ctx, record):
        pid = record.get("pid")
        if pid is None:
            found = ctx.plant.find_components().get(record.get("component"))
            if found is None:
                return Check("fault_removed", True, "process not running")
            pid = found.pid
        try:
            status = psutil.Process(pid).status()
        except psutil.NoSuchProcess:
            return Check("fault_removed", True, "process gone")
        return Check("fault_removed", status != psutil.STATUS_STOPPED, f"process status {status}")


class CpuPressure(Fault):
    spec = FaultSpec(
        name="cpu_pressure",
        layer="infrastructure",
        summary=("Pin the API process to one logical CPU and run a duty-cycled busy loop pinned to the same CPU, "
                 "for duration_s. The rest of the host is not pinned or throttled."),
        params=(
            Param("duration_s", "int", "seconds of pressure", 1, 30, 15),
            Param("percent", "int", "busy-loop duty cycle", 10, 80, 60),
        ),
        target_keys=("plant", "component"),
        expected_effects=("api: higher request latency on the pinned CPU while contention lasts",
                          "other components: no direct effect"),
        cleanup="terminate the burner process; restore the API's original CPU affinity",
        recovery_criteria=("burner process gone", "API affinity restored", "api healthy",
                           "authenticated API operation succeeds"),
        preconditions=("component process running", "plant healthy"),
    )

    def hold_seconds(self, params):
        return float(params.get("duration_s", 15))

    def preconditions(self, ctx, target, params):
        return _component_precondition(ctx, target["component"])

    def inject(self, ctx, target, params):
        component = target["component"]
        proc = ctx.plant.find_components().get(component)
        if proc is None:
            raise PlantError(f"component {component} was not running")
        original = proc.cpu_affinity()
        try:
            cpu = proc.cpu_num()
        except (AttributeError, psutil.Error):
            cpu = original[0]
        if cpu not in original:
            cpu = original[0]
        seconds = int(params.get("duration_s", 15))
        percent = int(params.get("percent", 60))
        burner = subprocess.Popen(
            [sys.executable, "-m", "chaoslab.chaos.holders", "cpu_burn", "--seconds", str(seconds),
             "--percent", str(percent), "--cpu", str(cpu)],
            cwd=str(PACKAGE_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            proc.cpu_affinity([cpu])
        except Exception:
            burner.kill()
            raise
        return {"type": "cpu_pressure", "component": component, "pid": proc.pid, "original_affinity": original,
                "cpu": cpu, "burner_pid": burner.pid, "percent": percent, "seconds": seconds}

    def cleanup(self, ctx, record):
        problems = []
        burner = record.get("burner_pid")
        if burner:
            try:
                os.kill(burner, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                problems.append(f"burner kill failed: {exc}")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(burner):
                time.sleep(0.1)
            if _alive(burner):
                try:
                    os.kill(burner, signal.SIGKILL)
                except OSError as exc:
                    problems.append(f"burner SIGKILL failed: {exc}")
        if record.get("pid") is not None and record.get("original_affinity"):
            try:
                psutil.Process(record["pid"]).cpu_affinity(record["original_affinity"])
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError, ValueError) as exc:
                problems.append(f"affinity restore failed: {exc}")
        if _alive(record.get("burner_pid")):
            problems.append("burner still alive")
        return Check("cleanup", not problems, "; ".join(problems) or "burner stopped, affinity restored")

    def removed(self, ctx, record):
        burner_alive = _alive(record.get("burner_pid"))
        original = record.get("original_affinity")
        if record.get("pid") is None or not original:
            return Check("fault_removed", not burner_alive, f"burner alive: {burner_alive}; no affinity recorded")
        try:
            current = psutil.Process(record["pid"]).cpu_affinity()
        except psutil.NoSuchProcess:
            current = original
        restored = sorted(current) == sorted(original)
        return Check("fault_removed", (not burner_alive) and restored,
                     f"burner alive: {burner_alive}; affinity restored: {restored}")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class SqliteWriteLock(Fault):
    spec = FaultSpec(
        name="sqlite_write_lock",
        layer="infrastructure",
        summary=("Hold the plant database's write lock (BEGIN IMMEDIATE, never commit) from a separate process for "
                 "duration_s, then roll back. Writers wait on the lock; no data is changed."),
        params=(Param("duration_s", "int", "seconds the write lock is held", 1, 60, 10),),
        target_keys=("plant", "resource"),
        expected_effects=("agent and floor writes block and may fail with 'database is locked'",
                          "read-only requests may continue (WAL mode) or block (rollback-journal mode)"),
        cleanup="terminate the lock holder, which rolls back its empty transaction; probe that the lock is free",
        recovery_criteria=("lock holder gone", "write lock acquirable", "api healthy",
                           "authenticated API operation succeeds", "tag ingestion advancing",
                           "database integrity check ok"),
        preconditions=("database file present", "write lock currently free"),
    )

    def hold_seconds(self, params):
        return float(params.get("duration_s", 10))

    def preconditions(self, ctx, target, params):
        path = ctx.settings.database_file
        checks = [Check("database_present", path.is_file(), str(path))]
        if path.is_file():
            free, detail = _lock_is_free(str(path))
            checks.append(Check("write_lock_free", free, detail))
        return checks

    def inject(self, ctx, target, params):
        path = str(ctx.settings.database_file)
        seconds = int(params.get("duration_s", 10))
        holder = subprocess.Popen(
            [sys.executable, "-m", "chaoslab.chaos.holders", "sqlite_lock", "--db", path, "--seconds", str(seconds)],
            cwd=str(PACKAGE_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, text=True,
        )
        deadline = time.monotonic() + 10
        line = ""
        import select

        while time.monotonic() < deadline:
            ready, _, _ = select.select([holder.stdout], [], [], 0.2)
            if ready:
                line = holder.stdout.readline().strip()
                break
            if holder.poll() is not None:
                break
        if line != "LOCKED":
            try:
                holder.kill()
            except OSError:
                pass
            raise PlantError(f"lock holder did not acquire the write lock (said {line!r}, exit {holder.poll()})")
        return {"type": "sqlite_write_lock", "holder_pid": holder.pid, "database": path, "seconds": seconds}

    def cleanup(self, ctx, record):
        holder = record.get("holder_pid")
        database = record.get("database") or str(ctx.settings.database_file)
        if holder and _alive(holder):
            try:
                os.kill(holder, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(holder):
                time.sleep(0.1)
            if _alive(holder):
                try:
                    os.kill(holder, signal.SIGKILL)
                except OSError:
                    pass
                time.sleep(0.3)
        if holder and _alive(holder):
            return Check("cleanup", False, f"lock holder {holder} still alive")
        free, detail = _lock_is_free(database)
        return Check("cleanup", free, f"holder stopped; {detail}")

    def removed(self, ctx, record):
        holder_alive = _alive(record.get("holder_pid"))
        free, detail = _lock_is_free(record.get("database") or str(ctx.settings.database_file))
        return Check("fault_removed", (not holder_alive) and free, f"holder alive: {holder_alive}; {detail}")


def _lock_is_free(path: str) -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(path, timeout=1.0, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        return False, f"write lock not acquirable: {exc}"
    return True, "write lock acquirable"


class SimulatorFault(Fault):
    event_type: str
    needs_station = True
    windowed = True

    def hold_seconds(self, params):
        return 0.0

    def event(self, target: dict, params: dict) -> dict:
        raise NotImplementedError

    def window_line_seconds(self, params: dict) -> float:
        if self.windowed:
            return float(params["end_s"]) - float(params["start_s"])
        return 0.0

    def inject(self, ctx, target, params):
        raise PlantError("simulator faults are executed by the scenario runner, not injected into a live plant")

    def cleanup(self, ctx, record):
        return Check("cleanup", True, "ephemeral plant is discarded by the harness")

    def removed(self, ctx, record):
        return Check("fault_removed", True, "scripted window ended inside the replayed line time")


def _window_params(extra: tuple[Param, ...] = ()) -> tuple[Param, ...]:
    return (
        Param("start_s", "int", "line second at which the event begins", 0, 3600, required=True),
        Param("end_s", "int", "line second at which the event ends", 1, 3600, required=True),
        *extra,
    )


_SIM_COMMON = dict(
    layer="simulator",
    cleanup="none required: the scenario plays inside an ephemeral plant that the harness tears down",
    preconditions=("host resources above threshold",),
)


class SimDown(SimulatorFault):
    event_type = "down"
    spec = FaultSpec(
        name="sim_down",
        summary="Simulated machine breakdown: the station makes nothing and reports DOWN for the window.",
        params=_window_params(),
        target_keys=("plant", "station"),
        expected_effects=("station state DOWN for the window", "neighbouring stations starve then block",
                          "breakdown appears in downtime measurement"),
        recovery_criteria=("station returns to running after the window", "booking within the replay band",
                           "scripted breakdown detected"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "down", "station": target["station"], "start": int(params["start_s"]),
                "end": int(params["end_s"])}


class SimChangeover(SimulatorFault):
    event_type = "changeover"
    needs_station = False
    spec = FaultSpec(
        name="sim_changeover",
        summary="Simulated whole-line changeover: planned stop that must not be booked as downtime.",
        params=_window_params(),
        target_keys=("plant",),
        expected_effects=("all stations in CHANGEOVER for the window", "order advances at the end of the window",
                          "planned stop is not misclassified as downtime"),
        recovery_criteria=("stations return to running", "planned stop not misclassified as downtime"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "changeover", "start": int(params["start_s"]), "end": int(params["end_s"])}


class SimMicroStops(SimulatorFault):
    event_type = "micro_stops"
    windowed = False
    spec = FaultSpec(
        name="sim_micro_stops",
        summary="Simulated micro-stops: short random stops pre-rolled from the seed on one station.",
        params=(
            Param("every_min_s", "int", "minimum seconds between stops", 30, 600, 110),
            Param("every_max_s", "int", "maximum seconds between stops", 30, 600, 130),
            Param("duration_min_s", "int", "minimum stop length in seconds", 1, 60, 8),
            Param("duration_max_s", "int", "maximum stop length in seconds", 1, 60, 18),
        ),
        target_keys=("plant", "station"),
        expected_effects=("repeated short stops on the station", "OEE performance drops against baseline"),
        recovery_criteria=("station running between stops", "booking within the replay band"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "micro_stops", "station": target["station"],
                "every_s": [int(params["every_min_s"]), int(params["every_max_s"])],
                "duration_s": [int(params["duration_min_s"]), int(params["duration_max_s"])]}


class SimCounterReset(SimulatorFault):
    event_type = "counter_reset"
    windowed = False
    spec = FaultSpec(
        name="sim_counter_reset",
        summary="Simulated PLC counter reset: the station's counters snap back to zero at one instant.",
        params=(Param("at_s", "int", "line second of the reset", 1, 3600, required=True),),
        target_keys=("plant", "station"),
        expected_effects=("station counters drop to zero", "MES must re-baseline and book nothing for the reset"),
        recovery_criteria=("booking within the replay band", "no production invented by the reset"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "counter_reset", "station": target["station"], "at": int(params["at_s"])}


class SimScrapBurst(SimulatorFault):
    event_type = "scrap_burst"
    spec = FaultSpec(
        name="sim_scrap_burst",
        summary="Simulated scrap burst: the station's scrap rate is raised for the window.",
        params=_window_params((Param("scrap_pct", "float", "scrap percentage during the window", 1, 50, 10),)),
        target_keys=("plant", "station"),
        expected_effects=("scrap count rises on the station during the window", "quality component of OEE drops"),
        recovery_criteria=("scrap rate returns to baseline after the window", "booking within the replay band"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "scrap_burst", "station": target["station"], "scrap_pct": float(params["scrap_pct"]),
                "start": int(params["start_s"]), "end": int(params["end_s"])}


class SimStarve(SimulatorFault):
    event_type = "starve"
    spec = FaultSpec(
        name="sim_starve",
        summary="Simulated starvation: nothing arrives at the station although it is willing to work.",
        params=_window_params(),
        target_keys=("plant", "station"),
        expected_effects=("station and everything downstream report STARVED", "starvation is not booked as breakdown"),
        recovery_criteria=("station returns to running", "no invented breakdown"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "starve", "station": target["station"], "start": int(params["start_s"]),
                "end": int(params["end_s"])}


class SimBlock(SimulatorFault):
    event_type = "block"
    spec = FaultSpec(
        name="sim_block",
        summary="Simulated blockage: the station has nowhere to put what it has made.",
        params=_window_params(),
        target_keys=("plant", "station"),
        expected_effects=("station and everything upstream report BLOCKED", "blockage is not booked as breakdown"),
        recovery_criteria=("station returns to running", "no invented breakdown"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "block", "station": target["station"], "start": int(params["start_s"]),
                "end": int(params["end_s"])}


class SimDisconnect(SimulatorFault):
    event_type = "disconnect"
    needs_station = False
    spec = FaultSpec(
        name="sim_disconnect",
        summary=("Simulated loss of the machine layer: the replay closes its OPC UA endpoint for the window while "
                 "the line runs on. This exercises the agent's reconnect and the MES's treatment of unobserved time."),
        params=_window_params(),
        target_keys=("plant",),
        expected_effects=("agent reports machines disconnected for the window",
                          "unobserved time is reported as unknown, not as downtime",
                          "units counted while disconnected may be under-booked after reconnect"),
        recovery_criteria=("agent reconnects", "no invented breakdown inside the window"),
        **_SIM_COMMON,
    )

    def event(self, target, params):
        return {"type": "disconnect", "start": int(params["start_s"]), "end": int(params["end_s"])}


UNSUPPORTED = (
    FaultSpec(
        name="database_outage",
        layer="infrastructure",
        summary="Take the database away from the plant for a bounded time.",
        params=(),
        target_keys=("plant",),
        expected_effects=(),
        cleanup="not applicable",
        recovery_criteria=(),
        preconditions=(),
        supported=False,
        unsupported_reason=(
            "Lightweight mode uses an in-process SQLite file, not a database service. Making the file disappear or "
            "unreadable would risk the application's data and could create a stray empty database. A real outage "
            "needs the documented PostgreSQL full deployment, which requires Docker; Docker is not installed. "
            "sqlite_write_lock is the supported, reversible database fault."
        ),
    ),
    FaultSpec(
        name="database_latency",
        layer="infrastructure",
        summary="Add bounded latency to database connections.",
        params=(),
        target_keys=("plant",),
        expected_effects=(),
        cleanup="not applicable",
        recovery_criteria=(),
        preconditions=(),
        supported=False,
        unsupported_reason=(
            "SQLite is accessed in-process through a file; there is no network hop to delay. sqlite_write_lock "
            "produces write latency up to the lock duration and is the closest honest equivalent."
        ),
    ),
    FaultSpec(
        name="api_latency",
        layer="infrastructure",
        summary="Add bounded latency to API requests.",
        params=(),
        target_keys=("plant",),
        expected_effects=(),
        cleanup="not applicable",
        recovery_criteria=(),
        preconditions=(),
        supported=False,
        unsupported_reason=(
            "No safe injection point: delaying requests would need a proxy in the path, and the plant's own floor "
            "simulator talks to the API directly, so only lab-originated traffic would be affected. Host network "
            "shaping is prohibited by the safety policy. cpu_pressure and process_freeze are the supported ways to "
            "degrade API responsiveness."
        ),
    ),
    FaultSpec(
        name="memory_pressure",
        layer="infrastructure",
        summary="Consume memory on a target.",
        params=(),
        target_keys=("plant",),
        expected_effects=(),
        cleanup="not applicable",
        recovery_criteria=(),
        preconditions=(),
        supported=False,
        unsupported_reason="Disabled by safety policy (memory_pressure.enabled: false); this host has 8 GB RAM.",
    ),
)

FAULTS: dict[str, Fault] = {
    fault.spec.name: fault
    for fault in (NoFault(), ProcessOutage(), ProcessFreeze(), CpuPressure(), SqliteWriteLock(), SimDown(),
                  SimChangeover(), SimMicroStops(), SimCounterReset(), SimScrapBurst(), SimStarve(), SimBlock(),
                  SimDisconnect())
}
UNSUPPORTED_BY_NAME: dict[str, FaultSpec] = {spec.name: spec for spec in UNSUPPORTED}


def all_specs() -> list[FaultSpec]:
    return [fault.spec for fault in FAULTS.values()] + list(UNSUPPORTED)


def validate_params(spec: FaultSpec, params: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    known = {p.name: p for p in spec.params}
    for key in params:
        if key not in known:
            problems.append(f"unknown parameter {key!r} for fault {spec.name}")
    resolved: dict[str, Any] = {}
    for param in spec.params:
        if param.name in params:
            value = params[param.name]
        elif param.required:
            problems.append(f"parameter {param.name!r} is required for fault {spec.name}")
            continue
        else:
            value = param.default
        if value is None:
            continue
        if param.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"parameter {param.name!r} must be an integer")
                continue
        elif param.kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"parameter {param.name!r} must be a number")
                continue
            value = float(value)
        elif param.kind == "choice":
            if value not in param.choices:
                problems.append(f"parameter {param.name!r} must be one of {list(param.choices)}")
                continue
        if param.kind in ("int", "float"):
            if param.minimum is not None and value < param.minimum:
                problems.append(f"parameter {param.name!r}={value} is below the minimum {param.minimum:g}")
            if param.maximum is not None and value > param.maximum:
                problems.append(f"parameter {param.name!r}={value} is above the maximum {param.maximum:g}")
        resolved[param.name] = value
    return resolved, problems
