"""Recommended fixes for observed weaknesses. Recommendations only: nothing here changes code or runs anything.

Each recommendation names source locations only after checking, at analysis time, that the cited pattern is
present in the upstream checkout. A location that does not check out is reported as unverified, not cited.
"""

from __future__ import annotations

import re
from pathlib import Path

#: (path relative to the upstream src/fsmes directory, regex) -> what the location shows
LOCATIONS = {
    "equipment_states_route": ("api/routers/equipment.py", r"\"equipment\": s\.equipment\.code",
                               "/equipment/states reads s.equipment.code for every current state"),
    "equipment_state_relationship": ("domain/equipment.py", r"equipment: Mapped\[Equipment\] = relationship\(\)",
                                     "EquipmentState.equipment uses SQLAlchemy's default lazy loading"),
    "read_only_get_sessions": ("api/deps.py", r"if request\.method in READ_METHODS:",
                               "GET requests get a read-only session that never takes the write lock"),
    "begin_immediate": ("db.py", r"conn\.exec_driver_sql\(\"BEGIN IMMEDIATE\"\)",
                        "every non-read-only transaction takes SQLite's write lock when it begins"),
    "busy_timeout": ("config.py", r"sqlite_busy_timeout_ms: int = 15_000",
                     "writers wait up to 15 s for the write lock before failing"),
    "opc_reconnect_delay": ("integrations/opc/agent.py", r"retrying in 3s",
                            "the OPC agent waits a fixed 3 s before every reconnect attempt"),
    "single_api_process": ("cli.py", r"uvicorn\.run\(create_app\(\), host=",
                           "run-api serves the API from one uvicorn process"),
}


def verify(settings, key: str) -> dict:
    relative, pattern, meaning = LOCATIONS[key]
    path = Path(settings.upstream_dir) / "src" / "fsmes" / relative
    shown = f"factorysemantics-mes/src/fsmes/{relative}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"path": shown, "verified": False, "detail": "file not found in the upstream checkout"}
    for number, line in enumerate(lines, 1):
        if re.search(pattern, line):
            return {"path": shown, "line": number, "verified": True, "shows": meaning}
    return {"path": shown, "verified": False, "detail": "the expected code was not found; location not cited"}


def _rec(finding: dict, title: str, remediation: str, why: str, locations: list[dict], tradeoffs: str, effort: str,
         acceptance: list[str], validation: str, area: str) -> dict:
    return {"finding_id": finding["id"], "area": area, "title": title, "remediation": remediation,
            "addresses": why, "locations": [loc for loc in locations if loc["verified"]],
            "unverified_locations": [loc for loc in locations if not loc["verified"]],
            "tradeoffs": tradeoffs, "effort": effort, "acceptance_criteria": acceptance,
            "manual_validation": validation, "applied": False}


