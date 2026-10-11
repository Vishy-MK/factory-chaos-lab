"""Weaknesses and positive findings from deterministic comparisons, ranked with the documented severity rubric.

Severity comes from points (config/analysis.yaml `severity`); confidence is judged separately from evidence
quality. A finding is only produced when a measured comparison supports it.
"""

from __future__ import annotations

from typing import Any

LEVEL_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
CONFIDENCE_ORDER = {"high": 2, "medium": 1, "low": 0}


def _points(table: list, value: float | None) -> int:
    if value is None:
        return 0
    for threshold, points in table:
        if value >= threshold:
            return int(points)
    return 0


def level(points: int, config: dict) -> str:
    for name in ("critical", "high", "medium", "low"):
        if points >= int(config["severity"]["levels"][name]):
            return name
    return "info"


class Context:
    def __init__(self, analysis: dict, config: dict):
        self.analysis = analysis
        self.config = config
        self.ref = analysis["inputs"]["experiment"]["ref"]
        self.fault = analysis["inputs"]["experiment"]["fault"]
        self.during = analysis["comparisons"].get("fault") or {}
        self.after = analysis["comparisons"].get("post") or analysis["comparisons"].get("recovery") or {}
        self.windows = analysis["windows"]
        self.baseline_ok = analysis["baseline"]["readiness"] == "healthy"
        self.compatible = analysis["compatibility"]["compatible"]
        self.activation = self.fault["activation"]["status"]

    def metric(self, phase: str, name: str) -> dict:
        source = self.during if phase == "fault" else self.after
        return (source.get("metrics") or {}).get(name) or {}

    def window_metric(self, window: str, name: str) -> Any:
        return ((self.windows.get(window) or {}).get("metrics") or {}).get(name)

    def confidence(self, direct: bool, qualified: bool) -> tuple[str, list[str]]:
        reasons = []
        score = 2 if direct else 1
        if not direct:
            reasons.append("downstream measurement only")
        if qualified:
            score -= 1
            reasons.append("small or truncated sample window")
        if not self.baseline_ok:
            score -= 1
            reasons.append(f"baseline {self.analysis['baseline']['readiness']}")
        if not self.compatible:
            score = 0
            reasons.append("baseline not compatible with the run")
        if self.activation in ("uncertain", "not_injected"):
            score = 0
            reasons.append(f"fault attribution {self.activation}")
        name = {2: "high", 1: "medium"}.get(max(score, 0), "low")
        return name, reasons


def _finding(ctx: Context, kind: str, component: str, dependency: str | None, symptom: str, comparison: dict,
             criteria: list[tuple[str, int, str]], refs: list[str], direct: bool, qualified: bool,
             explanation: str | None = None, explanation_status: str = "unverified",
             limitations: list[str] | None = None) -> dict:
    points = sum(p for _, p, _ in criteria)
    confidence, why = ctx.confidence(direct, qualified)
    return {
        "kind": kind, "type": "weakness", "experiment": ctx.ref, "component": component, "dependency": dependency,
        "symptom": symptom, "baseline_comparison": comparison,
        "severity": level(points, ctx.config), "severity_points": points,
        "severity_rationale": [{"criterion": c, "points": p, "detail": d} for c, p, d in criteria if p],
        "confidence": confidence, "confidence_rationale": why or ["direct measurement, healthy compatible baseline, "
                                                                  "confirmed fault window"],
        "evidence": refs, "limitations": limitations or [],
        "likely_explanation": explanation, "explanation_status": explanation_status if explanation else None,
    }


