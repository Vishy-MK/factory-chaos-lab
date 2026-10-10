import copy
import threading
import time

import pytest

from chaoslab import model
from chaoslab.adapters.metrics import MetricsAdapter
from chaoslab.audit import Lease
from chaoslab.chaos import controller as controller_module
from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos.controller import Controller
from chaoslab.chaos.faults import Check, Fault


class RecordingFault(Fault):
    spec = fault_registry.ProcessFreeze.spec

    def __init__(self, cleanup_ok=True, hold=1.0):
        self.cleanup_ok = cleanup_ok
        self.hold = hold
        self.injected = []
        self.cleaned = []

    def hold_seconds(self, params):
        return self.hold

    def preconditions(self, ctx, target, params):
        return []

    def inject(self, ctx, target, params):
        self.injected.append(target)
        return {"type": "process_freeze", "component": target["component"], "pid": 1}

    def cleanup(self, ctx, record):
        self.cleaned.append(record)
        return Check("cleanup", self.cleanup_ok, "fake cleanup" if self.cleanup_ok else "fake cleanup failed")

    def removed(self, ctx, record):
        return Check("fault_removed", self.cleanup_ok, "fake removal")


@pytest.fixture
def controller(settings, stub_plant, monkeypatch):
    monkeypatch.setattr(controller_module.watchdog, "spawn", lambda root, experiment_id=None: 0)
    lab = copy.deepcopy(settings.lab)
    lab["sampling"]["interval_s"] = 0.2
    lab["sampling"]["post_s"] = 0.3
    policy = copy.deepcopy(settings.policy)
    policy["limits"]["min_baseline_s"] = 1
    object.__setattr__(settings, "lab", lab)
    object.__setattr__(settings, "policy", policy)
    ctl = Controller(settings)
    ctl.plant = stub_plant
    ctl.metrics = MetricsAdapter(stub_plant)
    return ctl


def experiment(name="t", fault="process_freeze", component="api", params=None, **extra):
    definition = {
        "name": name, "category": "infrastructure", "baseline_s": 1,
        "target": {"plant": "bottling", "component": component},
        "fault": {"type": fault, "parameters": params if params is not None else {"duration_s": 1}},
        "recovery": {"timeout_s": 30},
    }
    definition.update(extra)
    return model.from_mapping(definition)


def test_no_fault_experiment_completes_and_result_is_persisted(controller):
    result = controller.run(experiment("baseline", fault="none", params={"duration_s": 1},
                                       target={"plant": "bottling"}))
    assert result["overall_result"] == "recovered"
    assert result["fault_injection"]["injected"] is False
    stored = controller.store.read_result(result["experiment_id"])
    assert stored["experiment_id"] == result["experiment_id"]
    assert stored["application"]["deployment_mode"].startswith("lightweight")
    assert (controller.store.directory(result["experiment_id"]) / "samples.jsonl").is_file()


def test_result_contains_required_sections(controller):
    result = controller.run(experiment("sections", fault="none", params={"duration_s": 1},
                                       target={"plant": "bottling"}))
    for key in ("experiment_id", "started_at", "application", "initial_health", "initial_resources",
                "fault_definition", "workload", "fault_injection", "observed_metrics", "production_measurements",
                "scoring", "cleanup", "recovery", "final_health", "overall_result", "limitations"):
        assert key in result, key
    assert result["scoring"]["applied"] is False


def test_invalid_target_is_rejected_and_nothing_is_injected(controller, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("bad-target", component="postgres"))
    assert result["overall_result"] == "rejected"
    assert any("postgres" in r for r in result["reasons"])
    assert fake.injected == []


def test_excessive_duration_is_rejected(controller, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("too-long", params={"duration_s": 3600}))
    assert result["overall_result"] == "rejected"
    assert fake.injected == []


def test_rejected_result_is_persisted(controller):
    result = controller.run(experiment("persist-reject", component="postgres"))
    assert controller.store.read_result(result["experiment_id"])["overall_result"] == "rejected"


