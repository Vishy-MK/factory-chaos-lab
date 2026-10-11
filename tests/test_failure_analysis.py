"""Read-only failure impact analysis. Fixtures are SYNTHETIC (tests/analysis_fixtures.py)."""

import asyncio
import hashlib
import json

import pytest

from analysis_fixtures import T0, iso, outage_run, raw_sample, write_run
from chaoslab.agents.llm import LLMError, Reply
from chaoslab.analysis import baseline as baseline_module
from chaoslab.analysis import compare, engine, findings as findings_module, narrative, score, service
from chaoslab.analysis.evidence import ARTIFACT_FORMAT, EvidenceError, load, validate_artifact
from chaoslab.analysis.measure import counter_progress, percentile, window_metrics
from chaoslab.mcp_server import build_server
from test_agents import ScriptedLLM, answer, calls


@pytest.fixture
def config(settings):
    return engine.load_config(settings)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------- read-only enforcement


def test_analyst_role_holds_only_read_only_tools(lab_root):
    toolbox = narrative.ReadOnlyToolBox(build_server(lab_root), narrative.ROLE.tools)
    allowed = asyncio.run(toolbox._read_only())
    assert set(narrative.ROLE.tools) <= allowed
    assert not set(narrative.ROLE.tools) & narrative.FORBIDDEN_TOOLS
    assert not allowed & {"run_experiment", "emergency_stop", "plant_start", "plant_stop"}
    assert len(asyncio.run(toolbox.specs())) == len(narrative.ROLE.tools)


def test_a_role_with_a_mutating_tool_cannot_be_offered(lab_root):
    for tool in ("run_experiment", "emergency_stop", "plant_start", "plant_stop"):
        toolbox = narrative.ReadOnlyToolBox(build_server(lab_root), narrative.ROLE.tools + (tool,))
        with pytest.raises(PermissionError):
            asyncio.run(toolbox.specs())


def test_injection_and_plant_calls_are_refused_without_running(lab_root, settings, monkeypatch):
    from chaoslab.api import LabApi
    from chaoslab.lifecycle import Lifecycle

    def boom(*args, **kwargs):
        raise AssertionError("a mutating lab call was made")

    for owner, name in ((LabApi, "run"), (LabApi, "submit"), (LabApi, "emergency_stop"), (Lifecycle, "start"),
                        (Lifecycle, "stop")):
        monkeypatch.setattr(owner, name, boom)
    toolbox = narrative.ReadOnlyToolBox(build_server(lab_root), narrative.ROLE.tools)
    for tool, arguments in (("run_experiment", {"name": "api-process-outage-10s", "dry_run": True}),
                            ("emergency_stop", {}), ("plant_start", {}), ("plant_stop", {})):
        assert asyncio.run(toolbox.call(tool, arguments)).startswith("ERROR")
    llm = ScriptedLLM(calls(("run_experiment", {"name": "x", "dry_run": True}), ("emergency_stop", {})), answer({}))
    run = asyncio.run(narrative.Agent(narrative.ROLE, llm, toolbox, narrative.deny_all).run("go"))
    assert all(c["result"].startswith("ERROR") for c in run.transcript if c.get("tool"))
    assert not (settings.state_dir / "submissions").exists()


def test_the_whole_analysis_never_calls_the_controller(settings, config, monkeypatch):
    from chaoslab.api import LabApi
    from chaoslab.chaos.controller import Controller

    def boom(*args, **kwargs):
        raise AssertionError("the analysis called the experiment controller")

    for owner, name in ((Controller, "run"), (LabApi, "run"), (LabApi, "emergency_stop")):
        monkeypatch.setattr(owner, name, boom)
    outage_run(settings)
    result = service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)
    assert result["evidence_ok"]


# -------------------------------------------------------------------------------- baseline and compatibility


def test_healthy_baseline(settings, config):
    outage_run(settings)
    assessment = baseline_module.assess(load(settings, "api-outage-synthetic"), config)
    assert assessment["readiness"] == "healthy", assessment["checks"]
    assert assessment["window"]["samples"] == 20


