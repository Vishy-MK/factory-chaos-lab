"""Deterministic metrics of one window of normalised samples. No model is involved anywhere here.

Every metric carries its unit, method and sample count. A metric the source does not measure is reported as
unavailable, never as zero. Percentiles use the nearest-rank method on the samples of one window and are never
averaged across windows.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

METRICS = {
    # id: (unit, definition)
    "api_success_ratio": ("ratio", "share of samples in which the MES API health check answered 'ok'"),
    "authenticated_success_ratio": ("ratio", "share of authenticated work-order summary requests that succeeded"),
    "api_errors": ("count", "health checks that failed, by error type"),
    "api_timeouts": ("count", "health checks that failed with a timeout"),
    "api_latency_ms": ("ms", "health-check round-trip latency of successful checks (lab probe, 1 per sample)"),
    "machines_connected_min": ("machines", "fewest machines the MES reported connected in the window"),
    "machines_all_connected_ratio": ("ratio", "share of samples with every machine connected"),
    "tag_ingest_per_second": ("tags/s", "growth of the newest tag-value id per second, summed over segments "
                                        "between counter resets"),
    "tag_ingest_stall_max_s": ("s", "longest span inside the window in which the newest tag id did not advance"),
    "tag_id_resets": ("count", "times the newest tag id went backwards (counter or replay reset)"),
    "telemetry_age_max_s": ("s", "oldest telemetry age the source reported in the window"),
    "telemetry_stale_ratio": ("ratio", "share of samples whose telemetry was older than the staleness limit"),
    "floor_age_max_s": ("s", "longest time since the floor simulator last recorded a check"),
    "target_reachable_ratio": ("ratio", "share of independent probes that reached the fault target"),
    "production_increase": ("units", "growth of each production counter, summed over segments between resets"),
    "production_counter_resets": ("count", "times a production counter went backwards"),
    "db_statements_per_second": ("statements/s", "plant database statements per second (lab hook)"),
    "db_statement_ms_mean": ("ms", "mean statement time including any injected delay (lab hook)"),
    "db_driver_ms_mean": ("ms", "mean driver time excluding injected delay (lab hook)"),
    "db_injected_statements": ("count", "statements the hook delayed"),
    "db_injected_delay_ms_mean": ("ms", "mean delay the hook added per delayed statement"),
    "db_delayed_statement_ms_mean": ("ms", "mean total time of delayed statements"),
    "db_errors": ("count", "database errors seen by the hook"),
    "db_lock_errors": ("count", "'database is locked' errors seen by the hook"),
    "db_driver_ms_mean_by_component": ("ms", "mean driver time per database client"),
    "cgroup": ("mixed", "cgroup v2 counters of the limited component's lab scope"),
    "host_cpu_percent_max": ("%", "highest host CPU utilisation sampled"),
    "host_available_mb_min": ("MB", "lowest host available memory sampled"),
}


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least q of the samples at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"count": len(values), "min": round(min(values), 3), "p50": round(percentile(values, 0.50), 3),
            "p90": round(percentile(values, 0.90), 3), "p95": round(percentile(values, 0.95), 3),
            "p99": round(percentile(values, 0.99), 3), "max": round(max(values), 3),
            "mean": round(statistics.fmean(values), 3), "method": "nearest-rank"}


def _ratio(flags: list[bool]) -> dict | None:
    if not flags:
        return None
    return {"value": round(sum(flags) / len(flags), 4), "samples": len(flags)}


def counter_progress(points: list[tuple[float, float]]) -> dict | None:
    """Growth of a counter that can reset: increases are summed per segment; a decrease starts a new segment."""
    if len(points) < 2:
        return None
    increase, resets, stall, changed_at, last = 0.0, 0, 0.0, points[0][0], points[0][1]
    for t, value in points[1:]:
        if value < last:
            resets += 1
            changed_at = t
        elif value > last:
            increase += value - last
            changed_at = t
        stall = max(stall, t - changed_at)
        last = value
    span = points[-1][0] - points[0][0]
    return {"increase": increase, "per_second": round(increase / span, 4) if span > 0 else None, "resets": resets,
            "longest_stall_s": round(stall, 2), "span_s": round(span, 2), "points": len(points)}


def window_metrics(samples: list[dict], config: dict, start: float | None = None,
                   end: float | None = None) -> dict:
    """All metrics of one window. Unmeasured metrics are absent from `metrics` and listed in `unavailable`."""
    from chaoslab.adapters.metrics import summarise_cgroups, summarise_database

    measurement = config["measurement"]
    out: dict[str, Any] = {}
    n = len(samples)
    times = [s["t"] for s in samples]
    meta = {"samples": n, "start": start if start is not None else (min(times) if times else None),
            "end": end if end is not None else (max(times) if times else None),
            "observed_span_s": round(max(times) - min(times), 2) if len(times) > 1 else 0.0,
            "adequate": n >= int(measurement["min_samples_per_window"])}
    if start is not None and end is not None:
        meta["window_s"] = round(end - start, 2)
        if times and (min(times) - start > 2.5 or end - max(times) > 2.5):
            meta["truncated"] = "samples do not cover the whole window"

    up = [s["api_up"] for s in samples if s["api_up"] is not None]
    if up:
        out["api_success_ratio"] = _ratio([bool(x) for x in up])
        errors: dict[str, int] = {}
        for s in samples:
            if s["api_up"] is False:
                errors[str(s.get("api_error") or "unknown")] = errors.get(str(s.get("api_error") or "unknown"), 0) + 1
        out["api_errors"] = {"value": sum(errors.values()), "by_type": errors, "samples": len(up)}
        out["api_timeouts"] = {"value": sum(v for k, v in errors.items() if "timeout" in k.lower()),
                               "samples": len(up)}
    auth = [s["auth_ok"] for s in samples if s["auth_ok"] is not None]
    if auth:
        out["authenticated_success_ratio"] = _ratio([bool(x) for x in auth])
    latency = [float(s["api_latency_ms"]) for s in samples if s["api_latency_ms"] is not None]
    if latency:
        out["api_latency_ms"] = distribution(latency)
    machines = [s for s in samples if s["machines_connected"] is not None and s["machines_total"]]
    if machines:
        out["machines_connected_min"] = {"value": min(s["machines_connected"] for s in machines),
                                         "total": max(s["machines_total"] for s in machines), "samples": len(machines)}
        out["machines_all_connected_ratio"] = _ratio([s["machines_connected"] == s["machines_total"] for s in machines])
    tags = [(s["t"], float(s["tag_id"])) for s in samples if s["tag_id"] is not None]
    progress = counter_progress(tags)
    if progress:
        out["tag_ingest_per_second"] = {"value": progress["per_second"], "samples": progress["points"]}
        out["tag_ingest_stall_max_s"] = {"value": progress["longest_stall_s"], "samples": progress["points"]}
        out["tag_id_resets"] = {"value": progress["resets"], "samples": progress["points"]}
    telemetry = [float(s["telemetry_age_s"]) for s in samples if s["telemetry_age_s"] is not None]
    if telemetry:
        out["telemetry_age_max_s"] = {"value": round(max(telemetry), 2), "samples": len(telemetry)}
        out["telemetry_stale_ratio"] = _ratio([a > float(measurement["telemetry_stale_s"]) for a in telemetry])
    floor = [float(s["floor_age_s"]) for s in samples if s["floor_age_s"] is not None]
    if floor:
        out["floor_age_max_s"] = {"value": round(max(floor), 2), "samples": len(floor)}
    reach = [s["target_reachable"] for s in samples if s["target_reachable"] is not None]
    if reach:
        out["target_reachable_ratio"] = _ratio([bool(x) for x in reach])
    counters: dict[str, list[tuple[float, float]]] = {}
    for s in samples:
        for name, value in (s.get("production") or {}).items():
            if isinstance(value, (int, float)):
                counters.setdefault(name, []).append((s["t"], float(value)))
    if counters:
        progressed = {name: counter_progress(points) for name, points in counters.items()}
        out["production_increase"] = {"value": {k: v["increase"] for k, v in progressed.items() if v},
                                      "samples": max(len(p) for p in counters.values())}
        out["production_counter_resets"] = {"value": sum(v["resets"] for v in progressed.values() if v),
                                            "by_counter": {k: v["resets"] for k, v in progressed.items() if v},
                                            "samples": max(len(p) for p in counters.values())}
    db = summarise_database([{"t": s["t"], "database": s["database"]} for s in samples if s.get("database")])
    if db:
        for key, field in (("db_statements_per_second", "statements_per_second"),
                           ("db_statement_ms_mean", "statement_ms_mean"), ("db_driver_ms_mean", "driver_ms_mean"),
                           ("db_injected_statements", "injected_statements"),
                           ("db_injected_delay_ms_mean", "injected_delay_ms_mean"),
                           ("db_delayed_statement_ms_mean", "delayed_statement_ms_mean"),
                           ("db_errors", "errors"), ("db_lock_errors", "lock_errors")):
            if db.get(field) is not None:
                out[key] = {"value": db[field], "samples": db["samples"], "statements": db["statements"]}
        out["db_statement_ms_p95_bound"] = {"value": (db.get("statement_ms") or {}).get("p95_upper_bound"),
                                            "samples": db["samples"], "statements": db["statements"],
                                            "method": "histogram bucket upper bound"}
        by_component = _driver_by_component(samples)
        if by_component:
            out["db_driver_ms_mean_by_component"] = {"value": by_component, "samples": db["samples"]}
    cg = summarise_cgroups([{"t": s["t"], "cgroups": s["cgroups"]} for s in samples if s.get("cgroups")])
    if cg:
        out["cgroup"] = {"value": cg, "samples": max(v["samples"] for v in cg.values())}
    cpu = [s["host_cpu_percent"] for s in samples if s.get("host_cpu_percent") is not None]
    if cpu:
        out["host_cpu_percent_max"] = {"value": round(max(cpu), 1), "samples": len(cpu)}
    memory = [s["host_available_mb"] for s in samples if s.get("host_available_mb") is not None]
    if memory:
        out["host_available_mb_min"] = {"value": round(min(memory), 1), "samples": len(memory)}
    unavailable = sorted(m for m in METRICS if m not in out and m != "cgroup")
    return {"window": meta, "metrics": out, "unavailable": unavailable}


def _driver_by_component(samples: list[dict]) -> dict | None:
    rows = [s for s in samples if s.get("database")]
    if len(rows) < 2:
        return None
    first, last = rows[0]["database"], rows[-1]["database"]
    out: dict[str, dict] = {}
    for pid, end in last.items():
        start = first.get(pid) or {}
        statements = end["statements_total"] - start.get("statements_total", 0)
        driver = end["db_ms_total"] - start.get("db_ms_total", 0.0)
        row = out.setdefault(end["component"], {"statements": 0, "driver_ms_total": 0.0})
        row["statements"] += statements
        row["driver_ms_total"] += driver
    return {name: {"statements": r["statements"],
                   "driver_ms_mean": round(r["driver_ms_total"] / r["statements"], 3) if r["statements"] else None}
            for name, r in out.items()}