def test_failed_cleanup_is_reported_not_ignored(controller, monkeypatch):
    fake = RecordingFault(cleanup_ok=False)
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("cleanup-fails"))
    assert fake.injected, "the fault should have been injected"
    assert result["overall_result"] == "unsafe_cleanup_failed"
    assert result["cleanup"]["ok"] is False
    assert result["recovery"]["recovered"] is False
    assert controller.store.read_result(result["experiment_id"])["overall_result"] == "unsafe_cleanup_failed"


def test_successful_fault_cleanup_runs_after_injection(controller, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("clean"))
    assert result["overall_result"] == "recovered"
    assert len(fake.injected) == 1
    assert len(fake.cleaned) == 1
    assert result["fault_injection"]["injected"] is True
    assert result["cleanup"]["ok"] is True


def test_cleanup_runs_even_when_injection_raises(controller, monkeypatch):
    class Exploding(RecordingFault):
        def inject(self, ctx, target, params):
            raise RuntimeError("boom")

    fake = Exploding()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("explodes"))
    assert result["overall_result"] == "injection_failed"
    assert len(fake.cleaned) == 1
    assert result["fault_injection"]["injected"] is False


def test_emergency_stop_aborts_and_cleans_up(controller, monkeypatch, settings):
    fake = RecordingFault(hold=30.0)
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    outcome = {}

    def runner():
        outcome["result"] = controller.run(experiment("stop-me", params={"duration_s": 30}))

    thread = threading.Thread(target=runner)
    started = time.monotonic()
    thread.start()
    lease = Lease(settings.lease_file, settings.stop_flag)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = lease.read()
        if current and current.get("state") == "fault_active":
            break
        time.sleep(0.05)
    assert lease.read()["state"] == "fault_active"
    lease.request_stop()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert time.monotonic() - started < 25
    assert outcome["result"]["overall_result"] == "aborted"
    assert len(fake.cleaned) == 1


def test_second_experiment_is_refused_while_another_holds_the_lease(controller, settings):
    lease = Lease(settings.lease_file, settings.stop_flag)
    assert lease.acquire("20261010T000000Z-other-abcdef", "other", time.time() + 100)
    result = controller.run(experiment("blocked", fault="none", params={"duration_s": 1},
                                       target={"plant": "bottling"}))
    assert result["overall_result"] == "rejected_preconditions"
    assert any("already active" in r or "another experiment" in r for r in result["reasons"])


def test_low_memory_refuses_experiments(controller, settings):
    policy = copy.deepcopy(settings.policy)
    policy["resources"]["min_available_memory_mb"] = 10 ** 9
    object.__setattr__(settings, "policy", policy)
    ctl = Controller(settings)
    ctl.plant = controller.plant
    result = ctl.run(experiment("no-memory", fault="none", params={"duration_s": 1}, target={"plant": "bottling"}))
    assert result["overall_result"] == "rejected_preconditions"
    assert any("available memory" in r for r in result["reasons"])


def test_dry_run_executes_nothing(controller, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("dry"), dry_run=True)
    assert result["overall_result"] == "dry_run_ok"
    assert fake.injected == []


def test_unhealthy_plant_blocks_infrastructure_experiments(controller, stub_plant, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    stub_plant.state.api_up = False
    result = controller.run(experiment("unhealthy"))
    assert result["overall_result"] == "rejected_preconditions"
    assert fake.injected == []


def test_audit_log_records_the_lifecycle(controller, monkeypatch):
    fake = RecordingFault()
    monkeypatch.setitem(fault_registry.FAULTS, "process_freeze", fake)
    result = controller.run(experiment("audited"))
    events = [e["event"] for e in controller.audit.read(result["experiment_id"])]
    for expected in ("submitted", "state", "fault_injected", "cleanup", "finished"):
        assert expected in events