def test_unhealthy_and_insufficient_baselines_are_not_treated_as_normal(settings, config):
    samples = [raw_sample(T0 + i, "baseline", tag=1000, up=i % 4 != 0) for i in range(20)]   # stalled + failing
    write_run(settings, "20261010T000001Z-sick-baseline-bbbbbb", samples, fault_type="none", component=None)
    sick = baseline_module.assess(load(settings, "sick-baseline"), config)
    assert sick["readiness"] == "unhealthy"
    assert {"api_success_ratio", "ingestion_advancing"} <= set(sick["failed_checks"])
    assert any("unhealthy" in n for n in sick["notes"])
    short = [raw_sample(T0 + i, "baseline", tag=1000 + 10 * i) for i in range(3)]
    write_run(settings, "20261010T000002Z-short-baseline-cccccc", short, fault_type="none", component=None)
    assert baseline_module.assess(load(settings, "short-baseline"), config)["readiness"] in ("insufficient", "unhealthy")


def test_missing_values_are_unmeasured_not_zero(settings, config):
    samples = [raw_sample(T0 + i, "baseline", tag=1000 + 10 * i, floor=None) for i in range(20)]
    write_run(settings, "20261010T000003Z-nofloor-dddddd", samples, fault_type="none", component=None)
    assessment = baseline_module.assess(load(settings, "nofloor"), config)
    check = next(c for c in assessment["checks"] if c["name"] == "floor_progressing")
    assert check["ok"] is None and check["detail"] == "not measured"
    assert "floor_age_max_s" in assessment["missing_measurements"]
    assert "floor_age_max_s" not in assessment["metrics"]


def test_compatibility_requires_matching_identity():
    base = {"measurement_method": "m", "plant": "bottling", "application_commit": "a", "application_version": "1",
            "deployment_mode": "lightweight", "workload": "baseline", "replay_speed": 1.0, "sampling_interval_s": 1.0}
    assert compare.compatibility(base, dict(base), same_source=False)["compatible"]
    other = compare.compatibility(base, {**base, "application_commit": "b"}, same_source=False)
    assert not other["compatible"] and other["incompatible_because"] == ["application_commit"]
    method = compare.compatibility(base, {**base, "measurement_method": "external"}, same_source=False)
    assert not method["compatible"]
    unknown = compare.compatibility(base, {**base, "replay_speed": None}, same_source=False)
    assert unknown["compatible"] and any("replay_speed" in q for q in unknown["qualifications"])


def test_incompatible_baseline_lowers_confidence_and_qualifies_the_score(settings):
    outage_run(settings)
    other = outage_run(settings, run_id="20261010T000004Z-other-commit-eeeeee", commit="different")
    assert other.is_dir()
    analysis = engine.analyse(settings, "api-outage-synthetic", baseline_ref="other-commit")
    assert not analysis["compatibility"]["compatible"]
    assert all(f["confidence"] == "low" for f in analysis["findings"])
    assert "baseline is not compatible with the run" in analysis["score"]["qualifications"]


# ----------------------------------------------------------------------------------------- comparisons


def test_nearest_rank_percentiles_and_sample_qualification(config):
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 0.5) == 10 and percentile(values, 0.95) == 19 and percentile(values, 1.0) == 20
    assert percentile([], 0.5) is None
    few = window_metrics([{**s, "label": "fault"} for s in []], config)
    assert few["window"]["samples"] == 0 and not few["window"]["adequate"]


def test_counter_and_replay_resets_are_segmented():
    progress = counter_progress([(0, 100), (1, 110), (2, 120), (3, 5), (4, 15), (5, 15), (6, 15), (7, 25)])
    assert progress["resets"] == 1 and progress["increase"] == 40
    assert progress["longest_stall_s"] == 2.0
    assert counter_progress([(0, 1)]) is None


def test_before_during_after_comparison(settings, config):
    outage_run(settings)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    during = analysis["comparisons"]["fault"]["metrics"]
    assert during["api_success_ratio"]["percentage_points"] == -100.0
    assert during["api_success_ratio"]["direction"] == "worse"
    assert during["api_latency_ms"]["available"] is False      # no successful checks during the outage
    assert during["tag_ingest_per_second"]["available"] is False
    post = analysis["comparisons"]["post"]["metrics"]
    assert post["api_success_ratio"]["percentage_points"] == 0.0
    assert analysis["observed_fault_duration_s"]["window_s"] == 10.0


