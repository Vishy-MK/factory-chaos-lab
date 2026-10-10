import copy
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import create_engine, text

from chaoslab import model
from chaoslab.adapters.factorysemantics import PLANT_NAMES_DIR, with_database_hook, with_plant_names
from chaoslab.adapters.metrics import MetricsAdapter, summarise_database
from chaoslab.api import LabApi
from chaoslab.audit import Lease
from chaoslab.chaos import cleanup as cleanup_module
from chaoslab.chaos import controller as controller_module
from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos import watchdog
from chaoslab.chaos.controller import Controller
from chaoslab.chaos.faults import FAULTS, FaultContext
from chaoslab.plant_names import chaoslab_dbhook as hook
from chaoslab.policy import PolicyValidator

LATENCY = FAULTS["database_latency"]


def definition(target=None, **params):
    return model.from_mapping({
        "name": "db", "category": "infrastructure", "baseline_s": 20,
        "target": target or {"plant": "bottling", "resource": "database"},
        "fault": {"type": "database_latency", "parameters": params},
    })


@pytest.fixture
def database(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.database_file
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"))
    engine.dispose()
    return path


@pytest.fixture
def installed(settings, database):
    installed = hook.install(str(settings.db_hook_dir), settings.plant_name, str(database), "run-api",
                             flusher=True)
    try:
        yield installed
    finally:
        hook.uninstall()


def timed(engine, statements=3) -> float:
    durations = []
    for _ in range(statements):
        started = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).fetchall()
        durations.append(time.perf_counter() - started)
    return sum(durations) / len(durations)


# -- parameters and policy -----------------------------------------------------------------------------------


def test_defaults_and_bounds():
    resolved, problems = fault_registry.validate_params(LATENCY.spec, {})
    assert not problems and resolved == {"delay_ms": 200, "duration_s": 10}
    for bad in ({"delay_ms": 0}, {"delay_ms": 1001}, {"duration_s": 0}, {"duration_s": 61}, {"delay_ms": 2.5},
                {"delay_ms": True}, {"jitter_ms": 5}):
        assert fault_registry.validate_params(LATENCY.spec, bad)[1], bad


def test_definition_file_is_allowed(settings):
    from chaoslab.settings import PACKAGE_ROOT

    loaded = model.load_file(PACKAGE_ROOT / "experiments" / "definitions" / "database-latency-200ms-10s.yaml")
    decision = PolicyValidator(settings).validate(loaded)
    assert decision.allowed, decision.reasons
    assert decision.normalised[0]["fault"]["parameters"] == {"delay_ms": 200, "duration_s": 10}
    assert loaded.baseline_s == 20


def test_policy_limits_and_switch(settings):
    validator = PolicyValidator(settings)
    assert validator.validate(definition(delay_ms=200, duration_s=10)).allowed
    assert not validator.validate(definition(delay_ms=200, duration_s=31)).allowed
    policy = copy.deepcopy(settings.policy)
    policy["database_latency"]["max_delay_ms"] = 100
    object.__setattr__(settings, "policy", policy)
    decision = PolicyValidator(settings).validate(definition(delay_ms=200))
    assert any("delay_ms exceeds policy maximum" in r for r in decision.reasons)
    policy["database_latency"]["enabled"] = False
    decision = PolicyValidator(settings).validate(definition(delay_ms=50))
    assert any("disabled by safety policy" in r for r in decision.reasons)


@pytest.mark.parametrize("target", [
    {"plant": "bottling", "resource": "/home/vishy/other.db"},
    {"plant": "bottling", "component": "api"},
    {"plant": "packaging", "resource": "database"},
    {"plant": "bottling", "resource": "database", "path": "/tmp/x.db"},
])
def test_only_the_allowlisted_database_is_a_target(settings, target):
    assert not PolicyValidator(settings).validate(definition(target=target)).allowed


# -- the hook ------------------------------------------------------------------------------------------------


