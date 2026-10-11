"""Which services an injection is affecting, live: normal, degraded, down or unknown, with the reason.

Each service is judged from the exporter's own probes against that service's rolling baseline, the median of the
values seen while no experiment held the lease. Until a baseline exists, fixed fallback limits apply. Nothing is
inferred that was not measured: a service without a signal is `unknown`, not `normal`.
"""

from __future__ import annotations

import statistics
import time
from collections import deque

NORMAL, DEGRADED, DOWN, UNKNOWN = 0, 1, 2, -1
COMPONENT_SERVICE = {"api": "MES API", "opc-sim": "OPC UA server", "opc-agent": "OPC agent / ingestion",
                     "floor": "Shop floor", "database": "Database"}
SERVICES = ("MES API", "Database", "OPC UA server", "Machine connectivity", "OPC agent / ingestion", "Shop floor",
            "RAG backend")
BASELINE_WINDOW_S = 600
MIN_BASELINE = 10
FLOOR_LIMIT_S = 45.0


class ImpactTracker:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.quiet: dict[str, deque] = {k: deque() for k in ("api_latency", "db_ms", "ingest")}
        self.previous_db: tuple[float, float, float, float, float] | None = None   # t, statements, ms, errors, locked
        self.tags: deque = deque(maxlen=64)
        self.previous_cgroups: dict[str, tuple[float, float, float]] = {}

    def _baseline(self, name: str) -> float | None:
        values = [v for _, v in self.quiet[name]]
        return statistics.median(values) if len(values) >= MIN_BASELINE else None

    def _remember(self, name: str, value: float | None, quiet: bool, now: float) -> None:
        if value is None or not quiet:
            return
        series = self.quiet[name]
        series.append((now, value))
        while series and now - series[0][0] > BASELINE_WINDOW_S:
            series.popleft()

    def evaluate(self, probe: dict, components: dict, db_rows: dict | None, cgroups: dict | None,
                 rag_up: bool | None, experiment_active: bool) -> dict[str, dict]:
        """probe: plant_up, api_latency_s, machines_connected, machines_total, floor_age_s, tag_id (each optional)."""
        now = self.clock()
        quiet = not experiment_active
        out: dict[str, dict] = {}

        def put(service, status, reason):
            out[service] = {"status": status, "reason": reason}

        def component_state(name):
            row = components.get(name) or {}
            if not row.get("alive"):
                return DOWN, f"{name} process not running"
            if row.get("status") == "stopped":
                return DOWN, f"{name} process frozen (stopped)"
            return None

        # MES API
        down = component_state("api")
        latency = probe.get("api_latency_s")
        baseline = self._baseline("api_latency")
        if down:
            put("MES API", *down)
        elif probe.get("plant_up") is False:
            put("MES API", DOWN, "the API does not answer /health")
        elif latency is None:
            put("MES API", UNKNOWN, "no API probe yet")
        else:
            limit = max(3 * baseline, baseline + 0.1) if baseline is not None else 0.5
            slow = latency > limit
            put("MES API", DEGRADED if slow else NORMAL,
                f"/health {latency * 1000:.0f} ms vs limit {limit * 1000:.0f} ms"
                + ("" if baseline is not None else " (fallback limit; baseline still building)"))
            self._remember("api_latency", latency, quiet and not slow, now)

        # Database (the lab hook's counters in the database clients)
        if not db_rows:
            put("Database", UNKNOWN, "no database hook counters (plant not started by the lab)")
        else:
            statements = sum(sum(r["statements"].values()) for r in db_rows.values())
            ms = sum(r["observed_ms"] for r in db_rows.values())
            errors = sum(r["errors"] for r in db_rows.values())
            locked = sum(r["lock_errors"] for r in db_rows.values())
            delay = any(r["active"] for r in db_rows.values())
            reasons, status = [], NORMAL
            if self.previous_db is not None and statements >= self.previous_db[1]:
                d_statements = statements - self.previous_db[1]
                mean = (ms - self.previous_db[2]) / d_statements if d_statements else None
                base = self._baseline("db_ms")
                if mean is not None:
                    limit = 3 * base + 5 if base is not None else 50.0
                    if mean > limit:
                        status = DEGRADED
                        reasons.append(f"mean statement {mean:.1f} ms vs limit {limit:.1f} ms")
                    else:
                        reasons.append(f"mean statement {mean:.2f} ms")
                    self._remember("db_ms", mean, quiet and mean <= limit and not delay, now)
                if errors > self.previous_db[3]:
                    status = DEGRADED
                    reasons.append(f"{errors - self.previous_db[3]:.0f} new errors")
                if locked > self.previous_db[4]:
                    status = DEGRADED
                    reasons.append(f"{locked - self.previous_db[4]:.0f} new 'database is locked'")
            if delay:
                status = DEGRADED
                reasons.insert(0, "latency injection active")
            self.previous_db = (now, statements, ms, errors, locked)
            put("Database", status, "; ".join(reasons) or "collecting counters")

        # OPC UA server and machine connectivity
        down = component_state("opc-sim")
        connected, total = probe.get("machines_connected"), probe.get("machines_total")
        put("OPC UA server", *(down or (NORMAL, "replay server running")))
        if connected is None or not total:
            put("Machine connectivity", UNKNOWN, "no connectivity figure from the MES")
        elif connected == 0:
            put("Machine connectivity", DOWN, f"0 of {total:.0f} machines connected")
        elif connected < total:
            put("Machine connectivity", DEGRADED, f"{connected:.0f} of {total:.0f} machines connected")
        else:
            put("Machine connectivity", NORMAL, f"{total:.0f} of {total:.0f} machines connected")

        # OPC agent / ingestion
        down = component_state("opc-agent")
        tag = probe.get("tag_id")
        if tag is not None:
            if self.tags and tag < self.tags[-1][1]:
                self.tags.clear()          # counter reset: start a new segment
            self.tags.append((now, float(tag)))
        recent = [(t, v) for t, v in self.tags if now - t <= 15]
        rate = None
        if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
            rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
        if down:
            put("OPC agent / ingestion", *down)
        elif rate is None:
            put("OPC agent / ingestion", UNKNOWN, "not enough tag ingestion history yet")
        else:
            base = self._baseline("ingest")
            if rate == 0 and len(recent) >= 3:
                put("OPC agent / ingestion", DOWN, "no new telemetry in the last 15 s")
            elif base is not None and rate < 0.5 * base:
                put("OPC agent / ingestion", DEGRADED, f"{rate:.1f} tags/s vs baseline {base:.1f}")
            else:
                put("OPC agent / ingestion", NORMAL, f"{rate:.1f} tags/s" + (f" (baseline {base:.1f})" if base else ""))
            self._remember("ingest", rate, quiet and rate > 0, now)

        # Shop floor
        down = component_state("floor")
        age = probe.get("floor_age_s")
        if down:
            put("Shop floor", *down)
        elif age is None:
            put("Shop floor", UNKNOWN, "no floor figure from the MES")
        else:
            put("Shop floor", DEGRADED if age > FLOOR_LIMIT_S else NORMAL,
                f"last quality check {age:.0f} s ago (limit {FLOOR_LIMIT_S:.0f} s)")

        # cgroup limits make the limited component's service degraded while they bite
        for component, stats in (cgroups or {}).items():
            service = COMPONENT_SERVICE.get(component)
            periods, throttled = stats.get("cpu_nr_periods") or 0, stats.get("cpu_nr_throttled") or 0
            high = (stats.get("memory_events") or {}).get("high", 0)
            previous = self.previous_cgroups.get(component)
            self.previous_cgroups[component] = (periods, throttled, high)
            if not service or previous is None or periods < previous[0]:
                continue
            ratio = (throttled - previous[1]) / (periods - previous[0]) if periods > previous[0] else 0.0
            new_high = high - previous[2]
            if ratio > 0.2 or new_high > 0:
                current = out.get(service, {"status": NORMAL, "reason": ""})
                if current["status"] in (NORMAL, UNKNOWN):
                    detail = f"cgroup throttled {ratio * 100:.0f}% of periods" if ratio > 0.2 else \
                        f"{new_high:.0f} memory.high events"
                    out[service] = {"status": DEGRADED, "reason": f"{detail}; {current['reason']}".strip("; ")}

        if rag_up is not None:
            put("RAG backend", NORMAL if rag_up else DOWN, "/health ok" if rag_up else "/health not answering")
        return out


def target_service(records: list[dict]) -> list[tuple[str, str]]:
    """(fault type, service) for each fault the active lease records."""
    out = []
    for record in records or []:
        component = record.get("component") or ("database" if record.get("type") in ("database_latency",
                                                                                     "sqlite_write_lock") else None)
        if record.get("type"):
            out.append((record["type"], COMPONENT_SERVICE.get(component, component or "plant")))
    return out