def test_zero_baseline_has_no_relative_change():
    change = compare._change(0, 5, False, ratio=False)
    assert change["relative_change_pct"] is None and "zero" in change["note"]
    assert compare._change(None, 5, False, ratio=False)["available"] is False


def test_latency_percentiles_are_compared_per_window_not_averaged(config):
    base = window_metrics([{"t": T0 + i, **_norm(latency=10.0 + i % 3)} for i in range(20)], config)
    slow = window_metrics([{"t": T0 + 50 + i, **_norm(latency=100.0 + i)} for i in range(20)], config)
    result = compare.compare_windows(base, slow, 5)["metrics"]["api_latency_ms"]
    assert result["p95"]["baseline"] == 12.0 and result["p95"]["value"] == 118.0  # rank ceil(0.95 x 20) = 19
    assert "never averaged" in result["method"]


def _norm(latency):
    return {"label": "x", "api_up": True, "api_latency_ms": latency, "api_error": None, "auth_ok": True,
            "machines_total": 6, "machines_connected": 6, "tag_id": None, "floor_age_s": None,
            "telemetry_age_s": None, "target_reachable": None, "production": None, "database": None,
            "cgroups": None, "host_cpu_percent": None, "host_available_mb": None, "components": {}}


# --------------------------------------------------------------------------------------------- recovery


def test_recovery_time_is_measured_from_the_end_of_the_fault(settings):
    outage_run(settings, recover_after=4)
    timing = engine.analyse(settings, "api-outage-synthetic")["recovery"]["recovery_time"]
    assert timing["recovered"] is True and timing["seconds_after_fault_end"] == 4.5


def test_no_recovery_is_reported_and_scored_as_such(settings):
    outage_run(settings, never=True)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    assert analysis["recovery"]["recovery_time"]["recovered"] is False
    kinds = [f["kind"] for f in analysis["findings"]]
    assert "recovery:not_recovered" in kinds
    failed = next(f for f in analysis["findings"] if f["kind"] == "recovery:not_recovered")
    assert failed["severity"] == "critical"
    critical = [f["kind"] for f in analysis["findings"] if f["severity"] == "critical"]
    assert analysis["findings"][:len(critical)] == [f for f in analysis["findings"] if f["severity"] == "critical"]
    assert "recovery:not_recovered" in critical
    recovery = next(d for d in analysis["score"]["dimensions"] if d["name"] == "recovery")
    assert recovery["score"] == 0.0


# ----------------------------------------------------------------------------------- severity and scoring


def test_severity_levels_and_ordering_are_deterministic(settings, config):
    assert findings_module.level(9, config) == "critical" and findings_module.level(6, config) == "high"
    assert findings_module.level(3, config) == "medium" and findings_module.level(1, config) == "low"
    assert findings_module.level(0, config) == "info"
    outage_run(settings)
    first = engine.analyse(settings, "api-outage-synthetic")["findings"]
    second = engine.analyse(settings, "api-outage-synthetic")["findings"]
    assert [(f["id"], f["kind"], f["severity"]) for f in first] == [(f["id"], f["kind"], f["severity"]) for f in second]
    order = [findings_module.LEVEL_ORDER[f["severity"]] for f in first]
    assert order == sorted(order, reverse=True)
    availability = next(f for f in first if f["kind"] == "availability:api_success_ratio")
    assert availability["severity"] in ("high", "critical") and availability["severity_rationale"]


def test_severity_and_confidence_are_separate(settings):
    outage_run(settings, injected=True)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    analysis["inputs"]["experiment"]["fault"]["activation"]["status"] = "uncertain"
    found = findings_module.evaluate(analysis, engine.load_config(settings))
    serious = next(f for f in found if f["kind"] == "availability:api_success_ratio")
    assert serious["severity"] in ("high", "critical") and serious["confidence"] == "low"