def test_control_problems_reject_stale_or_foreign_activations(settings, database):
    directory, plant, db = str(settings.db_hook_dir), settings.plant_name, str(database)
    boot = hook.boot_id()
    control = hook.write_control(directory, plant, db, 200, 5)
    now = time.monotonic()
    assert hook.control_problem(control, plant, db, now, boot) is None
    assert hook.control_problem(control, "packaging", db, now, boot) == "activation names another plant"
    assert hook.control_problem(control, plant, db + "x", now, boot) == "activation names another database"
    assert hook.control_problem(control, plant, db, now, "other-boot") == "activation is from another boot"
    assert hook.control_problem(control, plant, db, now + 6, boot) == "activation expired"
    stretched = {**control, "expires_monotonic": control["activated_monotonic"] + 3600}
    assert hook.control_problem(stretched, plant, db, now, boot) == "activation window out of bounds"
    assert hook.control_problem({**control, "delay_ms": 99999}, plant, db, now, boot) == "delay out of bounds"
    assert hook.control_problem({"unreadable": True}, plant, db, now, boot) == "activation file unreadable"
    with pytest.raises(ValueError):
        hook.write_control(directory, plant, db, 5000, 5)
    with pytest.raises(ValueError):
        hook.write_control(directory, plant, db, 100, 600)


def test_hook_adds_the_configured_delay_only_to_the_plant_database(settings, database, installed, tmp_path):
    plant_engine = create_engine(f"sqlite:///{database}")
    other_engine = create_engine(f"sqlite:///{tmp_path / 'unrelated.db'}")
    directory = str(settings.db_hook_dir)
    before = timed(plant_engine)
    hook.write_control(directory, settings.plant_name, str(database), 150, 5)
    time.sleep(hook.CONTROL_POLL_S * 2)
    during = timed(plant_engine)
    unrelated = timed(other_engine)
    assert during - before >= 0.14, (before, during)
    assert unrelated < 0.05
    hook.remove_control(directory)
    time.sleep(hook.CONTROL_POLL_S * 2)
    assert timed(plant_engine) < 0.05
    installed.flush(force=True)
    stats = [s for s in hook.read_stats(directory) if s["pid"] == os.getpid()][0]
    assert stats["injected"]["statements"] >= 3
    assert 140 <= stats["injected"]["ms_total"] / stats["injected"]["statements"] <= 200
    assert stats["statements"]["read"] >= 6


def test_activation_expires_at_its_monotonic_deadline_without_cleanup(settings, database, installed):
    engine = create_engine(f"sqlite:///{database}")
    hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 100, 0.4)
    time.sleep(hook.CONTROL_POLL_S * 2)
    assert timed(engine, 1) >= 0.09
    time.sleep(0.5)
    assert timed(engine) < 0.05
    assert hook.read_control(str(settings.db_hook_dir)) is not None, "expiry must not depend on cleanup"
    installed.flush(force=True)
    stats = [s for s in hook.read_stats(str(settings.db_hook_dir)) if s["pid"] == os.getpid()][0]
    assert stats["control"]["active"] is False and stats["control"]["problem"] == "activation expired"


def test_hook_counts_errors_and_lock_errors(settings, database, installed):
    engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 0.1})
    with pytest.raises(Exception):
        with engine.connect() as conn:
            conn.execute(text("SELECT * FROM missing_table"))
    holder = create_engine(f"sqlite:///{database}").raw_connection()
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO t (v) VALUES ('x')"))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    installed.flush(force=True)
    stats = [s for s in hook.read_stats(str(settings.db_hook_dir)) if s["pid"] == os.getpid()][0]
    assert stats["errors"] >= 2 and stats["lock_errors"] >= 1


FAKE_COMPONENT = """
import json, sys, time
from sqlalchemy import create_engine, text
engine = create_engine("sqlite:///" + sys.argv[2])
durations = []
for _ in range(3):
    started = time.perf_counter()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).fetchall()
    durations.append(time.perf_counter() - started)
import chaoslab_dbhook
print(json.dumps({"hooked": chaoslab_dbhook._INSTALLED is not None, "mean_s": sum(durations) / 3}))
"""


