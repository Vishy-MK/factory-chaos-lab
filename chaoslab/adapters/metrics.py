from __future__ import annotations

import statistics
import threading
import time
from typing import Any

from chaoslab import resources
from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter, parse_prometheus


class MetricsAdapter:
    def __init__(self, plant: FactorySemanticsAdapter):
        self.plant = plant
        self.workload_counters: dict[str, Any] = {}

    def sample(self, phase: str = "") -> dict:
        wall = time.time()
        sample: dict[str, Any] = {"t": wall, "phase": phase}
        health = self.plant.health(timeout=2.0)
        body = health.body if health.ok and isinstance(health.body, dict) else {}
        sample["api"] = {
            "up": bool(health.ok and body.get("status") == "ok"),
            "latency_ms": round(health.latency_s * 1000, 1) if health.ok else None,
            "status": health.status,
            "error": health.error,
        }
        watching = body.get("watching") if body else None
        sample["machines"] = {
            "total": watching.get("machines") if watching else None,
            "connected": watching.get("connected") if watching else None,
            "disconnected": watching.get("disconnected") if watching else None,
            "unknown": watching.get("unknown") if watching else None,
        }
        floor = body.get("floor") if body else None
        sample["floor_seconds_since_check"] = floor.get("seconds_since") if floor else None

        sample["authenticated_ok"] = None
        sample["work_orders"] = {"good_qty": None, "scrap_qty": None, "running": None}
        sample["equipment_states"] = None
        sample["tag_values_max_id"] = None
        sample["audit_entries_max_id"] = None

        if sample["api"]["up"]:
            summary = self.plant.order_summary()
            sample["authenticated_ok"] = bool(summary.ok)
            if summary.ok and isinstance(summary.body, dict):
                by_status = summary.body.get("by_status") or {}
                sample["work_orders"] = {
                    "good_qty": summary.body.get("good_qty"),
                    "scrap_qty": summary.body.get("scrap_qty"),
                    "running": by_status.get("running"),
                }
            states = self.plant.equipment_states()
            if states.ok and isinstance(states.body, list):
                sample["equipment_states"] = {row["equipment"]: row["state"] for row in states.body}
            metrics = self.plant.metrics_text()
            if metrics.ok and isinstance(metrics.body, str):
                for name, _labels, value in parse_prometheus(metrics.body):
                    if name == "mes_tag_values_max_id":
                        sample["tag_values_max_id"] = int(value)
                    elif name == "mes_audit_entries_max_id":
                        sample["audit_entries_max_id"] = int(value)

        table = self.plant.process_table()
        sample["components"] = {
            name: {"alive": row["alive"], "status": row["status"], "rss_mb": row["rss_mb"],
                   "cpu_percent": row["cpu_percent"]}
            for name, row in table.items()
        }
        sample["database"] = database_counters(self.plant.settings)
        sample["cgroups"] = scope_counters(table)
        host = resources.host_snapshot(cpu_interval=0.0)
        sample["host"] = host.as_dict()
        return sample

    @staticmethod
    def summarise_database_window(samples: list[dict], start: float, end: float) -> dict | None:
        return summarise_database([s for s in samples if start <= s["t"] <= end])

    def summarise(self, samples: list[dict]) -> dict:
        if not samples:
            return {"samples": 0}
        up = [s for s in samples if s["api"]["up"]]
        latencies = [s["api"]["latency_ms"] for s in samples if s["api"]["latency_ms"] is not None]
        probed = [s for s in samples if s["authenticated_ok"] is not None]
        connected = [s["machines"]["connected"] for s in samples if s["machines"]["connected"] is not None]
        floor_age = [s["floor_seconds_since_check"] for s in samples if s["floor_seconds_since_check"] is not None]
        ids = [(s["t"], s["tag_values_max_id"]) for s in samples if s["tag_values_max_id"] is not None]
        good = [s["work_orders"]["good_qty"] for s in samples if s["work_orders"]["good_qty"] is not None]
        scrap = [s["work_orders"]["scrap_qty"] for s in samples if s["work_orders"]["scrap_qty"] is not None]
        rss = {}
        for name in samples[0]["components"]:
            values = [s["components"][name]["rss_mb"] for s in samples
                      if name in s["components"] and s["components"][name]["rss_mb"] is not None]
            rss[name] = round(max(values), 1) if values else None
        alive_ratio = {}
        for name in samples[0]["components"]:
            flags = [bool(s["components"].get(name, {}).get("alive")) for s in samples]
            alive_ratio[name] = round(sum(flags) / len(flags), 3)
        ingest = None
        if len(ids) >= 2 and ids[-1][0] > ids[0][0]:
            ingest = round((ids[-1][1] - ids[0][1]) / (ids[-1][0] - ids[0][0]), 3)
        return {
            "samples": len(samples),
            "duration_s": round(samples[-1]["t"] - samples[0]["t"], 2),
            "api_success_ratio": round(len(up) / len(samples), 3),
            "authenticated_success_ratio": (
                round(sum(1 for s in probed if s["authenticated_ok"]) / len(probed), 3) if probed else None
            ),
            "api_latency_ms": _distribution(latencies),
            "machines_connected_min": min(connected) if connected else None,
            "machines_total": next((s["machines"]["total"] for s in samples if s["machines"]["total"]), None),
            "floor_seconds_since_check_max": round(max(floor_age), 1) if floor_age else None,
            "tag_ingest_per_second": ingest,
            "tag_ingest_stall_s_max": _longest_stall(ids),
            "tag_values_max_id_start": ids[0][1] if ids else None,
            "tag_values_max_id_end": ids[-1][1] if ids else None,
            "good_qty_end": good[-1] if good else None,
            "scrap_qty_end": scrap[-1] if scrap else None,
            "component_alive_ratio": alive_ratio,
            "component_rss_mb_max": rss,
            "host_available_memory_mb_min": round(min(s["host"]["available_memory_mb"] for s in samples), 1),
            "host_cpu_percent_max": round(max(s["host"]["cpu_percent"] for s in samples), 1),
            "database": summarise_database(samples),
            "cgroups": summarise_cgroups(samples),
        }