def test_score_with_full_evidence_reproduces_the_formula(settings):
    outage_run(settings)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    result = analysis["score"]
    dims = {d["name"]: d for d in result["dimensions"]}
    if result["overall"]["status"] == "insufficient_evidence":
        assert result["overall"]["missing_dimensions"]
        assert all(dims[m]["score"] is None for m in result["overall"]["missing_dimensions"])
    else:
        expected = sum(result["weights"][n] * dims[n]["score"] for n in dims) / sum(result["weights"].values())
        assert result["overall"]["score"] == round(expected, 1)


def test_missing_evidence_never_improves_the_score(settings, config):
    outage_run(settings)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    analysis["windows"]["fault"] = {"window": {"samples": 0}, "metrics": {}, "unavailable": []}
    result = score.compute(analysis, config)
    availability = next(d for d in result["dimensions"] if d["name"] == "availability")
    assert availability["score"] is None and "fault-active" in availability["missing"]
    assert result["overall"]["status"] == "insufficient_evidence" and result["overall"]["score"] is None
    assert "availability" in result["overall"]["missing_dimensions"]
    assert result["evidence_coverage"] < 1.0


def test_before_and_after_only_cannot_score_the_fault(settings):
    samples = [raw_sample(T0 + i, "baseline", tag=1000 + 15 * i) for i in range(20)]
    samples += [raw_sample(T0 + 60 + i, "post", tag=3000 + 15 * i) for i in range(20)]
    write_run(settings, "20261010T000005Z-snapshots-ffffff", samples, fault_start=None)
    analysis = engine.analyse(settings, "snapshots")
    assert analysis["inputs"]["experiment"]["fault"]["activation"]["status"] == "uncertain"
    assert any("fault-active measurements" in m for m in analysis["evidence_coverage"]["missing"])
    assert analysis["score"]["overall"]["status"] == "insufficient_evidence"


# ------------------------------------------------------------------- structured output and evidence refs


def test_evidence_references_resolve_or_are_rejected(settings):
    outage_run(settings)
    analysis = engine.analyse(settings, "api-outage-synthetic")
    evidence = load(settings, "api-outage-synthetic")
    ref = analysis["inputs"]["experiment"]["ref"]
    assert analysis["evidence_check"]["ok"]
    for good in ("analysis#comparisons.fault.metrics.api_success_ratio", f"{ref}#samples",
                 f"{ref}#recovery.stages.data_consistency", "analysis#recovery.evidence.integrity_check"):
        assert engine.resolve_ref(good, analysis, evidence, settings), good
    for bad in ("analysis#comparisons.fault.metrics.invented", "run:20991231T000000Z-nope-000000#samples",
                f"{ref}#no.such.path", "made up", f"{ref}#logs/missing.log"):
        assert not engine.resolve_ref(bad, analysis, evidence, settings), bad


def _valid_narrative(analysis):
    finding = analysis["findings"][0]
    return {"analysis_id": analysis["analysis_id"], "summary": "The API was unavailable for the fault window.",
            "baseline_assessment": "healthy",
            "impact": [{"statement": "API checks failed during the fault", "kind": "observation",
                        "evidence": ["analysis#comparisons.fault.metrics.api_success_ratio"]}],
            "weakness_notes": [{"finding_id": finding["id"], "interpretation": "as measured",
                                "evidence": finding["evidence"]}],
            "agrees_with_controller": True, "disagreements": [], "cannot_conclude": [], "missing_evidence": []}


def test_narrative_schema_validation(settings):
    outage_run(settings)
    directory = service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)["directory"]
    stored = json.loads((__import__("pathlib").Path(directory) / "analysis.json").read_text())
    evidence = load(settings, "api-outage-synthetic")
    resolve = lambda ref: engine.resolve_ref(ref, stored, evidence, settings)  # noqa: E731
    assert narrative.validate(_valid_narrative(stored), stored, resolve) == []
    broken = _valid_narrative(stored)
    broken["impact"][0]["kind"] = "certain_root_cause"
    broken["weakness_notes"][0]["finding_id"] = "W99"
    broken["impact"].append({"statement": "x", "kind": "observation", "evidence": ["analysis#invented"]})
    problems = narrative.validate(broken, stored, resolve)
    assert any("kind" in p for p in problems) and any("W99" in p or "finding_id" in p for p in problems)
    assert any("does not resolve" in p for p in problems)
    assert narrative.validate("prose", stored, resolve)


