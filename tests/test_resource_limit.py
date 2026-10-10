import copy
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from chaoslab import model
from chaoslab.adapters.metrics import summarise_cgroups
from chaoslab.api import LabApi
from chaoslab.chaos import cgroups
from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos.controller import Controller
from chaoslab.chaos.faults import FAULTS, FaultContext
from chaoslab.policy import PolicyValidator
from chaoslab.report import render
from chaoslab.settings import PACKAGE_ROOT

LIMIT = FAULTS["resource_limit"]
SCOPES_OK, SCOPES_DETAIL = cgroups.available()
needs_scopes = pytest.mark.skipif(not SCOPES_OK, reason=f"user cgroup scopes unavailable: {SCOPES_DETAIL}")


def definition(component="api", **params):
    return model.from_mapping({
        "name": "limit", "category": "infrastructure", "baseline_s": 20,
        "target": {"plant": "bottling", "component": component},
        "fault": {"type": "resource_limit", "parameters": params},
    })


# -- parameters and policy -----------------------------------------------------------------------------------


def test_parameter_bounds():
    resolved, problems = fault_registry.validate_params(LIMIT.spec, {"cpu_percent": 20})
    assert not problems and resolved == {"duration_s": 15, "cpu_percent": 20}
    for bad in ({"cpu_percent": 1}, {"cpu_percent": 500}, {"memory_max_mb": 8}, {"duration_s": 61},
                {"memory_high_mb": "64"}, {"swap_mb": 10}):
        assert fault_registry.validate_params(LIMIT.spec, bad)[1], bad


def test_policy_requires_a_limit_and_enforces_minimums(settings):
    validator = PolicyValidator(settings)
    assert validator.validate(definition(cpu_percent=20)).allowed
    assert validator.validate(definition("opc-agent", memory_high_mb=80, memory_max_mb=120)).allowed
    cases = {
        "needs memory_high_mb": definition(),
        "below policy minimum": definition(memory_max_mb=40),
        "must not exceed memory_max_mb": definition(memory_high_mb=200, memory_max_mb=100),
        "duration exceeds": definition(cpu_percent=20, duration_s=45),
        "not an allowlisted component": definition("postgres", cpu_percent=20),
    }
    for expected, experiment in cases.items():
        decision = validator.validate(experiment)
        assert not decision.allowed and any(expected in r for r in decision.reasons), (expected, decision.reasons)
    policy = copy.deepcopy(settings.policy)
    policy["resource_limit"]["enabled"] = False
    object.__setattr__(settings, "policy", policy)
    assert any("disabled by safety policy" in r
               for r in PolicyValidator(settings).validate(definition(cpu_percent=20)).reasons)


def test_stored_definitions_are_allowed(settings):
    for name in ("api-cpu-limit-20pct-15s", "api-memory-high-64mb-15s"):
        loaded = model.load_file(PACKAGE_ROOT / "experiments" / "definitions" / f"{name}.yaml")
        decision = PolicyValidator(settings).validate(loaded)
        assert decision.allowed, (name, decision.reasons)


# -- the cgroup module ---------------------------------------------------------------------------------------


def test_only_lab_scope_names_map_to_paths():
    assert cgroups.unit_name("bottling", "opc-agent") == "chaoslab-bottling-opc-agent.scope"
    for plant, component in (("bottling", "../etc"), ("Bottling", "api"), ("bottling", "api/x")):
        with pytest.raises(cgroups.CgroupError):
            cgroups.unit_name(plant, component)
    for unit in ("session-3.scope", "chaoslab-bottling-api.service", "../chaoslab-x-y.scope", "user.slice"):
        with pytest.raises(cgroups.CgroupError):
            cgroups.scope_dir(unit)
    assert cgroups.scope_dir("chaoslab-bottling-api.scope").parent == cgroups.app_slice()
    assert cgroups.process_unit(os.getpid()) is None


def test_limit_values_are_kernel_units():
    assert cgroups.limit_values(None, None, 25) == {"cpu.max": "25000 100000"}
    assert cgroups.limit_values(64, 128, None) == {"memory.high": str(64 * 1048576),
                                                   "memory.max": str(128 * 1048576), "memory.swap.max": "0"}
    assert cgroups.limit_values(None, None, None) == {}


@pytest.fixture
def scoped_sleeper():
    unit = f"chaoslab-pytest-{uuid.uuid4().hex[:8]}.scope"
    proc = subprocess.Popen(cgroups.scope_command(unit, ["sleep", "60"]))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and cgroups.process_unit(proc.pid) != unit:
        time.sleep(0.05)
    try:
        yield unit, proc
    finally:
        proc.kill()
        proc.wait()


