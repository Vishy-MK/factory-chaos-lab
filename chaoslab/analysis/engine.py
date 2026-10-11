"""Failure impact analysis pipeline. Read only with respect to the lab, the plant and experiment records.

The only writes are the analysis artifacts under results/analysis/<analysis-id>/ (and baseline assessments under
results/analysis/baselines/). Experiment records are read, never modified.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import yaml

from chaoslab.analysis import baseline as baseline_module
from chaoslab.analysis import compare, findings as findings_module, fixes, recovery as recovery_module, score
from chaoslab.analysis.evidence import Evidence, EvidenceError, apply_fault_metadata, load
from chaoslab.analysis.measure import METRICS, window_metrics
from chaoslab.settings import Settings

ENGINE_VERSION = "1"
DIRECT_PREFIXES = ("db_", "cgroup", "target_reachable")


def load_config(settings: Settings) -> dict:
    path = settings.root / "config" / "analysis.yaml"
    if not path.is_file():
        from chaoslab.settings import PACKAGE_ROOT

        path = PACKAGE_ROOT / "config" / "analysis.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _windows(evidence: Evidence, config: dict) -> dict:
    out = {}
    for name in ("baseline", "fault", "recovery", "post"):
        window = evidence.windows.get(name) or {}
        samples = evidence.window_samples(name)
        if not samples:
            out[name] = {"window": {"samples": 0, "start": window.get("start"), "end": window.get("end"),
                                    "basis": window.get("basis")}, "metrics": {}, "unavailable": sorted(METRICS)}
            continue
        measured = window_metrics(samples, config, window.get("start"), window.get("end"))
        measured["window"]["basis"] = window.get("basis")
        out[name] = measured
    return out


def _impact(analysis: dict) -> dict:
    """Confirmed observations from comparisons, split into direct measurements of the fault target and downstream
    effects. Interpretation lives in findings and the narrative, not here."""
    direct, downstream = [], []
    for phase in ("fault", "recovery", "post"):
        comparison = analysis["comparisons"].get(phase)
        if not comparison:
            continue
        for metric, change in comparison["metrics"].items():
            if not change.get("available"):
                continue
            if metric == "api_latency_ms":
                p95 = change["p95"]
                if p95.get("direction") == "worse" and (p95.get("ratio") or 0) >= 1.5:
                    downstream.append({"phase": phase, "metric": "api_latency_ms.p95", "baseline": p95["baseline"],
                                       "value": p95["value"], "ratio": p95["ratio"],
                                       "ref": f"analysis#comparisons.{phase}.metrics.api_latency_ms"})
                continue
            if change.get("direction") != "worse":
                continue
            significant = abs(change.get("percentage_points") or 0) >= 0.5 or \
                abs(change.get("relative_change_pct") or 0) >= 15 or \
                (change.get("baseline") == 0 and change.get("value"))
            if not significant:
                continue
            entry = {"phase": phase, "metric": metric, "baseline": change["baseline"], "value": change["value"],
                     **{k: change[k] for k in ("percentage_points", "relative_change_pct") if k in change},
                     "ref": f"analysis#comparisons.{phase}.metrics.{metric}"}
            (direct if metric.startswith(DIRECT_PREFIXES) else downstream).append(entry)
    for name in ("db_injected_statements", "cgroup"):
        metric = ((analysis["windows"].get("fault") or {}).get("metrics") or {}).get(name)
        if metric and metric.get("value"):
            direct.append({"phase": "fault", "metric": name, "value": metric["value"],
                           "ref": f"analysis#windows.fault.metrics.{name}"})
    confounders = []
    base_cpu = ((analysis["windows"]["baseline"]["metrics"]).get("host_cpu_percent_max") or {}).get("value")
    fault_cpu = (((analysis["windows"].get("fault") or {}).get("metrics") or {}).get("host_cpu_percent_max") or {}).get("value")
    if base_cpu is not None and fault_cpu is not None and fault_cpu - base_cpu >= 20:
        confounders.append(f"host CPU peaked at {fault_cpu}% during the fault vs {base_cpu}% in the baseline; "
                           "contention from other processes may contribute")
    workload = analysis["inputs"]["experiment"]["identity"].get("workload")
    if workload:
        confounders.append(f"the lab's '{workload}' workload ran throughout; its probes are part of the measured load")
    return {"direct": direct, "downstream": downstream, "confounders": confounders,
            "note": "direct = measured at the fault target (database hook, cgroup, target probe); downstream = "
                    "effects seen through the API, ingestion and connectivity"}


def _coverage(analysis: dict, evidence: Evidence) -> dict:
    windows = {name: analysis["windows"][name]["window"]["samples"] for name in analysis["windows"] if name != "run"}
    missing = []
    if not windows.get("fault"):
        missing.append("fault-active measurements: temporary errors and peak degradation during the fault cannot "
                       "be established from before/after snapshots")
    fault_type = evidence.fault.get("type")
    fault_metrics = (analysis["windows"].get("fault") or {}).get("metrics") or {}
    if fault_type == "database_latency" and "db_statement_ms_mean" not in fault_metrics:
        missing.append("direct database timings: database latency itself was not measured, only its downstream effects "
                       "(start the plant with the lab so the database hook loads)")
    if "api_latency_ms" not in fault_metrics and windows.get("fault"):
        missing.append("API request latency during the fault (the source reports API up/down only)")
    if "authenticated_success_ratio" not in fault_metrics and windows.get("fault"):
        missing.append("authenticated operation probes during the fault")
    base_metrics = set(analysis["windows"]["baseline"]["metrics"])
    if windows.get("fault"):
        tags_measured = any(x["tag_id"] is not None for x in evidence.samples)
        failed_probes = sum(1 for s in evidence.window_samples("fault") if s["api_up"] is False or
                            s["target_reachable"] is False or (tags_measured and s["tag_id"] is None))
        for metric in sorted(base_metrics - set(fault_metrics) - {"api_errors", "api_timeouts"}):
            missing.append(f"{metric} was measured in the baseline but not during the fault "
                           f"({failed_probes} of {windows['fault']} fault samples had failed or timed-out probes)")
    if evidence.fault["activation"]["status"] in ("uncertain", "not_injected"):
        missing.append("fault timing and activation evidence from the injecting system")
    if not analysis["recovery"]["recovery_time"].get("available"):
        missing.append("samples after a known fault window, to time recovery")
    measured = sorted({m for w in analysis["windows"].values() for m in w["metrics"]})
    return {"samples_per_window": windows, "measured_metrics": measured, "missing": missing,
            "activation": evidence.fault["activation"]}


def _limitations(analysis: dict, evidence: Evidence, config: dict) -> list[str]:
    notes = list(evidence.notes)
    notes.append(f"percentiles use the nearest-rank method within one window; windows with fewer than "
                 f"{config['measurement']['min_samples_per_window']} samples qualify every comparison made from them")
    if evidence.kind == "opcua_report":
        notes.append("the external report observes a different MES instance with its own methods; compare it only "
                     "with its own baseline window")
    if analysis["inputs"]["baseline"]["ref"] != evidence.ref:
        notes.append("the baseline comes from a different run: conditions can differ even when identities match")
    for qualification in analysis["compatibility"].get("qualifications", []):
        notes.append(f"compatibility: {qualification}")
    notes.append("the score describes resilience to this failure under the observed conditions only")
    return notes


# -------------------------------------------------------------------------------------- evidence references


def resolve_path(node: Any, path: str) -> bool:
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        elif isinstance(node, list):
            match = next((item for item in node if isinstance(item, dict) and part in (item.get("name"),
                                                                                       item.get("id"))), None)
            if match is None:
                if part.isdigit() and int(part) < len(node):
                    match = node[int(part)]
                else:
                    return False
            node = match
        else:
            return False
    return True


def resolve_ref(ref: str, analysis: dict, evidence: Evidence, settings: Settings) -> bool:
    """Whether a reference points at a real record: the analysis, the run's record, its samples or a log file."""
    if not isinstance(ref, str) or "#" not in ref:
        return False
    source, path = ref.split("#", 1)
    if source == "analysis":
        return resolve_path(analysis, path)
    if source != evidence.ref and source != analysis["inputs"]["baseline"]["ref"]:
        return False
    if path == "samples":
        return bool(evidence.samples)
    if path.startswith("logs/"):
        return any(log.endswith(path[len("logs/"):]) for log in evidence.logs)
    return resolve_path(evidence.record, path)