def _scripted_narrative(stored, *final_answers):
    replies = []
    for final in final_answers:
        replies += [calls(("get_analysis", {"analysis_id": stored["analysis_id"], "section": "summary"}),
                          ("get_analysis", {"analysis_id": stored["analysis_id"], "section": "refs"})),
                    answer(final)]
    return ScriptedLLM(*replies)


def test_narrative_retries_until_valid_then_completes(lab_root, settings):
    outage_run(settings)
    directory = __import__("pathlib").Path(
        service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)["directory"])
    stored = json.loads((directory / "analysis.json").read_text())
    evidence = load(settings, "api-outage-synthetic")
    bad = {**_valid_narrative(stored), "impact": [{"statement": "x", "kind": "observation",
                                                   "evidence": ["analysis#made.up"]}]}
    llm = _scripted_narrative(stored, bad, _valid_narrative(stored))
    result = narrative.run(lab_root, stored, lambda r: engine.resolve_ref(r, stored, evidence, settings), llm=llm)
    assert result["status"] == "complete" and len(result["attempts"]) == 2
    assert result["attempts"][0]["problems"]
    assert "get_analysis" in result["attempts"][1]["tool_calls"]


def test_llm_failure_keeps_the_deterministic_analysis(lab_root, settings):
    outage_run(settings)

    class Down:
        async def chat(self, messages, tools):
            raise LLMError("provider unreachable")

    result = service.analyse_failure(settings, "api-outage-synthetic", llm=Down())
    assert result["narrative_status"] == "incomplete"
    directory = __import__("pathlib").Path(result["directory"])
    stored = json.loads((directory / "analysis.json").read_text())
    assert stored["findings"] and stored["score"]["overall"]["status"]
    saved = json.loads((directory / "narrative.json").read_text())
    assert saved["status"] == "incomplete" and len(saved["attempts"]) == 3
    assert "Narrative incomplete" in (directory / "report.md").read_text()


def test_invalid_answers_end_incomplete_within_bounds(lab_root, settings):
    outage_run(settings)
    directory = __import__("pathlib").Path(
        service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)["directory"])
    stored = json.loads((directory / "analysis.json").read_text())
    llm = _scripted_narrative(stored, {"no": 1}, {"no": 2}, {"no": 3})
    result = narrative.run(lab_root, stored, lambda r: False, llm=llm)
    assert result["status"] == "incomplete" and len(result["attempts"]) == 3


# ------------------------------------------------------------------------------ inputs and preservation


def test_original_experiment_records_are_never_modified(settings):
    directory = outage_run(settings)
    before = {p.name: digest(p) for p in directory.iterdir()}
    result = service.analyse_failure(settings, "api-outage-synthetic", with_narrative=False)
    assert {p.name: digest(p) for p in directory.iterdir()} == before
    assert "/analysis/" in result["directory"]
    assert not (directory / "analysis.json").exists()


def test_artifact_format_is_validated(settings, tmp_path, config):
    assert validate_artifact({"format": "x"})
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"format": ARTIFACT_FORMAT, "measurement_method": "m",
                               "samples": [{"t": 1.0, "surprise": 1}]}))
    with pytest.raises(EvidenceError):
        load(settings, str(bad))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"format": ARTIFACT_FORMAT, "measurement_method": "m",
                                "samples": [{"t": T0 + i, "api_up": True, "api_latency_ms": 5.0} for i in range(10)],
                                "fault": {"type": "external_thing", "timing": {"active_from": T0 + 3,
                                                                               "active_until": T0 + 6}},
                                "windows": {"baseline": {"start": T0, "end": T0 + 2.9}}}))
    evidence = load(settings, str(good))
    assert evidence.kind == "measurement_artifact" and len(evidence.window_samples("fault")) == 4