@needs_scopes
def test_limits_are_written_read_and_lifted_on_a_real_scope(scoped_sleeper):
    unit, proc = scoped_sleeper
    assert cgroups.process_unit(proc.pid) == unit
    original = cgroups.read_limits(unit)
    assert cgroups.is_unlimited(original)
    cgroups.write_limits(unit, cgroups.limit_values(64, 96, 15))
    limits = cgroups.read_limits(unit)
    assert limits == {"memory.high": str(64 * 1048576), "memory.max": str(96 * 1048576), "memory.swap.max": "0",
                      "cpu.max": "15000 100000"}
    stats = cgroups.stats(unit)
    assert stats["memory_current"] > 0 and stats["cpu_nr_periods"] is not None
    assert cgroups.lift(unit, original)
    assert cgroups.is_unlimited(cgroups.read_limits(unit))


@needs_scopes
def test_reset_helper_lifts_the_limits_at_its_deadline_and_on_sigterm(scoped_sleeper):
    unit, _ = scoped_sleeper
    for stop_early in (False, True):
        cgroups.write_limits(unit, cgroups.limit_values(None, None, 10))
        helper = subprocess.Popen([sys.executable, "-m", "chaoslab.chaos.holders", "resource_reset", "--unit", unit,
                                   "--seconds", "1" if not stop_early else "60"], cwd=str(PACKAGE_ROOT))
        if stop_early:
            time.sleep(0.5)
            helper.terminate()
        helper.wait(timeout=10)
        assert cgroups.is_unlimited(cgroups.read_limits(unit)), stop_early


def test_reset_helper_refuses_a_foreign_unit():
    out = subprocess.run([sys.executable, "-m", "chaoslab.chaos.holders", "resource_reset", "--unit",
                          "session-3.scope", "--seconds", "1"], cwd=str(PACKAGE_ROOT), capture_output=True)
    assert out.returncode != 0


# -- fault lifecycle -----------------------------------------------------------------------------------------


class Proc:
    def __init__(self, pid):
        self.pid = pid


@needs_scopes
def test_inject_cleanup_and_removal_on_a_real_scope(settings, stub_plant, monkeypatch):
    unit = cgroups.unit_name("bottling", "floor")
    proc = subprocess.Popen(cgroups.scope_command(unit, ["sleep", "60"]))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and cgroups.process_unit(proc.pid) != unit:
            time.sleep(0.05)
        stub_plant.find_components = lambda: {"floor": Proc(proc.pid)}
        ctx = FaultContext(settings=settings, plant=stub_plant)
        prepared = LIMIT.prepare(ctx, {"component": "floor"}, {})
        assert prepared.ok and "already runs" in prepared.detail
        record = LIMIT.inject(ctx, {"plant": "bottling", "component": "floor"}, {"cpu_percent": 10,
                                                                                 "duration_s": 30})
        assert record["unit"] == unit and cgroups.read_limits(unit)["cpu.max"] == "10000 100000"
        assert record["original"]["cpu.max"] == "max 100000"
        check = LIMIT.cleanup(ctx, record)
        assert check.ok, check.detail
        assert LIMIT.removed(ctx, record).ok
        assert LIMIT.cleanup(ctx, record).ok, "cleanup must be repeatable"
        assert LIMIT.cleanup(ctx, {"type": "resource_limit", "component": "floor"}).ok, "intent-only record"
    finally:
        proc.kill()
        proc.wait()


def test_cleanup_restarts_a_component_the_kernel_killed(settings, stub_plant):
    spawned = []

    def spawn(component, scope_unit=None):
        spawned.append((component, scope_unit))
        stub_plant.find_components = lambda: {"api": Proc(os.getpid())}
        return 4242

    stub_plant.find_components = lambda: {}
    stub_plant.spawn_component = spawn
    ctx = FaultContext(settings=settings, plant=stub_plant)
    check = LIMIT.cleanup(ctx, {"type": "resource_limit", "component": "api"})
    assert check.ok, check.detail
    assert spawned == [("api", "chaoslab-bottling-api.scope")]
    assert "restarted" in check.detail


def test_cleanup_refuses_an_unknown_component(settings, stub_plant):
    ctx = FaultContext(settings=settings, plant=stub_plant)
    assert not LIMIT.cleanup(ctx, {"type": "resource_limit", "component": "../../x"}).ok


def test_inject_refuses_a_component_outside_its_scope(settings, stub_plant):
    stub_plant.find_components = lambda: {"api": Proc(os.getpid())}
    ctx = FaultContext(settings=settings, plant=stub_plant)
    with pytest.raises(Exception, match="not in its lab scope"):
        LIMIT.inject(ctx, {"plant": "bottling", "component": "api"}, {"cpu_percent": 20})


