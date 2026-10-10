import json

import pytest

from chaoslab.api import LabApi
from chaoslab.store import ResultStore


@pytest.fixture
def api(lab_root):
    return LabApi(lab_root)


def definition(**overrides):
    base = {
        "name": "proposal", "category": "infrastructure",
        "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}},
    }
    base.update(overrides)
    return base


def test_fault_types_are_discoverable_with_bounds(api):
    names = {f["name"]: f for f in api.discover_fault_types()}
    assert {"process_outage", "process_freeze", "cpu_pressure", "sqlite_write_lock", "sim_down"} <= set(names)
    assert names["process_outage"]["supported"] is True
    assert names["database_outage"]["supported"] is False
    assert names["database_outage"]["unsupported_reason"]
    duration = next(p for p in names["process_outage"]["parameters"] if p["name"] == "duration_s")
    assert duration["maximum"] == 60


def test_targets_are_discoverable(api):
    targets = api.discover_targets()
    assert targets["hosts"] == ["127.0.0.1"]
    assert sorted(targets["plants"]["bottling"]["components"]) == ["api", "floor", "opc-agent", "opc-sim"]
    assert "database" in targets["resources"]
    assert {p["name"] for p in targets["workload_profiles"]} >= {"baseline", "idle"}


def test_validate_accepts_a_good_proposal_and_explains_a_bad_one(api):
    assert api.validate(definition())["allowed"] is True
    bad = api.validate(definition(fault={"type": "process_outage", "parameters": {"duration_s": 600}}))
    assert bad["allowed"] is False
    assert bad["stage"] == "policy"
    assert bad["reasons"]
    schema = api.validate({"name": "x"})
    assert schema["allowed"] is False
    assert schema["stage"] == "schema"


def test_submit_refuses_invalid_proposals_without_starting_anything(api):
    response = api.submit(definition(target={"plant": "prod", "component": "api"}))
    assert response["accepted"] is False
    assert response["stage"] == "policy"
    assert api.status()["active"] is None


def test_submit_refuses_while_another_experiment_is_active(api, settings):
    import time

    from chaoslab.audit import Lease

    Lease(settings.lease_file, settings.stop_flag).acquire("20261010T000000Z-held-aaaaaa", "held", time.time() + 100)
    response = api.submit(definition())
    assert response["accepted"] is False
    assert response["stage"] == "lease"


def test_status_result_and_compare_work_on_stored_results(api, settings):
    store = ResultStore(settings.results_dir)
    for index, success in enumerate((0.0, 0.5), start=1):
        run_id = f"20261010T00000{index}Z-proposal-00000{index}"
        store.create(run_id)
        store.write_result(run_id, {
            "experiment_id": run_id, "overall_result": "recovered",
            "definition": {"name": "proposal", "category": "infrastructure",
                           "steps": [{"target": {"plant": "bottling", "component": "api"},
                                      "fault": {"type": "process_outage", "parameters": {}}}]},
            "observed_metrics": {"fault": {"api_success_ratio": success}},
            "recovery": {"recovered": True, "duration_s": 2.0, "stages": {}},
            "cleanup": {"ok": True}, "scoring": {"applied": False}, "final_health": {},
        })
    first, second = "20261010T000001Z-proposal-000001", "20261010T000002Z-proposal-000002"
    assert api.status(first)["finished"] is True
    assert api.status("latest")["experiment_id"] == second
    assert api.recovery(second)["recovery"]["recovered"] is True
    assert api.scoring(second)["scoring"]["applied"] is False
    comparison = api.compare(first, second)
    row = next(r for r in comparison["rows"] if r["metric"] == "fault_api_success_ratio")
    assert row["delta"] == 0.5
    assert json.dumps(api.metrics(second))
    assert json.dumps(api.logs(second))


def test_unknown_experiment_is_a_clear_error(api):
    with pytest.raises(FileNotFoundError):
        api.result("does-not-exist")


def test_emergency_stop_with_nothing_active_is_safe(api):
    report = api.emergency_stop(wait_s=0.5)
    assert report["ok"] is True
    assert report["active_experiment"] is None
    assert report["forced_cleanup"] is False


def test_emergency_stop_cleans_a_dead_controllers_lease(api, settings):
    import os
    import signal
    import subprocess
    import time

    import psutil

    from chaoslab.audit import Lease

    victim = subprocess.Popen(["sleep", "60"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    try:
        os.kill(victim.pid, signal.SIGSTOP)
        time.sleep(0.2)
        lease = Lease(settings.lease_file, settings.stop_flag)
        lease.acquire("20261010T000000Z-dead-cccccc", "dead", time.time() + 600)
        lease.update(controller_pid=dead.pid, state="fault_active",
                     records=[{"type": "process_freeze", "component": "api", "pid": victim.pid}])
        report = api.emergency_stop(wait_s=0.5)
        assert report["forced_cleanup"] is True
        assert report["ok"] is True
        assert psutil.Process(victim.pid).status() != psutil.STATUS_STOPPED
        assert lease.active() is None
    finally:
        victim.kill()
        victim.wait()