def test_supplied_fault_metadata_never_confirms_activation(settings, tmp_path):
    samples = [raw_sample(T0 + i, "baseline" if i < 20 else "post", tag=1000 + 15 * i,
                          up=not 20 <= i < 25) for i in range(40)]
    write_run(settings, "20261010T000006Z-unrecorded-a1b2c3", samples, fault_type="none", component=None)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"type": "process_outage", "target": {"component": "api"},
                                "timing": {"active_from": iso(T0 + 20), "active_until": iso(T0 + 25)}}))
    result = service.analyse_failure(settings, "unrecorded", fault_meta=str(meta), with_narrative=False)
    assert result["attribution"] == "uncertain"
    stored = json.loads(open(result["analysis"]).read())
    assert stored["score"]["overall"]["status"] == "insufficient_evidence"
    assert any("supplied" in n for n in stored["limitations"])


def test_external_opcua_report_is_normalised(settings, tmp_path):
    def sample(t, phase, ok, connected, max_id, age, count):
        return {"t_s": t, "at_utc": iso(T0 + t), "phase": phase,
                "probe": {"ok": ok, "values": {"MIX01/GoodCount": {"value": count}} if ok else {}},
                "mes": {"ok": True, "machines": 2, "connected": connected, "plant": "demo"},
                "telemetry": {"max_id": max_id, "stalest_age_s": age}}
    rows = [sample(i, "BASELINE", True, 2, 100 + i, 3.0, 10 + i) for i in range(10)]
    rows += [sample(10 + i, "FAULT_OBSERVATION", False, 0 if i > 3 else 2, 110, 5.0 + i * 2, 0) for i in range(10)]
    rows += [sample(20 + i, "RECOVERY_VALIDATION", True, 2 if i > 1 else 0, 111 + i, 3.0, i) for i in range(8)]
    report = {"schema_version": 1, "run_id": "synthetic01", "samples": rows, "deployment_mode": "real_mes",
              "timestamps": {"started_utc": iso(T0), "baseline_start_utc": iso(T0), "baseline_end_utc": iso(T0 + 9),
                             "injection_start_utc": iso(T0 + 9.9), "fault_confirmed_utc": iso(T0 + 10),
                             "restored_utc": iso(T0 + 19.5), "recovery_end_utc": iso(T0 + 28)},
              "injection": {"adapter": "server_process", "component": "opc-sim",
                            "configured_fault_duration_s": 10, "actual_fault_duration_s": 9.5},
              "outcomes": {"injection": "succeeded", "cleanup": "clean"}, "recovery": {"outcome": "recovered"},
              "evidence": {"mes": {"commit": "abc", "version": "0.2.0"}}}
    path = tmp_path / "20261010T000000Z_opcua-synthetic_synthetic01.json"
    path.write_text(json.dumps(report))
    analysis = engine.analyse(settings, str(path))
    assert analysis["inputs"]["experiment"]["fault"]["activation"]["status"] == "confirmed_by_measurement"
    kinds = {f["kind"] for f in analysis["findings"]}
    assert "observation:machines" in kinds and "data:counter_reset" in kinds
    observation = next(f for f in analysis["findings"] if f["kind"] == "observation:machines")
    assert "not evidence that physical production stopped" in observation["likely_explanation"]
    assert analysis["score"]["overall"]["status"] == "insufficient_evidence"   # no API latency, no integrity check


def test_cli_baseline_and_analyze(lab_root, settings, capsys):
    from chaoslab.cli import main

    outage_run(settings)
    assert main(["--root", str(lab_root), "analysis", "baseline", "api-outage-synthetic"]) == 0
    assert "healthy" in capsys.readouterr().out
    assert main(["--root", str(lab_root), "analysis", "analyze", "api-outage-synthetic", "--no-narrative"]) == 0
    out = capsys.readouterr().out
    assert "analysis " in out and "W01" in out and "report " in out
    assert main(["--root", str(lab_root), "analysis", "show", "latest"]) == 0
    assert "# Failure impact analysis" in capsys.readouterr().out
