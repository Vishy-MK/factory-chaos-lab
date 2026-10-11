"""SYNTHETIC fixtures for the failure analysis tests. Generated numbers, not measurements from a real plant."""

from __future__ import annotations

import json
from datetime import datetime, timezone

T0 = 1_790_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def raw_sample(t, phase, up=True, latency=8.0, auth=True, connected=6, tag=1000, floor=5.0, error=None,
               database=None, cgroups=None, components=None, cpu=10.0):
    return {"t": t, "phase": phase,
            "api": {"up": up, "latency_ms": latency if up else None, "status": 200 if up else None, "error": error},
            "machines": {"total": 6, "connected": connected, "disconnected": 6 - connected, "unknown": 0},
            "floor_seconds_since_check": floor, "authenticated_ok": auth if up else None,
            "work_orders": {"good_qty": 1.0, "scrap_qty": 0.0, "running": 1}, "equipment_states": None,
            "tag_values_max_id": tag, "audit_entries_max_id": 1, "database": database, "cgroups": cgroups,
            "components": components or {n: {"alive": True, "status": "sleeping", "rss_mb": 50.0, "cpu_percent": 1.0}
                                         for n in ("api", "opc-sim", "opc-agent", "floor")},
            "host": {"available_memory_mb": 4000.0, "cpu_percent": cpu}}


def write_run(settings, run_id: str, samples: list[dict], fault_type="process_outage", component="api",
              params=None, injected=True, hold=10.0, fault_start=None, commit="abc123", overall="recovered",
              integrity_ok=True, removal_ok=True, extra: dict | None = None):
    directory = settings.results_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "samples.jsonl").write_text("\n".join(json.dumps(s) for s in samples) + "\n", encoding="utf-8")
    params = params if params is not None else {"duration_s": int(hold)}
    target = {"plant": "bottling", "component": component} if component else {"plant": "bottling"}
    steps = []
    if fault_type != "none":
        step = {"index": 1, "type": fault_type, "target": target, "parameters": params, "hold_s": hold,
                "injected": injected, "record": {"type": fault_type}, "cleanup_ok": removal_ok}
        if fault_start is not None:
            step["injection_started_at"] = iso(fault_start - 0.1)
            step["injection_finished_at"] = iso(fault_start)
            step["cleanup_finished_at"] = iso(fault_start + hold + 1)
        steps.append(step)
    result = {
        "experiment_id": run_id, "lab_version": "test",
        "application": {"name": "FactorySemantics MES", "version": "0.2.0", "commit": commit,
                        "deployment_mode": "lightweight", "plant": "bottling"},
        "definition": {"name": run_id.split("-", 1)[1], "category": "infrastructure", "workload": "baseline",
                       "baseline_s": 20, "steps": [{"target": target, "fault": {"type": fault_type,
                                                                                "parameters": params}}]},
        "fault_injection": {"injected": injected and fault_type != "none", "steps": steps},
        "initial_resources": {"database_integrity": {"ok": True, "detail": "ok"}},
        "cleanup": {"ok": removal_ok, "steps": [{"type": fault_type, "ok": removal_ok,
                                                 "removed": {"name": "fault_removed", "ok": removal_ok,
                                                             "detail": "synthetic removal check"}}]
                    if fault_type != "none" else []},
        "recovery": {"recovered": overall == "recovered", "duration_s": 1.0,
                     "stages": {"data_consistency": {"ok": integrity_ok, "detail": "synthetic quick_check"}}},
        "overall_result": overall, "reasons": [],
        **(extra or {}),
    }
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return directory


def outage_run(settings, run_id="20261010T000000Z-api-outage-synthetic-aaaaaa", recover_after=4, never=False,
               **kwargs):
    """20 s healthy baseline, 10 s API outage, then recovery `recover_after` samples after the fault window."""
    samples, tag = [], 1000
    for i in range(20):
        tag += 15
        samples.append(raw_sample(T0 + i, "baseline", tag=tag))
    fault_start = T0 + 20
    for i in range(10):
        samples.append(raw_sample(fault_start + i + 0.5, "fault", up=False, error="URLError", tag=None,
                                  components={"api": {"alive": False}, "opc-sim": {"alive": True},
                                              "opc-agent": {"alive": True}, "floor": {"alive": True}}))
    for i in range(20):
        t = fault_start + 10 + i + 0.5
        healthy = not never and i >= recover_after
        tag += 15 if healthy else 0
        samples.append(raw_sample(t, "recovery" if i < 8 else "post", up=healthy, error=None if healthy else
                                  "URLError", tag=tag if healthy else None))
    return write_run(settings, run_id, samples, fault_start=fault_start, hold=10.0,
                     overall="not_recovered" if never else "recovered", **kwargs)
