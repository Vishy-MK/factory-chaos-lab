"""Recovery assessment from the samples after the fault window, independent of the controller's own verdict."""

from __future__ import annotations

from chaoslab.analysis.evidence import Evidence


def _healthy(sample: dict, history: list[dict], config: dict) -> tuple[bool, list[str]]:
    """Whether one sample meets every recovery criterion the source measures; unmeasured ones are listed."""
    measurement = config["measurement"]
    unmeasured, ok = [], True
    if sample["api_up"] is None:
        unmeasured.append("api")
    else:
        ok &= bool(sample["api_up"])
    if sample["auth_ok"] is None:
        unmeasured.append("authenticated_operation")
    else:
        ok &= bool(sample["auth_ok"])
    if sample["machines_connected"] is None or not sample["machines_total"]:
        unmeasured.append("machines_connected")
    else:
        ok &= sample["machines_connected"] == sample["machines_total"]
    if sample["tag_id"] is None:
        unmeasured.append("ingestion_advancing")
    else:
        older = [h for h in history if h["tag_id"] is not None and sample["t"] - h["t"] >= measurement["ingest_window_s"]]
        ok &= bool(older) and sample["tag_id"] > older[-1]["tag_id"]
    if sample["floor_age_s"] is None:
        unmeasured.append("floor_progressing")
    else:
        ok &= sample["floor_age_s"] <= measurement["floor_age_limit_s"]
    if sample["telemetry_age_s"] is not None:
        ok &= sample["telemetry_age_s"] <= measurement["telemetry_stale_s"]
    return ok, unmeasured


def recovery_time(evidence: Evidence, config: dict) -> dict:
    fault_end = (evidence.windows.get("fault") or {}).get("end")
    if fault_end is None:
        return {"available": False, "reason": "no fault window: recovery time cannot be anchored"}
    stable = int(config["measurement"]["stable_samples"])
    after = [s for s in evidence.samples if s["t"] > fault_end]
    if not after:
        return {"available": False, "reason": "no samples after the fault window"}
    run, first_ok, unmeasured = 0, None, set()
    for index, sample in enumerate(after):
        history = [s for s in evidence.samples if s["t"] < sample["t"]]
        ok, missing = _healthy(sample, history, config)
        unmeasured.update(missing)
        if ok:
            run += 1
            first_ok = sample["t"] if run == 1 else first_ok
            if run >= stable:
                return {"available": True, "recovered": True, "seconds_after_fault_end": round(first_ok - fault_end, 2),
                        "stable_samples": stable, "criteria_unmeasured": sorted(unmeasured),
                        "method": f"first of {stable} consecutive samples after the fault window meeting every "
                                  "measured recovery criterion",
                        "samples_after_fault": len(after)}
        else:
            run, first_ok = 0, None
    return {"available": True, "recovered": False, "seconds_after_fault_end": None, "stable_samples": stable,
            "criteria_unmeasured": sorted(unmeasured), "samples_after_fault": len(after),
            "observed_until_s": round(after[-1]["t"] - fault_end, 2),
            "method": "no stable healthy run was observed before the samples end"}