def scope_counters(table: dict[str, dict]) -> dict | None:
    """cgroup counters of every component that runs in its lab scope, keyed by component; None if none does."""
    try:
        from chaoslab.chaos import cgroups
    except Exception:
        return None
    found = {}
    for name, row in table.items():
        pid = row.get("pid")
        unit = cgroups.process_unit(pid) if pid else None
        if not unit:
            continue
        try:
            stats = cgroups.stats(unit)
        except Exception:
            stats = None
        if stats:
            found[name] = stats
    return found or None


def _counter_delta(start, end):
    if start is None or end is None:
        return None
    # A scope recreated inside the phase (component restarted) starts its counters again from zero.
    return end - start if end >= start else end


def summarise_cgroups(samples: list[dict]) -> dict | None:
    """Per scoped component: memory, OOM kills, CPU use, throttling and pressure stalls inside the phase."""
    with_cg = [s for s in samples if s.get("cgroups")]
    if not with_cg:
        return None
    out = {}
    components = sorted({name for s in with_cg for name in s["cgroups"]})
    for name in components:
        rows = [(s["t"], s["cgroups"][name]) for s in with_cg if name in s["cgroups"]]
        (t0, first), (t1, last) = rows[0], rows[-1]
        window = t1 - t0
        current = [r["memory_current"] for _, r in rows if r.get("memory_current") is not None]
        usage = _counter_delta(first.get("cpu_usage_us"), last.get("cpu_usage_us"))
        periods = _counter_delta(first.get("cpu_nr_periods"), last.get("cpu_nr_periods"))
        throttled = _counter_delta(first.get("cpu_nr_throttled"), last.get("cpu_nr_throttled"))
        throttled_us = _counter_delta(first.get("cpu_throttled_us"), last.get("cpu_throttled_us"))

        def stall(kind):
            a = ((first.get(kind) or {}).get("some") or {}).get("total_us")
            b = ((last.get(kind) or {}).get("some") or {}).get("total_us")
            delta = _counter_delta(a, b)
            return round(delta / 1000.0, 1) if delta is not None else None

        events = {k: _counter_delta(first["memory_events"].get(k), last["memory_events"].get(k))
                  for k in ("high", "max", "oom_kill")}
        out[name] = {
            "unit": last.get("unit"),
            "samples": len(rows),
            "window_s": round(window, 2),
            "limits_end": last.get("limits"),
            "memory_current_mb_max": round(max(current) / 1048576, 1) if current else None,
            "memory_current_mb_mean": round(statistics.fmean(current) / 1048576, 1) if current else None,
            "memory_peak_mb": round(last["memory_peak"] / 1048576, 1) if last.get("memory_peak") else None,
            "memory_high_events": events["high"],
            "memory_max_events": events["max"],
            "oom_kills": events["oom_kill"],
            "cpu_percent_mean": round(usage / (window * 1e6) * 100, 1) if usage is not None and window > 0 else None,
            "cpu_throttled_periods": throttled,
            "cpu_throttled_ratio": round(throttled / periods, 3) if periods else None,
            "cpu_throttled_ms": round(throttled_us / 1000.0, 1) if throttled_us is not None else None,
            "memory_stall_ms": stall("memory_pressure"),
            "cpu_stall_ms": stall("cpu_pressure"),
        }
    return out