def recommend(settings, findings: list[dict], analysis: dict) -> list[dict]:
    fault = analysis["inputs"]["experiment"]["fault"]
    fault_type = fault.get("type")
    params = fault.get("parameters") or {}
    windows = analysis["windows"]
    out = []
    for finding in findings:
        if finding["type"] != "weakness":
            continue
        kind = finding["kind"]
        if kind == "latency:api" and fault_type == "database_latency":
            delay = params.get("delay_ms")
            base_p95 = finding["baseline_comparison"]["p95"]["baseline"]
            out.append(_rec(
                finding, "Cut the statements per request on the probed read endpoints",
                "Eager-load the equipment of current states (for example selectinload(EquipmentState.equipment) "
                "in services.equipment.current_states) so /equipment/states issues a fixed number of statements "
                "instead of one more per machine.",
                "API latency under database delay grows with statements per request; the lazy relationship adds a "
                "statement per machine on an endpoint the plant's own screens and the lab call every second.",
                [verify(settings, "equipment_states_route"), verify(settings, "equipment_state_relationship"),
                 verify(settings, "read_only_get_sessions")],
                "A slightly larger single query; behaviour is unchanged. Other lazy relationships on hot paths "
                "deserve the same review.", "small (one query option and a test)",
                [f"with the same {delay} ms database_latency fault, /equipment/states executes at most 2 statements "
                 "per request (count from the hook's statement counter with an otherwise idle plant)",
                 f"API health-check p95 during the fault at most baseline p95 + 3 x delay "
                 f"({base_p95} + {3 * (delay or 0)} ms)"],
                "Apply the change in a branch, restart the plant, run a no-fault baseline, then rerun the same "
                "database_latency definition and compare with this analysis.", "transaction scope / query shape"))
        elif kind == "database:amplification":
            out.append(_rec(
                finding, "Keep write transactions short so a slow statement holds the write lock less long",
                "Book OPC readings in smaller transactions (commit per batch of state changes rather than per "
                "connection cycle) and move any read that does not need the lock out of write transactions.",
                "Writers begin with BEGIN IMMEDIATE and hold SQLite's single write lock while each of their delayed "
                "statements sleeps, so every other writer waits too (unverified: the hook does not time lock waits).",
                [verify(settings, "begin_immediate"), verify(settings, "busy_timeout")],
                "More commits mean more WAL syncs; with synchronous=NORMAL that cost is small. Very small batches "
                "raise per-row overhead.", "medium (agent booking code and its tests)",
                ["delayed statements take at most the injected delay + 20 ms on average under the same fault",
                 "0 'database is locked' errors and no ingestion stall longer than 2 s during the fault"],
                "Rerun the same database_latency definition and compare db_delayed_statement_ms_mean and "
                "db_driver_ms_mean_by_component with this analysis. Add per-transaction timing to the hook first "
                "if lock waits must be shown directly.", "database concurrency"))
        elif kind.startswith("throughput:") and fault_type == "database_latency":
            out.append(_rec(
                finding, "Decouple tag ingestion from database statement latency",
                "Buffer readings in the OPC agent and book them in fewer, larger statements (executemany) so a "
                "slower database lowers the commit rate, not the number of readings kept.",
                "Ingestion fell because each booking statement now costs the injected delay.",
                [verify(settings, "begin_immediate")],
                "A bounded buffer needs a size limit and an overflow policy; a crash loses what is buffered.",
                "medium", ["tag ingestion during the fault at least 85% of baseline",
                           "no reading lost: newest tag id after recovery equals the replayed count"],
                "Rerun the database_latency definition with the change and compare tag_ingest_per_second.",
                "buffering and backpressure"))
        elif kind == "observation:machines":
            reconnect = ((analysis["recovery"].get("recovery_time") or {}).get("seconds_after_fault_end"))
            out.append(_rec(
                finding, "Reconnect to the OPC server sooner after it returns",
                "Replace the fixed 3 s wait with bounded exponential backoff with jitter (for example 0.5 s doubling "
                "to a 5 s cap), and record each attempt in the connection table.",
                "Observation was lost for the whole outage and part of the recovery; a fixed delay sets a floor on "
                "how quickly the MES sees machines again.",
                [verify(settings, "opc_reconnect_delay")],
                "More connection attempts against a server that is down; the cap and jitter bound that.", "small",
                [f"machines reconnected within 1.5 s of the OPC server answering again (this run: {reconnect} s from "
                 "the end of the fault window to stable recovery)",
                 "reconnect attempts per minute against a down server at most 20"],
                "Rerun the same OPC outage (lab opc-server-outage-10s or the external chaos-opcua tool) and compare "
                "the recovery time and the MES reconnect timestamp.", "reconnection behaviour"))
        elif kind == "data:counter_reset":
            out.append(_rec(
                finding, "Make counter resets visible and verify they are not booked as production",
                "Record a counter-reset event when a machine counter goes backwards and check that booked production "
                "for the reset sample is zero; alert on it.",
                "The restarted server's counters fell to zero; an MES that books the difference would invent or lose "
                "production. These samples cannot show which one happened.", [],
                "None functional; one event per reset.", "small",
                ["every counter reset appears as an event with machine, old and new value",
                 "booked production across the reset equals the units counted before and after"],
                "Repeat the outage and compare MES booking with the probe's counters across the reset.",
                "observability"))
        elif kind == "latency:api" and fault_type == "resource_limit":
            out.append(_rec(
                finding, "Give the API CPU headroom or a second worker for request bursts",
                "Size the API's CPU allowance above its measured burst use, or run more than one API worker; the "
                "API is served by a single uvicorn process.",
                "Requests queued behind the throttled process; latency rose while CPU use sat at the quota.",
                [verify(settings, "single_api_process")],
                "Each worker costs about the API's memory again (about 100 MB here); SQLite WAL allows concurrent "
                "readers, writes stay serialised.", "small to medium",
                ["API health p95 under the same CPU quota at most 3 x baseline p95",
                 "throttled ratio under the quota below 0.5"],
                "Rerun the same resource_limit definition after the change and compare cgroup and latency figures.",
                "resource sizing"))
        elif kind.startswith("availability:"):
            out.append(_rec(
                finding, "Bound client retries and report unavailability explicitly",
                "Callers of the API (floor simulator, screens, agent) retry with bounded backoff and surface "
                "'unavailable' instead of failing silently; the lab measured the failures but not how clients "
                "handled them.", "Requests failed while the API was affected.", [],
                "Retries add load at recovery time; backoff and a cap limit it.", "medium",
                ["no client error escapes without a retry during a 10 s API outage",
                 "API success ratio back to baseline within 5 s of the API answering"],
                "Rerun the same fault and compare authenticated success and recovery time.", "error handling"))
        elif kind.startswith("recovery:"):
            out.append(_rec(
                finding, "Investigate why recovery is slow or incomplete before tuning",
                "Read the component logs in the recovery window and add a readiness signal for the failing "
                "criterion; tune timeouts only once the cause is known.",
                "Recovery evidence did not reach baseline within the observed window.", [],
                "None until a cause is identified.", "unknown",
                ["the failing recovery criterion passes within 10 s of the fault ending"],
                "Repeat the same experiment with longer post-recovery sampling.", "observability"))
    gaps = analysis.get("evidence_coverage", {}).get("missing", [])
    if gaps:
        out.append({"finding_id": None, "area": "observability", "title": "Collect the evidence this analysis lacked",
                    "remediation": "; ".join(gaps), "addresses": "conclusions above are limited by these gaps",
                    "locations": [], "unverified_locations": [], "tradeoffs": "measurement overhead only",
                    "effort": "small", "acceptance_criteria": ["the listed measurements are present in the next run"],
                    "manual_validation": "rerun with the measurements enabled and analyse again", "applied": False})
    return out