def assess(evidence: Evidence, baseline: dict, recovery_window: dict, post_window: dict | None,
           config: dict) -> dict:
    """Each line of recovery evidence with its source. Missing evidence is None, never assumed ok."""
    timing = recovery_time(evidence, config)
    stages = evidence.controller.get("stages") or {}
    items = []

    def item(name, ok, detail, ref=None):
        items.append({"name": name, "ok": ok, "detail": detail, **({"ref": ref} if ref else {})})

    target = post_window if post_window and post_window["window"]["samples"] else recovery_window
    label = "post-recovery" if target is post_window else "recovery"
    m, b = target["metrics"], baseline["metrics"]
    auth = m.get("authenticated_success_ratio")
    item("operations_resumed", None if auth is None else auth["value"] >= 0.99,
         "not measured" if auth is None else f"authenticated success {auth['value']} in the {label} window")
    machines = m.get("machines_all_connected_ratio")
    item("machines_reconnected", None if machines is None else machines["value"] == 1.0,
         "not measured" if machines is None else f"all machines connected in {machines['value'] * 100:.0f}% of "
                                                 f"{label} samples")
    rate, base_rate = m.get("tag_ingest_per_second"), b.get("tag_ingest_per_second")
    item("ingestion_advancing", None if rate is None else (rate["value"] or 0) > 0,
         "not measured" if rate is None else f"{rate['value']} tags/s ({label}) vs baseline "
                                             f"{(base_rate or {}).get('value')}")
    stale = m.get("telemetry_stale_ratio")
    if stale is not None:
        item("telemetry_fresh", stale["value"] == 0.0, f"stale telemetry in {stale['value'] * 100:.0f}% of {label} samples")
    floor = m.get("floor_age_max_s")
    item("floor_progressing", None if floor is None else floor["value"] <= config["measurement"]["floor_age_limit_s"],
         "not measured" if floor is None else f"floor last check at most {floor['value']} s ago ({label})")
    latency, base_latency = m.get("api_latency_ms"), b.get("api_latency_ms")
    if latency and base_latency:
        bound = base_latency["p95"] * 2 + 10
        item("api_latency_near_baseline", latency["p95"] <= bound,
             f"{label} p95 {latency['p95']} ms vs baseline p95 {base_latency['p95']} ms (bound {bound:.1f} ms = "
             f"2 x baseline + 10 ms)")
    else:
        item("api_latency_near_baseline", None, "API latency not measured in both windows")
    db, base_db = m.get("db_statement_ms_mean"), b.get("db_statement_ms_mean")
    if db and base_db:
        bound = base_db["value"] + max(25.0, 0.5 * base_db["value"])
        item("database_latency_near_baseline", db["value"] <= bound,
             f"{label} mean statement {db['value']} ms vs baseline {base_db['value']} ms (bound {bound:.3f} ms)")
    injected = m.get("db_injected_statements")
    if injected is not None:
        item("injection_inactive", injected["value"] == 0, f"{injected['value']} delayed statements in the {label} window")
    cleanup = evidence.controller.get("cleanup") or {}
    removal = [step.get("removed") for step in cleanup.get("steps") or [] if step.get("removed")]
    if removal:
        item("removal_verified", all(r.get("ok") for r in removal), "; ".join(r.get("detail", "") for r in removal),
             f"{evidence.ref}#cleanup.steps")
    elif cleanup.get("ok") is not None:
        item("removal_verified", bool(cleanup.get("ok")), "the injecting system reported cleanup "
             f"{'ok' if cleanup.get('ok') else 'failed'}; no separate removal check recorded",
             f"{evidence.ref}#cleanup" if evidence.kind == "lab_result" else f"{evidence.ref}#outcomes")
    else:
        item("removal_verified", None, "no cleanup or removal record")
    consistency = stages.get("data_consistency") if isinstance(stages, dict) else None
    if consistency:
        item("integrity_check", bool(consistency.get("ok")), consistency.get("detail", ""),
             f"{evidence.ref}#recovery.stages.data_consistency")
    else:
        item("integrity_check", None, "no database integrity check recorded")
    unknown = [i["name"] for i in items if i["ok"] is None]
    failed = [i["name"] for i in items if i["ok"] is False]
    if timing.get("available") and timing.get("recovered") and not failed:
        verdict = "recovered" if not unknown else "recovered_partially_evidenced"
    elif timing.get("available") and not timing.get("recovered"):
        verdict = "not_recovered"
    elif failed:
        verdict = "recovered_with_findings" if timing.get("recovered") else "not_recovered"
    else:
        verdict = "insufficient_evidence"
    return {
        "verdict": verdict,
        "recovery_time": timing,
        "evidence": items,
        "failed": failed,
        "unmeasured": unknown,
        "controller": {"overall_result": evidence.controller.get("overall_result"),
                       "recovered": evidence.controller.get("recovered"),
                       "recovery_duration_s": evidence.controller.get("recovery_duration_s"),
                       "note": "the controller measures recovery from the end of cleanup; this assessment measures "
                               "from the end of the fault window, so the two durations differ by the cleanup time"},
        "cautions": ["process liveness alone is not evidence of recovery and is not used as such",
                     "a passing integrity check shows the database file is consistent; it does not show that no "
                     "business operation was lost during the fault"],
    }