def _longest_stall(ids: list[tuple[float, int]]) -> float | None:
    """Longest wall time inside the samples during which the newest tag id did not advance."""
    if len(ids) < 2:
        return None
    longest, changed_at, last = 0.0, ids[0][0], ids[0][1]
    for t, value in ids[1:]:
        if value > last:
            changed_at, last = t, value
        longest = max(longest, t - changed_at)
    return round(longest, 2)


_DB_FIELDS = ("statements_total", "errors", "lock_errors", "observed_ms_total", "db_ms_total",
              "injected_statements", "injected_ms_total", "injected_observed_ms_total")


def database_counters(settings) -> dict | None:
    """Cumulative counters of every live hooked process of the plant, keyed by pid; None if no hook reports."""
    try:
        from chaoslab.plant_names import chaoslab_dbhook

        stats = chaoslab_dbhook.read_stats(str(settings.db_hook_dir))
    except Exception:
        return None
    rows = {}
    for row in stats:
        if row.get("plant") != settings.plant_name:
            continue
        try:
            rows[str(row["pid"])] = {
                "component": row["component"], "statements_total": row["statements_total"],
                "statements": row["statements"], "errors": row["errors"], "lock_errors": row["lock_errors"],
                "observed_ms_total": row["observed_ms_total"], "db_ms_total": row["db_ms_total"],
                "observed_histogram": row["observed_histogram"], "db_histogram": row["db_histogram"],
                "injected_statements": row["injected"]["statements"],
                "injected_ms_total": row["injected"]["ms_total"],
                "injected_observed_ms_total": row["injected"].get("observed_ms_total", 0.0),
                "activation_active": bool(row.get("control", {}).get("active")),
            }
        except (KeyError, TypeError):
            continue
    return rows or None


def _histogram_bound(histogram: list[int], quantile: float) -> float | None:
    from chaoslab.plant_names.chaoslab_dbhook import BUCKETS_MS

    total = sum(histogram)
    if not total:
        return None
    needed, running = quantile * total, 0
    for index, count in enumerate(histogram):
        running += count
        if running >= needed:
            return float(BUCKETS_MS[index]) if index < len(BUCKETS_MS) else None
    return None


def _histogram_summary(histogram: list[int]) -> dict:
    from chaoslab.plant_names.chaoslab_dbhook import BUCKETS_MS

    labels = [f"<={bound}" for bound in BUCKETS_MS] + [f">{BUCKETS_MS[-1]}"]
    return {
        "count": sum(histogram),
        "p50_upper_bound": _histogram_bound(histogram, 0.50),
        "p95_upper_bound": _histogram_bound(histogram, 0.95),
        "p99_upper_bound": _histogram_bound(histogram, 0.99),
        "buckets": {label: count for label, count in zip(labels, histogram) if count},
    }