def evaluate(analysis: dict, config: dict) -> list[dict]:
    ctx = Context(analysis, config)
    rubric = config["severity"]
    findings: list[dict] = []
    fault_type = ctx.fault.get("type")
    target = (ctx.fault.get("target") or {}).get("component") or (ctx.fault.get("target") or {}).get("resource")
    has_fault_window = bool(ctx.during)
    recovery = analysis["recovery"]
    # With the OPC data source itself down, missing ingestion is lost observation, not an ingestion defect.
    observation_fault = (target == "opc-sim") or str(fault_type).startswith("opcua_")
    persist_note = []

    # -- availability --------------------------------------------------------------------------------------
    if has_fault_window:
        for metric, what in (("api_success_ratio", "MES API health"), ("authenticated_success_ratio",
                                                                       "authenticated operations")):
            change = ctx.metric("fault", metric)
            if change.get("available") and change.get("percentage_points", 0) <= -0.5:
                lost = -change["percentage_points"]
                after = ctx.metric("post", metric)
                persists = after.get("available") and after.get("percentage_points", 0) <= -0.5
                window_s = ((ctx.windows.get("fault") or {}).get("window") or {}).get("window_s") or 0
                degraded_s = round(window_s * lost / 100, 1)
                criteria = [("availability_loss", _points(rubric["availability_drop_points"], lost),
                             f"{lost} percentage points of {what} lost during the fault"),
                            ("functionality", int(rubric["core_service_points"]),
                             "the MES API did not answer" if metric == "api_success_ratio"
                             else "user-facing authenticated operations failed"),
                            ("duration", _points(rubric["duration_points"], degraded_s),
                             f"about {degraded_s} s of lost {what} inside the fault window")]
                if persists:
                    criteria.append(("duration", rubric["persists_after_fault_points"], "still degraded after recovery"))
                findings.append(_finding(
                    ctx, f"availability:{metric}", "api", target, f"{what} success fell from "
                    f"{change['baseline']} to {change['value']} during the fault",
                    {"metric": metric, **change}, criteria,
                    [f"analysis#comparisons.fault.metrics.{metric}", f"{ctx.ref}#samples"], direct=False,
                    qualified=bool(change.get("qualified")),
                    explanation=(f"requests to the API failed while {target} was affected by {fault_type}"
                                 if target else None), explanation_status="plausible"))

    # -- latency ---------------------------------------------------------------------------------------------
    latency = ctx.metric("fault", "api_latency_ms")
    if latency.get("available"):
        p95 = latency["p95"]
        ratio = p95.get("ratio")
        if ratio is not None and ratio >= 1.5:
            after = ctx.metric("post", "api_latency_ms")
            persists = after.get("available") and (after["p95"].get("ratio") or 0) >= 1.5
            window_s = ((ctx.windows.get("fault") or {}).get("window") or {}).get("window_s") or 0
            criteria = [("latency_degradation", _points(rubric["latency_ratio_points"], ratio),
                         f"API health p95 {p95['baseline']} -> {p95['value']} ms ({ratio}x)"),
                        ("duration", _points(rubric["duration_points"], window_s),
                         f"degraded for the {window_s} s fault window")]
            if persists:
                criteria.append(("duration", rubric["persists_after_fault_points"], "p95 still elevated after recovery"))
            explanation, status = None, None
            if fault_type == "database_latency":
                api_driver = (((ctx.window_metric("fault", "db_driver_ms_mean_by_component") or {}).get("value") or {})
                              .get("run-api") or {}).get("driver_ms_mean")
                base_driver = (((ctx.window_metric("baseline", "db_driver_ms_mean_by_component") or {}).get("value")
                                or {}).get("run-api") or {}).get("driver_ms_mean")
                explanation = ("each API request runs several database statements and every statement received the "
                               "injected delay; GET requests use read-only sessions (fsmes/api/deps.py get_db), so "
                               "the API's reads do not wait for SQLite's write lock")
                if api_driver is not None and base_driver is not None and api_driver <= base_driver + 5:
                    status = "supported_by_code_and_measurement"
                    explanation += (f"; the API's own driver time stayed at {api_driver} ms per statement "
                                    f"(baseline {base_driver} ms)")
                else:
                    status = "plausible"
            elif fault_type == "resource_limit":
                explanation = "the API process was CPU-throttled by its cgroup quota while serving requests"
                status = "supported_by_measurement"
            elif fault_type == "cpu_pressure":
                explanation = "the API shared its pinned CPU with the burner process"
                status = "plausible"
            findings.append(_finding(
                ctx, "latency:api", "api", target, f"API health-check p95 rose {ratio}x during the fault",
                {"metric": "api_latency_ms", "p50": latency["p50"], "p95": p95, "samples": latency["samples"]},
                criteria, ["analysis#comparisons.fault.metrics.api_latency_ms",
                           "analysis#windows.fault.metrics.api_latency_ms"], direct=False,
                qualified=bool(latency.get("qualified")), explanation=explanation, explanation_status=status or "plausible",
                limitations=["one health-check probe per sample: a narrow view of request latency",
                             "percentiles of a short window rest on few samples"]))

    # -- throughput and ingestion ----------------------------------------------------------------------------
    for metric, component, what in (("tag_ingest_per_second", "opc-agent", "tag ingestion"),
                                    ("db_statements_per_second", "database clients", "database statement rate")):
        if observation_fault:
            break
        change = ctx.metric("fault", metric)
        if change.get("available") and change.get("relative_change_pct") is not None \
                and change["relative_change_pct"] <= -15:
            drop = -change["relative_change_pct"]
            criteria = [("throughput_degradation", _points(rubric["throughput_drop_points"], drop),
                         f"{what} fell {drop}% ({change['baseline']} -> {change['value']})")]
            stall = ctx.metric("fault", "tag_ingest_stall_max_s") if metric == "tag_ingest_per_second" else {}
            if stall.get("available"):
                criteria.append(("ingestion_stall", _points(rubric["stall_points"], stall["value"]),
                                 f"longest ingestion stall {stall['value']} s"))
            explanation = None
            if metric == "db_statements_per_second" and fault_type == "database_latency":
                explanation = ("statements are serialised per connection, so a fixed per-statement delay caps the "
                               "statement rate of each client")
            findings.append(_finding(
                ctx, f"throughput:{metric}", component, target, f"{what} dropped {drop}% during the fault",
                {"metric": metric, **change}, criteria, [f"analysis#comparisons.fault.metrics.{metric}"],
                direct=metric.startswith("db_"), qualified=bool(change.get("qualified")), explanation=explanation,
                explanation_status="plausible"))

    # -- observation loss: connectivity, telemetry, counters ------------------------------------------------
    connected = ctx.metric("fault", "machines_all_connected_ratio")
    stale = ctx.metric("fault", "telemetry_stale_ratio")
    resets = ctx.window_metric("run", "production_counter_resets") or {}
    lost_observation = (connected.get("available") and connected.get("percentage_points", 0) < 0) or \
        (stale.get("available") and (stale.get("value") or 0) > 0)
    if lost_observation:
        criteria = [("observation_loss", rubric["observation_loss_points"], "machines disconnected or telemetry "
                     "stale during the fault")]
        ingest = ctx.metric("fault", "tag_ingest_per_second")
        stall = ctx.metric("fault", "tag_ingest_stall_max_s")
        if stall.get("available"):
            criteria.append(("ingestion_stall", _points(rubric["stall_points"], stall["value"]),
                             f"no new telemetry for up to {stall['value']} s"))
        reconnect = recovery.get("recovery_time", {}).get("seconds_after_fault_end")
        if reconnect is not None and reconnect >= float(rubric["slow_recovery_s"]):
            criteria.append(("recovery_time", rubric["slow_recovery_points"], f"{reconnect} s to recover"))
        symptom = []
        if connected.get("available"):
            symptom.append(f"all machines connected in {connected['value'] * 100:.0f}% of fault samples "
                           f"(baseline {connected['baseline'] * 100:.0f}%)")
        if stale.get("available"):
            symptom.append(f"telemetry stale in {stale['value'] * 100:.0f}% of fault samples")
        findings.append(_finding(
            ctx, "observation:machines", "opc-agent", "opc-sim (OPC UA)", "; ".join(symptom),
            {"machines_all_connected_ratio": connected, "telemetry_stale_ratio": stale,
             "tag_ingest_per_second": ingest, "tag_ingest_stall_max_s": stall}, criteria,
            ["analysis#comparisons.fault.metrics.machines_all_connected_ratio"] +
            (["analysis#comparisons.fault.metrics.telemetry_stale_ratio"] if stale.get("available") else []),
            direct=fault_type in ("process_outage", "opcua_server_process") and target in ("opc-sim", None),
            qualified=bool(connected.get("qualified")),
            explanation="the MES lost sight of the machines: this is lost observation, not evidence that physical "
                        "production stopped", explanation_status="supported_by_measurement",
            limitations=["telemetry loss says nothing about what the line produced while unobserved"]))
    if resets.get("value"):
        findings.append(_finding(
            ctx, "data:counter_reset", "opc-sim", "production counters",
            f"production counters went backwards {resets['value']} time(s) after the fault",
            {"production_counter_resets": resets}, [("observation_loss", rubric["observation_loss_points"],
                                                     "counters restarted from zero after the server restart")],
            ["analysis#windows.run.metrics.production_counter_resets"], direct=True, qualified=False,
            explanation="the restarted OPC server lost its counter state; the MES must re-baseline rather than "
                        "book the drop", explanation_status="supported_by_measurement",
            limitations=["whether the MES booked the reset correctly is not shown by these samples"]))

    # -- database-specific ------------------------------------------------------------------------------------
    if fault_type == "database_latency":
        delayed = ctx.window_metric("fault", "db_delayed_statement_ms_mean")
        injected = ctx.window_metric("fault", "db_injected_delay_ms_mean")
        if delayed and injected and delayed["value"] is not None and injected["value"] is not None:
            extra = round(delayed["value"] - injected["value"], 1)
            by_component = (ctx.window_metric("fault", "db_driver_ms_mean_by_component") or {}).get("value") or {}
            base_by_component = (ctx.window_metric("baseline", "db_driver_ms_mean_by_component") or {}).get("value") or {}
            if extra >= 10:
                criteria = [("latency_degradation", 1, f"delayed statements took {extra} ms more than the "
                             f"{injected['value']} ms injected"),
                            ("scope", rubric["scope_points_per_extra_area"], "extra waiting spreads beyond the delay")]
                findings.append(_finding(
                    ctx, "database:amplification", "database clients", "SQLite plant database",
                    f"delayed statements took {delayed['value']} ms on average against {injected['value']} ms "
                    "injected", {"delayed_statement_ms_mean": delayed, "injected_delay_ms_mean": injected,
                                 "driver_ms_mean_by_component": by_component,
                                 "baseline_driver_ms_mean_by_component": base_by_component}, criteria,
                    ["analysis#windows.fault.metrics.db_delayed_statement_ms_mean",
                     "analysis#windows.fault.metrics.db_driver_ms_mean_by_component"], direct=True, qualified=False,
                    explanation=("writer transactions begin with BEGIN IMMEDIATE (fsmes/db.py _sqlite_transactions) "
                                 "and keep SQLite's write lock while their delayed statements sleep, so other writers "
                                 "queue behind them; GET requests use read-only sessions and do not take that lock"),
                    explanation_status="unverified",
                    limitations=["driver time per component is a mean; the hook does not record lock waits directly",
                                 "this is amplification of the injected delay, not the separate sqlite_write_lock "
                                 "fault"]))
        tail = ctx.window_metric("recovery", "db_delayed_statement_ms_mean")
        tail_count = ctx.window_metric("recovery", "db_injected_statements")
        if tail and injected and tail["value"] is not None and injected["value"] and tail_count and tail_count["value"] \
                and tail["value"] >= 3 * injected["value"]:
            findings.append(_finding(
                ctx, "database:tail", "database clients", "SQLite plant database",
                f"{tail_count['value']} delayed statements completed after the fault window, taking {tail['value']} ms "
                f"each against {injected['value']} ms injected",
                {"recovery_delayed_statement_ms_mean": tail, "recovery_delayed_statements": tail_count},
                [("duration", rubric["persists_after_fault_points"], "slow statements outlasted the fault window")],
                ["analysis#windows.recovery.metrics.db_delayed_statement_ms_mean"], direct=True, qualified=False,
                explanation=("statements that started inside the window queued behind other delayed write "
                             "transactions and finished long after the delay ended"), explanation_status="unverified",
                limitations=["counted when a statement completes, so a statement is attributed to the window it ends in"]))
        errors = ctx.window_metric("fault", "db_errors")
        if errors and errors["value"]:
            findings.append(_finding(
                ctx, "database:errors", "database clients", "SQLite plant database",
                f"{errors['value']} database errors during the fault",
                {"db_errors": errors, "db_lock_errors": ctx.window_metric("fault", "db_lock_errors")},
                [("functionality", 3, "database operations failed")], ["analysis#windows.fault.metrics.db_errors"],
                direct=True, qualified=False))

    if fault_type == "resource_limit":
        cg = ((ctx.window_metric("fault", "cgroup") or {}).get("value") or {}).get(target) or {}
        ratio = cg.get("cpu_throttled_ratio")
        if ratio:
            findings.append(_finding(
                ctx, "resources:cpu_throttling", target, "cgroup cpu.max",
                f"{target} was throttled in {ratio * 100:.0f}% of scheduler periods at "
                f"{cg.get('cpu_percent_mean')}% CPU", {"cgroup": cg},
                [("latency_degradation", 1 if ratio >= 0.5 else 0, "most scheduler periods throttled")],
                ["analysis#windows.fault.metrics.cgroup"], direct=True, qualified=False,
                explanation="the component needs more CPU in bursts than the quota allows",
                explanation_status="supported_by_measurement"))
        if cg.get("oom_kills"):
            findings.append(_finding(
                ctx, "resources:oom_kill", target, "cgroup memory.max", f"{target} was OOM-killed "
                f"{cg['oom_kills']} time(s)", {"cgroup": cg}, [("availability_loss", 4, "component killed")],
                ["analysis#windows.fault.metrics.cgroup"], direct=True, qualified=False))

    # -- recovery ---------------------------------------------------------------------------------------------
    timing = recovery.get("recovery_time") or {}
    if timing.get("available") and timing.get("recovered") is False:
        findings.append(_finding(
            ctx, "recovery:not_recovered", target or "plant", None,
            f"no stable healthy run within {timing.get('observed_until_s')} s after the fault window",
            {"recovery_time": timing}, [("recovery_failure", rubric["recovery_failed_points"], "did not recover")],
            ["analysis#recovery.recovery_time"], direct=False, qualified=False))
    elif timing.get("recovered") and timing.get("seconds_after_fault_end", 0) >= float(rubric["slow_recovery_s"]):
        findings.append(_finding(
            ctx, "recovery:slow", target or "plant", None,
            f"recovery took {timing['seconds_after_fault_end']} s after the fault window",
            {"recovery_time": timing}, [("recovery_time", rubric["slow_recovery_points"], "slow recovery")],
            ["analysis#recovery.recovery_time"], direct=False, qualified=False))
    for item in recovery.get("evidence", []):
        if item["ok"] is False and item["name"] in ("api_latency_near_baseline", "database_latency_near_baseline",
                                                    "ingestion_advancing", "machines_reconnected"):
            persist_note.append(item)
    for item in persist_note:
        findings.append(_finding(
            ctx, f"recovery:{item['name']}", target or "plant", None, f"after recovery: {item['detail']}",
            {"recovery_evidence": item}, [("duration", rubric["persists_after_fault_points"],
                                           "not back near baseline after recovery")],
            [f"analysis#recovery.evidence.{item['name']}"], direct=False, qualified=False))

    # -- scope ------------------------------------------------------------------------------------------------
    weaknesses = [f for f in findings if f["type"] == "weakness"]
    areas = {f["kind"].split(":")[0] for f in weaknesses}
    if len(areas) > 1:
        extra = (len(areas) - 1) * int(rubric["scope_points_per_extra_area"])
        for f in weaknesses:
            if f["kind"].split(":")[0] in ("availability", "latency", "throughput"):
                f["severity_points"] += extra
                f["severity_rationale"].append({"criterion": "scope", "points": extra,
                                                "detail": f"{len(areas)} areas affected: {sorted(areas)}"})
                f["severity"] = level(f["severity_points"], config)

    findings += _positives(ctx, analysis)
    findings.sort(key=lambda f: (-LEVEL_ORDER[f["severity"]], -f.get("severity_points", 0),
                                 -CONFIDENCE_ORDER.get(f["confidence"], 0), f["kind"]))
    for index, finding in enumerate(findings, 1):
        finding["id"] = f"{'W' if finding['type'] == 'weakness' else 'P'}{index:02d}"
    return findings