def run_fake_component(tmp_path, settings, database, command):
    script = tmp_path / "bin" / "fsmes"
    script.parent.mkdir(exist_ok=True)
    script.write_text(FAKE_COMPONENT, encoding="utf-8")
    env = with_database_hook(with_plant_names({"PATH": os.environ["PATH"], "MES_PLANT_NAME": "bottling"}),
                             settings)
    out = subprocess.run([sys.executable, str(script), command, str(database)], env=env, capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_sitecustomize_loads_the_hook_only_into_database_clients(tmp_path, settings, database):
    env = with_database_hook({}, settings)
    assert env["CHAOSLAB_DB_HOOK_COMPONENTS"] == "run-api,run-opc-agent"
    hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 120, 20)
    try:
        api = run_fake_component(tmp_path, settings, database, "run-api")
        sim = run_fake_component(tmp_path, settings, database, "run-opc-sim")
    finally:
        hook.remove_control(str(settings.db_hook_dir))
    assert api["hooked"] is True and api["mean_s"] >= 0.11
    assert sim["hooked"] is False and sim["mean_s"] < 0.05
    assert str(PLANT_NAMES_DIR) in with_plant_names({})["PYTHONPATH"]


def test_a_process_without_the_lab_environment_is_never_hooked(tmp_path, database):
    script = tmp_path / "fsmes"
    script.write_text(FAKE_COMPONENT, encoding="utf-8")
    env = with_plant_names({"PATH": os.environ["PATH"], "MES_PLANT_NAME": "bottling"})
    out = subprocess.run([sys.executable, str(script), "run-api", str(database)], env=env, capture_output=True,
                         text=True, timeout=60, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1])["hooked"] is False


# -- fault lifecycle -----------------------------------------------------------------------------------------


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(fault_registry.time, "sleep", lambda s: None)


def test_cleanup_is_idempotent_and_handles_partial_activation(settings, stub_plant, database, quick):
    ctx = FaultContext(settings=settings, plant=stub_plant)
    intent = LATENCY.intent({"plant": "bottling", "resource": "database"}, {})
    assert LATENCY.cleanup(ctx, intent).ok
    hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 200, 10)
    first = LATENCY.cleanup(ctx, intent)
    assert first.ok and first.detail == "activation removed"
    assert hook.read_control(str(settings.db_hook_dir)) is None
    second = LATENCY.cleanup(ctx, intent)
    assert second.ok and second.detail == "no activation present"
    assert LATENCY.removed(ctx, intent).ok


def test_preconditions_require_the_hook_and_no_leftover_activation(settings, stub_plant, database):
    ctx = FaultContext(settings=settings, plant=stub_plant)
    checks = {c.name: c for c in LATENCY.preconditions(ctx, {}, {})}
    assert checks["database_present"].ok
    assert not checks["hook_loaded"].ok and "restart the plant" in checks["hook_loaded"].detail
    sleepers = {name: subprocess.Popen(["sleep", "30"]) for name in ("api", "opc-agent")}
    try:
        for name, proc in sleepers.items():
            command = settings.allowlist["plants"]["bottling"]["components"][name]["command"]
            payload = {"version": hook.VERSION, "pid": proc.pid, "plant": "bottling", "component": command}
            settings.db_hook_dir.mkdir(parents=True, exist_ok=True)
            (settings.db_hook_dir / f"stats-{proc.pid}.json").write_text(json.dumps(payload))
        stub_plant.find_components = lambda: dict(sleepers)
        checks = {c.name: c for c in LATENCY.preconditions(ctx, {}, {})}
        assert checks["hook_loaded"].ok, checks["hook_loaded"].detail
        assert checks["no_activation_present"].ok
        hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 200, 10)
        checks = {c.name: c for c in LATENCY.preconditions(ctx, {}, {})}
        assert not checks["no_activation_present"].ok
    finally:
        for proc in sleepers.values():
            proc.kill()
            proc.wait()
        hook.remove_control(str(settings.db_hook_dir))


