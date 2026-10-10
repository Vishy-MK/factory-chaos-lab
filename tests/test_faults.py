import os
import signal
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos.faults import FAULTS, Check, FaultContext
from chaoslab.settings import PACKAGE_ROOT

ROOT = str(PACKAGE_ROOT)


def test_every_supported_fault_has_complete_metadata():
    for fault in FAULTS.values():
        spec = fault.spec
        assert spec.summary
        assert spec.cleanup
        assert spec.expected_effects
        assert spec.recovery_criteria
        assert spec.preconditions
        assert spec.target_keys


def test_every_unsupported_fault_states_a_reason():
    assert fault_registry.UNSUPPORTED
    for spec in fault_registry.UNSUPPORTED:
        assert spec.supported is False
        assert spec.unsupported_reason and len(spec.unsupported_reason) > 40


def test_parameter_defaults_are_applied_and_bounds_enforced():
    spec = FAULTS["process_outage"].spec
    resolved, problems = fault_registry.validate_params(spec, {})
    assert not problems
    assert resolved["duration_s"] == 10
    _, problems = fault_registry.validate_params(spec, {"duration_s": 61})
    assert problems
    _, problems = fault_registry.validate_params(spec, {"signal": "STOP"})
    assert problems


def test_required_parameters_are_enforced():
    spec = FAULTS["sim_down"].spec
    _, problems = fault_registry.validate_params(spec, {"start_s": 1})
    assert any("end_s" in p for p in problems)


def test_simulator_events_follow_the_upstream_vocabulary():
    down = FAULTS["sim_down"].event({"station": "RD"}, {"start_s": 10, "end_s": 20})
    assert down == {"type": "down", "station": "RD", "start": 10, "end": 20}
    reset = FAULTS["sim_counter_reset"].event({"station": "RD"}, {"at_s": 5})
    assert reset == {"type": "counter_reset", "station": "RD", "at": 5}
    micro = FAULTS["sim_micro_stops"].event({"station": "Palletiser"}, {
        "every_min_s": 110, "every_max_s": 130, "duration_min_s": 8, "duration_max_s": 18})
    assert micro["every_s"] == [110, 130] and micro["duration_s"] == [8, 18]
    assert FAULTS["sim_changeover"].event({}, {"start_s": 1, "end_s": 2})["type"] == "changeover"
    assert FAULTS["sim_disconnect"].event({}, {"start_s": 1, "end_s": 2})["type"] == "disconnect"


def test_simulator_faults_refuse_live_injection(settings, stub_plant):
    ctx = FaultContext(settings=settings, plant=stub_plant)
    with pytest.raises(Exception, match="scenario runner"):
        FAULTS["sim_down"].inject(ctx, {"station": "RD"}, {"start_s": 1, "end_s": 2})