def _positive(ctx: Context, kind: str, statement: str, refs: list[str], direct: bool, qualified: bool = False) -> dict:
    confidence, why = ctx.confidence(direct, qualified)
    return {"kind": kind, "type": "positive", "experiment": ctx.ref, "symptom": statement, "severity": "info",
            "severity_points": 0, "severity_rationale": [], "confidence": confidence,
            "confidence_rationale": why or ["direct measurement"], "evidence": refs, "limitations": [],
            "likely_explanation": None, "explanation_status": None}


def _positives(ctx: Context, analysis: dict) -> list[dict]:
    out = []
    for metric in ("api_success_ratio", "authenticated_success_ratio"):
        change = ctx.metric("fault", metric)
        if change.get("available") and change["value"] >= 0.999 and not change.get("qualified"):
            out.append(_positive(ctx, f"held:{metric}", f"{metric} stayed at {change['value']} during the fault",
                                 [f"analysis#comparisons.fault.metrics.{metric}"], direct=False))
    change = ctx.metric("fault", "tag_ingest_per_second")
    if change.get("available") and change.get("relative_change_pct") is not None and change["relative_change_pct"] > -15:
        out.append(_positive(ctx, "held:ingestion", f"tag ingestion continued at {change['value']} tags/s "
                             f"({change['relative_change_pct']}% vs baseline)",
                             ["analysis#comparisons.fault.metrics.tag_ingest_per_second"], direct=False))
    errors = ctx.window_metric("fault", "db_errors")
    if errors is not None and errors["value"] == 0:
        out.append(_positive(ctx, "held:database_errors", "no database errors during the fault",
                             ["analysis#windows.fault.metrics.db_errors"], direct=True))
    timing = analysis["recovery"].get("recovery_time") or {}
    if timing.get("recovered") and timing.get("seconds_after_fault_end") is not None \
            and timing["seconds_after_fault_end"] < 10:
        out.append(_positive(ctx, "recovered:fast", f"stable recovery {timing['seconds_after_fault_end']} s after "
                             "the fault window", ["analysis#recovery.recovery_time"], direct=False))
    integrity = next((i for i in analysis["recovery"]["evidence"] if i["name"] == "integrity_check"), None)
    if integrity and integrity["ok"]:
        out.append(_positive(ctx, "integrity:ok", "database integrity check passed after the fault "
                             "(file consistency only)", [integrity.get("ref") or "analysis#recovery.evidence"],
                             direct=True))
    return out