def test_watchdog_removes_the_activation_when_the_controller_dies(lab_root, settings, database):
    hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 200, 30)
    dead = subprocess.Popen(["true"])
    dead.wait()
    lease = Lease(settings.lease_file, settings.stop_flag)
    assert lease.acquire("20261010T000000Z-db-orphan-aaaaaa", "db-orphan", time.time() + 600)
    lease.update(controller_pid=dead.pid, state="fault_active",
                 records=[{"type": "database_latency", "delay_ms": 200}])
    assert watchdog.run(lab_root, poll_s=0.1) == 0
    assert hook.read_control(str(settings.db_hook_dir)) is None
    assert json.loads(settings.lease_file.read_text())["cleaned_by"] == "watchdog"


def test_emergency_stop_sweeps_an_unrecorded_activation(lab_root, settings, database, quick):
    hook.write_control(str(settings.db_hook_dir), settings.plant_name, str(database), 200, 30)
    report = LabApi(lab_root).emergency_stop(wait_s=0.1)
    assert hook.read_control(str(settings.db_hook_dir)) is None
    assert any(a.get("action") == "remove database latency activation" for a in report["sweep"]["actions"])
    assert report["sweep"]["ok"] is True


# -- measurement and recovery reporting ----------------------------------------------------------------------


def db_sample(t, pid_rows):
    return {"t": t, "database": pid_rows}


def row(statements, injected=0, injected_ms=0.0, observed_ms=0.0, errors=0, component="run-api"):
    histogram = [0] * (len(hook.BUCKETS_MS) + 1)
    histogram[0] = statements - injected
    histogram[hook.bucket_index(200.5)] = injected
    return {"component": component, "statements_total": statements, "statements": {"read": statements},
            "errors": errors, "lock_errors": 0, "observed_ms_total": observed_ms, "db_ms_total": observed_ms -
            injected_ms, "observed_histogram": histogram, "db_histogram": [statements] + [0] * len(hook.BUCKETS_MS),
            "injected_statements": injected, "injected_ms_total": injected_ms,
            "injected_observed_ms_total": injected_ms + 0.4 * injected, "activation_active": injected > 0}


def test_database_summary_reports_deltas_and_null_when_unavailable():
    assert summarise_database([{"t": 1.0, "database": None}, {"t": 2.0, "database": None}]) is None
    samples = [db_sample(10.0, {"1": row(100, observed_ms=50.0)}),
               db_sample(20.0, {"1": row(150, injected=40, injected_ms=8000.0, observed_ms=8100.0),
                                "2": row(10, component="run-opc-agent", observed_ms=5.0)})]
    summary = summarise_database(samples)
    assert summary["statements"] == 60 and summary["statements_per_second"] == 6.0
    assert summary["injected_statements"] == 40 and summary["injected_delay_ms_mean"] == 200.0
    assert summary["delayed_statement_ms_mean"] == 200.4
    assert summary["statement_ms_mean"] == round((8050.0 + 5.0) / 60, 3)
    assert summary["per_component"]["run-opc-agent"]["statements"] == 10
    assert summary["statement_ms"]["p95_upper_bound"] == 250.0
    assert summary["activation_seen"] is True
    quiet = summarise_database([db_sample(1.0, {"1": row(5)}), db_sample(2.0, {"1": row(5)})])
    assert quiet["statements"] == 0 and quiet["statement_ms_mean"] is None and quiet["error_ratio"] is None