def summarise_database(samples: list[dict]) -> dict | None:
    """Direct database evidence between the first and last sample of the phase, from the hook's counters.

    Percentiles come from fixed histogram buckets and are upper bounds (null above the last bucket); means are
    exact. A process that started inside the phase is counted from zero.
    """
    with_db = [s for s in samples if s.get("database")]
    if len(with_db) < 2 or with_db[-1]["t"] <= with_db[0]["t"]:
        return None
    first, last = with_db[0]["database"], with_db[-1]["database"]
    window = with_db[-1]["t"] - with_db[0]["t"]
    totals = dict.fromkeys(_DB_FIELDS, 0)
    kinds: dict[str, int] = {}
    observed, driver = None, None
    per_component: dict[str, dict] = {}
    for pid, end in last.items():
        start = first.get(pid)
        delta = {f: end[f] - (start[f] if start else 0) for f in _DB_FIELDS}
        for field in _DB_FIELDS:
            totals[field] += delta[field]
        for kind, count in end["statements"].items():
            kinds[kind] = kinds.get(kind, 0) + count - ((start or {}).get("statements") or {}).get(kind, 0)
        for name, store in (("observed_histogram", "observed"), ("db_histogram", "driver")):
            base = start[name] if start else [0] * len(end[name])
            values = [a - b for a, b in zip(end[name], base)]
            current = observed if store == "observed" else driver
            merged = values if current is None else [a + b for a, b in zip(current, values)]
            if store == "observed":
                observed = merged
            else:
                driver = merged
        component = per_component.setdefault(end["component"], {"statements": 0, "injected_statements": 0,
                                                                 "observed_ms_total": 0.0})
        component["statements"] += delta["statements_total"]
        component["injected_statements"] += delta["injected_statements"]
        component["observed_ms_total"] += delta["observed_ms_total"]
    count = totals["statements_total"]
    injected = totals["injected_statements"]
    return {
        "source": "chaoslab_dbhook: SQLAlchemy cursor execution in the plant's database clients",
        "samples": len(with_db),
        "window_s": round(window, 2),
        "statements": count,
        "statements_per_second": round(count / window, 2),
        "by_kind": kinds,
        "errors": totals["errors"],
        "lock_errors": totals["lock_errors"],
        "error_ratio": round(totals["errors"] / count, 4) if count else None,
        "injected_statements": injected,
        "injected_fraction": round(injected / count, 3) if count else None,
        "injected_delay_ms_total": round(totals["injected_ms_total"], 1),
        "injected_delay_ms_mean": round(totals["injected_ms_total"] / injected, 2) if injected else None,
        "delayed_statement_ms_mean": round(totals["injected_observed_ms_total"] / injected, 3) if injected else None,
        "statement_ms_mean": round(totals["observed_ms_total"] / count, 3) if count else None,
        "driver_ms_mean": round(totals["db_ms_total"] / count, 3) if count else None,
        "statement_ms": _histogram_summary(observed or []),
        "driver_ms": _histogram_summary(driver or []),
        "per_component": {
            name: {"statements": row["statements"], "injected_statements": row["injected_statements"],
                   "statement_ms_mean": round(row["observed_ms_total"] / row["statements"], 3)
                   if row["statements"] else None}
            for name, row in per_component.items()
        },
        "activation_seen": any(r.get("activation_active") for s in with_db for r in s["database"].values()),
    }


def _distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "count": len(ordered),
        "min": round(ordered[0], 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[p95_index], 1),
        "max": round(ordered[-1], 1),
    }


class Sampler:
    def __init__(self, metrics: MetricsAdapter, interval_s: float):
        self.metrics = metrics
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self.phase = "idle"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="chaoslab-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            with self._lock:
                phase = self.phase
            sample = self.metrics.sample(phase)
            with self._lock:
                if self.phase != phase:
                    sample["phase"] = "transition"
                    sample["phase_before"] = phase
                    sample["phase_after"] = self.phase
                self.samples.append(sample)
            remaining = self.interval_s - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        with self._lock:
            return list(self.samples)

    def by_phase(self, phase: str) -> list[dict]:
        with self._lock:
            return [s for s in self.samples if s["phase"] == phase]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.samples)


class HostSampler:
    def __init__(self, interval_s: float = 2.0):
        self.interval_s = interval_s
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import psutil

        psutil.cpu_percent(interval=None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="chaoslab-host-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import psutil

        while not self._stop.is_set():
            rss = 0.0
            processes = 0
            for proc in psutil.process_iter(["cmdline", "memory_info"]):
                try:
                    cmdline = proc.info["cmdline"] or []
                    if len(cmdline) >= 3 and cmdline[1].endswith("/fsmes"):
                        rss += proc.info["memory_info"].rss / 1048576
                        processes += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            host = resources.host_snapshot(cpu_interval=0.0)
            self.samples.append({"t": time.time(), "host": host.as_dict(), "fsmes_processes": processes,
                                 "fsmes_rss_mb": round(rss, 1)})
            self._stop.wait(self.interval_s)

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return list(self.samples)

    @staticmethod
    def summarise(samples: list[dict]) -> dict:
        if not samples:
            return {"samples": 0}
        return {
            "samples": len(samples),
            "host_available_memory_mb_min": round(min(s["host"]["available_memory_mb"] for s in samples), 1),
            "host_cpu_percent_mean": round(statistics.fmean(s["host"]["cpu_percent"] for s in samples), 1),
            "host_cpu_percent_max": round(max(s["host"]["cpu_percent"] for s in samples), 1),
            "fsmes_rss_mb_max": round(max(s["fsmes_rss_mb"] for s in samples), 1),
            "fsmes_processes_max": max(s["fsmes_processes"] for s in samples),
        }
