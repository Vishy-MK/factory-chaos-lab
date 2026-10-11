"""Compatibility of a baseline with an experiment, and per-metric comparisons across windows. Deterministic."""

from __future__ import annotations

from typing import Any

RATIO_METRICS = ("api_success_ratio", "authenticated_success_ratio", "machines_all_connected_ratio",
                 "telemetry_stale_ratio", "target_reachable_ratio")
#: metric -> True when a higher value is better
DIRECTION = {
    "api_success_ratio": True, "authenticated_success_ratio": True, "machines_all_connected_ratio": True,
    "target_reachable_ratio": True, "telemetry_stale_ratio": False, "api_errors": False, "api_timeouts": False,
    "machines_connected_min": True, "tag_ingest_per_second": True, "tag_ingest_stall_max_s": False,
    "telemetry_age_max_s": False, "floor_age_max_s": False, "db_statements_per_second": True,
    "db_statement_ms_mean": False, "db_driver_ms_mean": False, "db_errors": False, "db_lock_errors": False,
    "db_statement_ms_p95_bound": False, "production_counter_resets": False, "tag_id_resets": False,
    "host_cpu_percent_max": False, "host_available_mb_min": True,
}
SCALAR_METRICS = tuple(m for m in DIRECTION if m not in RATIO_METRICS)
PERCENTILES = ("p50", "p95", "p99", "max")


def compatibility(baseline_identity: dict, experiment_identity: dict, same_source: bool) -> dict:
    """Whether a baseline can stand for normal operation of the experiment's system. Mismatches qualify."""
    if same_source:
        return {"compatible": True, "checks": [{"name": "same_run", "ok": True,
                                                "detail": "baseline window of the same run: identical conditions"}],
                "qualifications": []}
    checks = []

    def check(name, key, required=True):
        a, b = baseline_identity.get(key), experiment_identity.get(key)
        if a is None or b is None:
            checks.append({"name": name, "ok": None, "detail": f"{key} not recorded (baseline {a!r}, run {b!r})"})
        else:
            checks.append({"name": name, "ok": a == b, "required": required,
                           "detail": f"baseline {a!r} vs run {b!r}"})

    check("measurement_method", "measurement_method")
    check("plant", "plant")
    check("application_commit", "application_commit")
    check("application_version", "application_version")
    check("deployment_mode", "deployment_mode")
    check("workload", "workload", required=False)
    check("replay_speed", "replay_speed", required=False)
    a, b = baseline_identity.get("sampling_interval_s"), experiment_identity.get("sampling_interval_s")
    if a and b:
        checks.append({"name": "sampling_interval", "ok": abs(a - b) / max(a, b) <= 0.25, "required": False,
                       "detail": f"baseline {a}s vs run {b}s"})
    failed_required = [c for c in checks if c["ok"] is False and c.get("required", True)]
    qualifications = [c["detail"] for c in checks if c["ok"] is False and not c.get("required", True)]
    qualifications += [c["detail"] for c in checks if c["ok"] is None]
    return {"compatible": not failed_required, "checks": checks, "qualifications": qualifications,
            "incompatible_because": [c["name"] for c in failed_required]}


def _value(metric: dict | None) -> Any:
    return None if metric is None else metric.get("value")


def _change(base, other, higher_is_better: bool | None, ratio: bool) -> dict:
    if base is None or other is None:
        return {"available": False, "reason": "missing in " + ("baseline" if base is None else "this window")}
    change: dict[str, Any] = {"available": True, "baseline": base, "value": other,
                              "absolute_change": round(other - base, 4)}
    if ratio:
        change["percentage_points"] = round((other - base) * 100, 2)
    elif base != 0:
        change["relative_change_pct"] = round((other - base) / abs(base) * 100, 1)
    else:
        change["relative_change_pct"] = None
        change["note"] = "baseline is zero: no relative change"
    if higher_is_better is not None and other != base:
        change["direction"] = "better" if (other > base) == higher_is_better else "worse"
    else:
        change["direction"] = "unchanged" if other == base else None
    return change


def compare_windows(baseline: dict, window: dict, min_samples: int) -> dict:
    """Matching metrics only: a metric missing on either side is reported unavailable, not compared."""
    result: dict[str, Any] = {}
    qualified = []
    if baseline["window"]["samples"] < min_samples:
        qualified.append(f"baseline has {baseline['window']['samples']} samples (< {min_samples})")
    if window["window"]["samples"] < min_samples:
        qualified.append(f"window has {window['window']['samples']} samples (< {min_samples})")
    if window["window"].get("truncated"):
        qualified.append(window["window"]["truncated"])
    for name in RATIO_METRICS:
        result[name] = _change(_value(baseline["metrics"].get(name)), _value(window["metrics"].get(name)),
                               DIRECTION.get(name), ratio=True)
    for name in SCALAR_METRICS:
        result[name] = _change(_value(baseline["metrics"].get(name)), _value(window["metrics"].get(name)),
                               DIRECTION.get(name), ratio=False)
    base_latency, latency = baseline["metrics"].get("api_latency_ms"), window["metrics"].get("api_latency_ms")
    if base_latency and latency:
        result["api_latency_ms"] = {
            "available": True, "method": "nearest-rank percentile of each window; never averaged across windows",
            "samples": {"baseline": base_latency["count"], "window": latency["count"]},
            **{q: {**_change(base_latency[q], latency[q], False, ratio=False),
                   "ratio": round(latency[q] / base_latency[q], 2) if base_latency[q] else None} for q in PERCENTILES},
        }
    else:
        result["api_latency_ms"] = {"available": False, "reason": "API latency not measured in "
                                    + ("baseline" if not base_latency else "this window")}
    for name, change in result.items():
        if change.get("available") and qualified:
            change["qualified"] = qualified
    return {"metrics": result, "qualified": qualified}