# ---------------------------------------------------------------------------------------------- the pipeline


def analyse(settings: Settings, experiment_ref: str, baseline_ref: str = "own", fault_meta: dict | None = None,
            config: dict | None = None) -> dict:
    config = config or load_config(settings)
    experiment = load(settings, experiment_ref)
    if fault_meta:
        apply_fault_metadata(experiment, fault_meta)
    if baseline_ref in (None, "", "own"):
        baseline_evidence, baseline_source = experiment, "own"
    else:
        try:
            stored = baseline_module.load_assessment(settings, baseline_ref)
            baseline_evidence = load(settings, stored["source"]["path"] if stored["source"]["kind"] != "lab_result"
                                     else stored["source"]["ref"].split(":", 1)[1])
            baseline_source = stored["baseline_id"]
        except EvidenceError:
            baseline_evidence, baseline_source = load(settings, baseline_ref), "run"
    same = baseline_evidence.ref == experiment.ref
    assessment = baseline_module.assess(baseline_evidence, config)
    timing = recovery_module.recovery_time(experiment, config)
    if not experiment.window_samples("post") and timing.get("recovered"):
        # No separate post-recovery window in the source: judge "after recovery" from the stable point onwards,
        # so the reconnect period itself is not counted against recovery.
        start = experiment.windows["fault"]["end"] + timing["seconds_after_fault_end"]
        end = max(s["t"] for s in experiment.samples)
        experiment.windows["post"] = {"start": start, "end": end,
                                      "basis": "derived: from the first stable healthy sample to the last sample"}
    windows = _windows(experiment, config)
    windows["run"] = window_metrics(experiment.samples, config)
    windows["run"]["window"]["basis"] = "every sample of the run (for resets that cross window boundaries)"
    base_samples, base_window, _ = baseline_module.baseline_samples(baseline_evidence)
    windows["baseline"] = window_metrics(base_samples, config, base_window.get("start"), base_window.get("end"))
    windows["baseline"]["window"]["basis"] = assessment["window"]["basis"]
    windows["baseline"]["window"]["source"] = baseline_evidence.ref
    min_samples = int(config["measurement"]["min_samples_per_window"])
    comparisons = {name: compare.compare_windows(windows["baseline"], windows[name], min_samples)
                   for name in ("fault", "recovery", "post") if windows[name]["window"]["samples"]}
    compatibility = compare.compatibility(baseline_evidence.identity, experiment.identity, same)
    analysis: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "inputs": {
            "experiment": experiment.summary(),
            "baseline": {"ref": baseline_evidence.ref, "source": baseline_source, "kind": baseline_evidence.kind,
                         "window": assessment["window"]},
            "fault_metadata_supplied": fault_meta or None,
        },
        "baseline": {k: assessment[k] for k in ("readiness", "checks", "failed_checks", "unmeasured_checks",
                                                "missing_measurements", "notes")},
        "compatibility": compatibility,
        "metric_definitions": {k: {"unit": u, "definition": d} for k, (u, d) in METRICS.items()},
        "windows": windows,
        "comparisons": comparisons,
        "observed_fault_duration_s": _observed_duration(experiment),
        "controller_verdict": experiment.controller,
    }
    post = windows["post"] if windows["post"]["window"]["samples"] else None
    analysis["recovery"] = recovery_module.assess(experiment, windows["baseline"], windows["recovery"], post, config)
    analysis["impact"] = _impact(analysis)
    analysis["evidence_coverage"] = _coverage(analysis, experiment)
    found = findings_module.evaluate(analysis, config)
    analysis["findings"] = found
    analysis["recommendations"] = fixes.recommend(settings, found, analysis)
    analysis["score"] = score.compute(analysis, config)
    analysis["limitations"] = _limitations(analysis, experiment, config)
    unresolved = [(f["id"], ref) for f in found for ref in f["evidence"]
                  if not resolve_ref(ref, analysis, experiment, settings)]
    analysis["evidence_check"] = {"unresolved": unresolved, "ok": not unresolved}
    analysis["config_digest"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    return analysis


def _observed_duration(evidence: Evidence) -> dict:
    timing = evidence.fault.get("timing") or {}
    start, end = timing.get("active_from"), timing.get("active_until")
    out = {"window_s": round(end - start, 2) if start is not None and end is not None else None,
           "basis": timing.get("basis"), "requested_s": evidence.fault.get("requested_duration_s")}
    if timing.get("actual_duration_s") is not None:
        out["reported_actual_s"] = timing["actual_duration_s"]
    return out


def persist(settings: Settings, analysis: dict, narrative: dict | None, report_text: str) -> Path:
    label = analysis["inputs"]["experiment"]["ref"].split(":", 1)[-1]
    analysis_id = baseline_module.new_id("analysis", label)
    directory = baseline_module.analysis_dir(settings) / analysis_id
    directory.mkdir(parents=True, exist_ok=True)
    analysis = {"analysis_id": analysis_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **analysis, "narrative": narrative}
    (directory / "analysis.json").write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
    (directory / "report.md").write_text(report_text.replace("{analysis_id}", analysis_id), encoding="utf-8")
    return directory


def read(settings: Settings, analysis_id: str) -> dict:
    root = baseline_module.analysis_dir(settings)
    matches = sorted(p for p in root.glob("*-analysis-*") if analysis_id in p.name) if root.is_dir() else []
    if analysis_id in ("latest", "last") and root.is_dir():
        matches = sorted(root.glob("*-analysis-*"))[-1:]
    if len(matches) != 1:
        raise EvidenceError(f"analysis {analysis_id!r}: {'not found' if not matches else 'ambiguous'}")
    return json.loads((matches[0] / "analysis.json").read_text(encoding="utf-8"))