def test_sqlite_lock_holder_blocks_writers_and_releases(tmp_path):
    database = tmp_path / "lock.db"
    connection = sqlite3.connect(database)
    connection.execute("create table t (x integer)")
    connection.commit()
    connection.close()
    holder = subprocess.Popen(
        [sys.executable, "-m", "chaoslab.chaos.holders", "sqlite_lock", "--db", str(database), "--seconds", "3"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "LOCKED"
    other = sqlite3.connect(database, timeout=0.3, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        other.execute("BEGIN IMMEDIATE")
    other.close()
    holder.wait(timeout=10)
    assert holder.returncode == 0
    free, _ = fault_registry._lock_is_free(str(database))
    assert free
    connection = sqlite3.connect(database)
    assert connection.execute("select count(*) from t").fetchone()[0] == 0
    connection.close()


def test_sqlite_lock_holder_releases_on_sigterm(tmp_path):
    database = tmp_path / "lock.db"
    sqlite3.connect(database).execute("create table t (x integer)").connection.commit()
    holder = subprocess.Popen(
        [sys.executable, "-m", "chaoslab.chaos.holders", "sqlite_lock", "--db", str(database), "--seconds", "60"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "LOCKED"
    os.kill(holder.pid, signal.SIGTERM)
    holder.wait(timeout=10)
    free, detail = fault_registry._lock_is_free(str(database))
    assert free, detail


def test_sqlite_lock_holder_reports_failure_when_already_locked(tmp_path):
    database = tmp_path / "lock.db"
    first = sqlite3.connect(database, isolation_level=None)
    first.execute("create table t (x integer)")
    first.execute("BEGIN IMMEDIATE")
    holder = subprocess.run(
        [sys.executable, "-m", "chaoslab.chaos.holders", "sqlite_lock", "--db", str(database), "--seconds", "2"],
        cwd=ROOT, capture_output=True, text=True, timeout=20)
    first.execute("ROLLBACK")
    assert holder.returncode == 3
    assert holder.stdout.startswith("FAILED")


def test_cpu_burner_is_bounded_and_pinned():
    cpu = sorted(os.sched_getaffinity(0))[0]
    burner = subprocess.Popen(
        [sys.executable, "-m", "chaoslab.chaos.holders", "cpu_burn", "--seconds", "2", "--percent", "50",
         "--cpu", str(cpu)], cwd=ROOT)
    time.sleep(0.8)
    assert psutil.Process(burner.pid).cpu_affinity() == [cpu]
    started = time.monotonic()
    burner.wait(timeout=10)
    assert time.monotonic() - started < 5
    assert burner.returncode == 0


def test_cpu_burner_clamps_absurd_durations():
    from chaoslab.chaos import holders

    assert holders.HARD_LIMIT_S <= 120


def test_process_freeze_cleanup_resumes_the_process(settings, stub_plant):
    child = subprocess.Popen(["sleep", "60"])
    try:
        os.kill(child.pid, signal.SIGSTOP)
        time.sleep(0.2)
        assert psutil.Process(child.pid).status() == psutil.STATUS_STOPPED
        ctx = FaultContext(settings=settings, plant=stub_plant)
        record = {"type": "process_freeze", "component": "api", "pid": child.pid}
        check = FAULTS["process_freeze"].cleanup(ctx, record)
        assert check.ok, check.detail
        assert FAULTS["process_freeze"].removed(ctx, record).ok
        assert psutil.Process(child.pid).status() != psutil.STATUS_STOPPED
    finally:
        child.kill()
        child.wait()


def test_cpu_pressure_cleanup_restores_affinity_and_stops_burner(settings, stub_plant):
    cpus = sorted(os.sched_getaffinity(0))
    target = subprocess.Popen(["sleep", "60"])
    burner = subprocess.Popen([sys.executable, "-m", "chaoslab.chaos.holders", "cpu_burn", "--seconds", "60",
                               "--percent", "30", "--cpu", str(cpus[0])], cwd=ROOT)
    try:
        psutil.Process(target.pid).cpu_affinity([cpus[0]])
        ctx = FaultContext(settings=settings, plant=stub_plant)
        record = {"type": "cpu_pressure", "component": "api", "pid": target.pid, "original_affinity": cpus,
                  "burner_pid": burner.pid}
        check = FAULTS["cpu_pressure"].cleanup(ctx, record)
        assert check.ok, check.detail
        burner.wait(timeout=5)
        assert sorted(psutil.Process(target.pid).cpu_affinity()) == cpus
        assert FAULTS["cpu_pressure"].removed(ctx, record).ok
    finally:
        target.kill()
        target.wait()
        if burner.poll() is None:
            burner.kill()


def test_cleanup_tolerates_a_minimal_intent_record(settings, stub_plant):
    ctx = FaultContext(settings=settings, plant=stub_plant)
    for name in ("cpu_pressure", "sqlite_write_lock"):
        record = FAULTS[name].intent({"plant": "bottling", "component": "api"}, {})
        assert FAULTS[name].cleanup(ctx, {**record, "database": str(settings.state_dir / "none.db")}) is not None


def test_check_serialises():
    assert Check("x", True, "y").as_dict() == {"name": "x", "ok": True, "detail": "y"}