@needs_scopes
def test_emergency_stop_lifts_limits_left_on_any_lab_scope(lab_root, scoped_sleeper, monkeypatch):
    unit, _ = scoped_sleeper
    cgroups.write_limits(unit, cgroups.limit_values(None, None, 10))
    report = LabApi(lab_root).emergency_stop(wait_s=0.1)
    assert cgroups.is_unlimited(cgroups.read_limits(unit))
    assert any(a.get("unit") == unit for a in report["sweep"]["actions"])


# -- measurement, evidence and report ------------------------------------------------------------------------


def cg(t, usage, periods, throttled, memory, high=0, oom=0, cpu_max="max 100000"):
    return {"t": t, "cgroups": {"api": {
        "unit": "chaoslab-bottling-api.scope", "limits": {"memory.high": "max", "memory.max": "max",
                                                          "memory.swap.max": "max", "cpu.max": cpu_max},
        "memory_current": memory * 1048576, "memory_peak": memory * 1048576,
        "memory_events": {"high": high, "max": 0, "oom": oom, "oom_kill": oom},
        "cpu_usage_us": usage, "cpu_nr_periods": periods, "cpu_nr_throttled": throttled,
        "cpu_throttled_us": throttled * 1000, "memory_pressure": {"some": {"avg10": 0.0, "total_us": 0}},
        "cpu_pressure": {"some": {"avg10": 0.0, "total_us": throttled * 500}}}}}


def test_cgroup_summary_computes_rates_and_handles_a_restarted_scope():
    assert summarise_cgroups([{"t": 1.0, "cgroups": None}]) is None
    summary = summarise_cgroups([cg(0.0, 0, 0, 0, 90), cg(10.0, 2_000_000, 100, 80, 100)])["api"]
    assert summary["cpu_percent_mean"] == 20.0 and summary["cpu_throttled_ratio"] == 0.8
    assert summary["memory_current_mb_max"] == 100.0 and summary["cpu_stall_ms"] == 40.0
    restarted = summarise_cgroups([cg(0.0, 5_000_000, 500, 10, 90, oom=1), cg(10.0, 1_000_000, 100, 0, 60)])["api"]
    assert restarted["cpu_percent_mean"] == 10.0 and restarted["oom_kills"] == 0


def test_lifted_stage_and_report(settings):
    ctl = Controller(settings)
    steps = [{"type": "resource_limit", "target": {"plant": "bottling", "component": "api"},
              "parameters": {"cpu_percent": 20, "duration_s": 15},
              "record": {"applied_at": 100.0, "duration_s": 15, "unit": "chaoslab-bottling-api.scope",
                         "applied": {"cpu.max": "20000 100000"}}}]
    observed = {
        "baseline": {"samples": 2, "cgroups": summarise_cgroups([cg(0, 0, 0, 0, 90), cg(10, 800_000, 100, 0, 90)])},
        "fault": {"samples": 2, "cgroups": summarise_cgroups([cg(100, 0, 0, 0, 90),
                                                              cg(110, 2_000_000, 100, 90, 95, cpu_max="20000 100000")])},
        "post": {"samples": 2, "cgroups": summarise_cgroups([cg(200, 0, 0, 0, 90), cg(210, 800_000, 100, 0, 90)])},
    }
    samples = [cg(t, 200_000 * i, 10 * i, 9 * i, 95) for i, t in enumerate((101.0, 105.0, 114.0))]
    evidence = ctl._resource_limit_evidence(observed, steps, {"duration_s": 1.5}, samples)
    assert evidence["lifted"]["ok"] and evidence["time_to_recovery_s"] == 1.5
    assert evidence["cgroup"]["limit_window"]["cpu_throttled_ratio"] == 0.9
    still = {**observed, "post": {"samples": 2, "cgroups": summarise_cgroups(
        [cg(200, 0, 0, 0, 90), cg(210, 800_000, 100, 30, 90, cpu_max="20000 100000")])}}
    assert not ctl._resource_limit_evidence(still, steps, None)["lifted"]["ok"]
    assert not ctl._resource_limit_evidence({}, steps, None)["lifted"]["ok"]
    result = {"experiment_id": "x", "overall_result": "recovered", "resource_limit": evidence,
              "definition": {"name": "limit", "category": "infrastructure", "baseline_s": 20, "workload": "baseline",
                             "steps": [{"target": steps[0]["target"],
                                        "fault": {"type": "resource_limit", "parameters": steps[0]["parameters"]}}]},
              "recovery": {"recovered": True, "duration_s": 1.5, "stages": {}}}
    text = render(result)
    assert "cpu_percent      20" in text and "throttled ratio" in text and "chaoslab-bottling-api.scope" in text
    assert "n/a" in text
    json.dumps(evidence)
