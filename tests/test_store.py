import csv
import json

import pytest

from chaoslab.store import CSV_COLUMNS, ResultStore, compare, flatten, new_run_id


def sample_result(run_id, overall="recovered", fault="process_outage", success_during=0.0):
    return {
        "experiment_id": run_id,
        "started_at": "2026-10-10T00:00:00Z",
        "application": {"version": "0.2.0"},
        "definition": {"name": "x", "category": "infrastructure",
                       "steps": [{"target": {"plant": "bottling", "component": "api"},
                                  "fault": {"type": fault, "parameters": {}}}]},
        "fault_injection": {"injected": True},
        "observed_metrics": {
            "baseline": {"api_success_ratio": 1.0, "api_latency_ms": {"p95": 12.0}, "tag_ingest_per_second": 14.0},
            "fault": {"api_success_ratio": success_during, "api_latency_ms": None, "tag_ingest_per_second": None,
                      "machines_connected_min": None},
            "post": {"api_success_ratio": 1.0, "api_latency_ms": {"p95": 13.0}, "tag_ingest_per_second": 14.1},
        },
        "cleanup": {"ok": True},
        "recovery": {"recovered": True, "duration_s": 1.9},
        "overall_result": overall,
    }


def test_run_ids_are_safe_and_unique():
    a, b = new_run_id("example"), new_run_id("example")
    assert a != b
    assert "/" not in a


def test_path_traversal_ids_are_refused(tmp_path):
    store = ResultStore(tmp_path)
    for bad in ("../x", "..", "a/b", "20261010T000000Z-x-zzzzzz"):
        with pytest.raises(ValueError):
            store.directory(bad)


def test_results_round_trip_and_are_never_overwritten(tmp_path):
    store = ResultStore(tmp_path)
    run_id = new_run_id("example")
    store.create(run_id)
    store.write_result(run_id, sample_result(run_id))
    assert store.read_result(run_id)["experiment_id"] == run_id
    with pytest.raises(FileExistsError):
        store.create(run_id)


def test_latest_and_resolve(tmp_path):
    store = ResultStore(tmp_path)
    ids = ["20261010T000001Z-a-000001", "20261010T000002Z-b-000002"]
    for run_id in ids:
        store.create(run_id)
        store.write_result(run_id, sample_result(run_id))
    assert store.latest() == ids[1]
    assert store.resolve("latest") == ids[1]
    assert store.resolve("-a-") == ids[0]
    with pytest.raises(FileNotFoundError):
        store.resolve("nothing-like-this")


def test_csv_marks_unavailable_not_zero(tmp_path):
    store = ResultStore(tmp_path)
    run_id = "20261010T000001Z-a-000001"
    store.create(run_id)
    store.write_result(run_id, sample_result(run_id))
    destination = tmp_path / "out.csv"
    assert store.export_csv(destination) == 1
    rows = list(csv.DictReader(destination.open()))
    assert rows[0]["fault_api_p95_ms"] == "unavailable"
    assert rows[0]["fault_tag_ingest_per_s"] == "unavailable"
    assert rows[0]["fault_api_success_ratio"] == "0.0"
    assert list(rows[0].keys()) == CSV_COLUMNS


def test_flatten_extracts_fault_and_target():
    flat = flatten(sample_result("20261010T000001Z-a-000001"))
    assert flat["fault_type"] == "process_outage"
    assert flat["target"] == "plant=bottling,component=api"
    assert flat["recovery_duration_s"] == 1.9


def test_compare_reports_deltas_and_null_for_unavailable():
    first = sample_result("20261010T000001Z-a-000001", success_during=0.0)
    second = sample_result("20261010T000002Z-a-000002", success_during=0.4)
    comparison = compare(first, second)
    rows = {r["metric"]: r for r in comparison["rows"]}
    assert rows["fault_api_success_ratio"]["delta"] == 0.4
    assert rows["fault_api_p95_ms"]["delta"] is None
    assert json.dumps(comparison)