def test_latency_restored_stage_uses_the_documented_tolerance(settings):
    ctl = Controller(settings)
    steps = [{"type": "database_latency", "parameters": {"delay_ms": 200, "duration_s": 10}}]

    def observed(post_mean, injected_after=0):
        base = {"statement_ms_mean": 0.4, "injected_statements": 0}
        return {"baseline": {"samples": 5, "database": base},
                "fault": {"samples": 5, "database": {"statement_ms_mean": 60.0, "injected_statements": 50,
                                                     "delayed_statement_ms_mean": 200.6}},
                "post": {"samples": 5, "database": {"statement_ms_mean": post_mean,
                                                    "injected_statements": injected_after}}}

    record = {"activated_at": 100.0, "duration_s": 10}
    window = [db_sample(t, {"1": row(n, injected=n - 10, injected_ms=(n - 10) * 200.0,
                                     observed_ms=(n - 10) * 200.4 + 4.0)})
              for t, n in ((99.0, 10), (101.0, 20), (109.0, 60))]
    ok = ctl._database_latency_evidence(observed(10.0), [{**steps[0], "record": record}], {"duration_s": 3.2},
                                        window)
    assert ok["restored"]["ok"] and ok["tolerance_ms"] == 25.0
    assert ok["direct_database"]["fault_window"]["statements"] == 40
    assert ok["measured_added_statement_ms_mean"] == 200.2 and ok["time_to_recovery_s"] == 3.2
    assert not ctl._database_latency_evidence(observed(40.0), steps, None)["restored"]["ok"]
    assert not ctl._database_latency_evidence(observed(1.0, injected_after=2), steps, None)["restored"]["ok"]
    missing = ctl._database_latency_evidence({"baseline": {"samples": 0}}, steps, None)
    assert not missing["restored"]["ok"] and "unavailable" in missing["restored"]["detail"]
    assert missing["direct_database"]["fault"] is None and missing["downstream"]["fault"] is None
    assert missing["measured_added_statement_ms_mean"] is None and missing["direct_database"]["fault_window"] is None


def test_full_experiment_injects_measures_cleans_up_and_verifies(settings, stub_plant, database, installed,
                                                                 monkeypatch):
    """The real fault and hook on a real SQLite file; only the plant's HTTP side is stubbed."""
    monkeypatch.setattr(controller_module.watchdog, "spawn", lambda root, experiment_id=None: 0)
    lab = copy.deepcopy(settings.lab)
    lab["sampling"].update(interval_s=0.2, post_s=1.2)
    policy = copy.deepcopy(settings.policy)
    policy["limits"]["min_baseline_s"] = 1
    allowlist = copy.deepcopy(settings.allowlist)
    allowlist["plants"]["bottling"]["database_clients"] = ["api"]
    for name, value in (("lab", lab), ("policy", policy), ("allowlist", allowlist)):
        object.__setattr__(settings, name, value)

    class Me:
        pid = os.getpid()

    stub_plant.find_components = lambda: {"api": Me()}
    context = lambda s: FaultContext(settings=s, plant=stub_plant)  # noqa: E731
    monkeypatch.setattr(controller_module, "build_context", context)
    monkeypatch.setattr(cleanup_module, "build_context", context)
    ctl = Controller(settings)
    ctl.plant = stub_plant
    ctl.metrics = MetricsAdapter(stub_plant)

    stop = threading.Event()
    engine = create_engine(f"sqlite:///{database}")

    def traffic():
        while not stop.is_set():
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO t (v) VALUES ('tick')"))
            stop.wait(0.02)

    writer = threading.Thread(target=traffic, daemon=True)
    writer.start()
    try:
        experiment = model.from_mapping({
            "name": "db-e2e", "category": "infrastructure", "baseline_s": 1,
            "target": {"plant": "bottling", "resource": "database"},
            "fault": {"type": "database_latency", "parameters": {"delay_ms": 100, "duration_s": 1}},
            "recovery": {"timeout_s": 30},
        })
        result = ctl.run(experiment)
    finally:
        stop.set()
        writer.join(timeout=5)
    assert result["overall_result"] == "recovered", (result["reasons"], result.get("error"))
    evidence = result["database_latency"]
    fault = evidence["direct_database"]["fault"]
    assert fault["injected_statements"] > 0 and 95 <= fault["injected_delay_ms_mean"] <= 130
    assert evidence["direct_database"]["fault_window"]["injected_statements"] > 0
    assert 80 <= evidence["measured_added_statement_ms_mean"] <= 130, evidence["direct_database"]
    assert evidence["direct_database"]["post"]["injected_statements"] == 0
    assert result["recovery"]["stages"]["database_latency_restored"]["ok"]
    assert result["cleanup"]["ok"] and hook.read_control(str(settings.db_hook_dir)) is None
    assert result["observed_metrics"]["baseline"]["database"]["injected_statements"] == 0
    assert evidence["downstream"]["baseline"]["api_success_ratio"] == 1.0
