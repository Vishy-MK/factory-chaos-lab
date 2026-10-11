"""Readable Markdown report of one analysis. Deterministic results first; the AI narrative is a separate section."""

from __future__ import annotations

from typing import Any


def _v(value: Any, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text}{unit}"


ROWS = [
    ("API success ratio", "api_success_ratio", ""), ("authenticated success ratio", "authenticated_success_ratio", ""),
    ("machines all connected (share)", "machines_all_connected_ratio", ""),
    ("tag ingestion", "tag_ingest_per_second", " /s"), ("longest ingestion stall", "tag_ingest_stall_max_s", " s"),
    ("telemetry stale (share)", "telemetry_stale_ratio", ""), ("oldest telemetry", "telemetry_age_max_s", " s"),
    ("floor last check (max)", "floor_age_max_s", " s"), ("target reachable (share)", "target_reachable_ratio", ""),
    ("DB statements", "db_statements_per_second", " /s"), ("DB mean statement time", "db_statement_ms_mean", " ms"),
    ("DB mean driver time", "db_driver_ms_mean", " ms"), ("DB delayed statements", "db_injected_statements", ""),
    ("DB injected delay per statement", "db_injected_delay_ms_mean", " ms"),
    ("DB delayed statement time", "db_delayed_statement_ms_mean", " ms"), ("DB errors", "db_errors", ""),
    ("DB 'database is locked'", "db_lock_errors", ""), ("host CPU max", "host_cpu_percent_max", " %"),
]


def _metric_table(analysis: dict) -> list[str]:
    windows = analysis["windows"]
    names = [n for n in ("baseline", "fault", "recovery", "post") if windows.get(n)]
    lines = ["| metric | " + " | ".join(f"{n} (n={windows[n]['window']['samples']})" for n in names) + " |",
             "|---|" + "---|" * len(names)]
    for label in ("p50", "p95"):
        cells = []
        for n in names:
            dist = windows[n]["metrics"].get("api_latency_ms")
            cells.append(_v(dist[label] if dist else None, " ms"))
        lines.append(f"| API latency {label} | " + " | ".join(cells) + " |")
    for label, key, unit in ROWS:
        values = [(windows[n]["metrics"].get(key) or {}).get("value") for n in names]
        if all(v is None for v in values):
            continue
        lines.append(f"| {label} | " + " | ".join(_v(v, unit) for v in values) + " |")
    return lines


def render(analysis: dict, narrative: dict | None) -> str:
    inputs = analysis["inputs"]
    experiment = inputs["experiment"]
    fault = experiment["fault"]
    score = analysis["score"]
    weaknesses = [f for f in analysis["findings"] if f["type"] == "weakness"]
    positives = [f for f in analysis["findings"] if f["type"] == "positive"]
    overall = score["overall"]
    lines = [
        f"# Failure impact analysis {{analysis_id}}",
        "",
        "## Summary",
        "",
        f"- **Experiment**: `{experiment['ref']}` ({experiment['kind']}), record `{experiment['path']}`",
        f"- **Fault**: `{fault.get('type')}` on `{fault.get('target')}` with `{fault.get('parameters')}`; attribution "
        f"**{fault['activation']['status']}**",
        f"- **Baseline**: `{inputs['baseline']['ref']}` ({inputs['baseline']['source']}), readiness "
        f"**{analysis['baseline']['readiness']}**; compatible: **{analysis['compatibility']['compatible']}**",
        f"- **Recovery (this analysis)**: **{analysis['recovery']['verdict']}**, "
        f"{_v(analysis['recovery']['recovery_time'].get('seconds_after_fault_end'), ' s')} after the fault window",
        f"- **Controller verdict (original record)**: `{analysis['controller_verdict'].get('overall_result')}`",
        f"- **Resilience score**: **{overall['status']}**" + (f" {overall['score']} / 100" if overall["score"] is not None
                                                                else f" (missing: {overall.get('missing_dimensions')})"),
        f"- **Weaknesses**: {len(weaknesses)} ({', '.join(f['id'] + ' ' + f['severity'] for f in weaknesses) or 'none'}); "
        f"positive findings: {len(positives)}",
        f"- **Narrative**: {(narrative or {}).get('status', 'not requested')}",
        "",
        f"Raw measurements: `{experiment['path'].rsplit('/', 1)[0]}/samples.jsonl`" if experiment["kind"] == "lab_result"
        else f"Raw measurements: `{experiment['path']}` (samples)",
        "Detailed findings: `analysis.json` next to this report.",
        "",
        "## Baseline assessment",
        "",
        f"Window: {inputs['baseline']['window'].get('basis')}, {inputs['baseline']['window'].get('samples')} samples.",
        "",
    ]
    for check in analysis["baseline"]["checks"]:
        mark = {True: "ok", False: "FAILED", None: "not measured"}[check["ok"]]
        lines.append(f"- {check['name']}: {mark} ({check['detail']})")
    if not analysis["compatibility"]["compatible"]:
        lines.append(f"- **incompatible**: {analysis['compatibility'].get('incompatible_because')}")
    lines += ["", "## Phase comparison", "",
              "Percentiles: nearest-rank within each window, never averaged across windows. `n` = samples.", ""]
    lines += _metric_table(analysis)
    impact = analysis["impact"]
    lines += ["", "## Impact", "", "Direct (measured at the fault target):"]
    lines += [f"- {i['phase']}: `{i['metric']}` {_v(i.get('baseline'))} -> {_v(i.get('value'))}" for i in impact["direct"]] \
        or ["- none measured"]
    lines += ["", "Downstream:"]
    lines += [f"- {i['phase']}: `{i['metric']}` {_v(i.get('baseline'))} -> {_v(i.get('value'))}" for i in impact["downstream"]] \
        or ["- none significant"]
    if impact["confounders"]:
        lines += ["", "Possible confounders:"] + [f"- {c}" for c in impact["confounders"]]
    lines += ["", "## Recovery", ""]
    for item in analysis["recovery"]["evidence"]:
        mark = {True: "ok", False: "NOT OK", None: "not measured"}[item["ok"]]
        lines.append(f"- {item['name']}: {mark}: {item['detail']}")
    lines += [f"- caution: {c}" for c in analysis["recovery"]["cautions"]]
    lines += ["", "## Ranked findings", ""]
    for f in analysis["findings"]:
        lines.append(f"### {f['id']} {f['severity'].upper()} (confidence {f['confidence']}): {f['symptom']}")
        lines.append("")
        if f["type"] == "weakness":
            lines.append(f"- component: {f['component']}; dependency: {f.get('dependency')}")
            lines.append("- severity rationale: " + ("; ".join(f"{r['criterion']} +{r['points']} ({r['detail']})"
                                                             for r in f["severity_rationale"]) or "none"))
        lines.append(f"- confidence: {', '.join(f['confidence_rationale'])}")
        if f.get("likely_explanation"):
            lines.append(f"- likely explanation ({f['explanation_status']}): {f['likely_explanation']}")
        for limitation in f.get("limitations") or []:
            lines.append(f"- limitation: {limitation}")
        lines.append(f"- evidence: {', '.join('`' + r + '`' for r in f['evidence'])}")
        lines.append("")
    lines += ["## Recommended fixes (not applied)", ""]
    for rec in analysis["recommendations"]:
        lines.append(f"### {rec.get('finding_id') or 'gap'}: {rec['title']}")
        lines.append("")
        lines.append(f"- remediation: {rec['remediation']}")
        lines.append(f"- why: {rec['addresses']}")
        for loc in rec["locations"]:
            lines.append(f"- verified location: `{loc['path']}:{loc['line']}` ({loc['shows']})")
        for loc in rec["unverified_locations"]:
            lines.append(f"- unverified location (not cited): `{loc['path']}` ({loc['detail']})")
        lines.append(f"- tradeoffs: {rec['tradeoffs']}; effort: {rec['effort']}")
        lines += [f"- acceptance: {a}" for a in rec["acceptance_criteria"]]
        lines.append(f"- manual validation: {rec['manual_validation']}")
        lines.append("")
    lines += ["## Resilience score", "", f"Formula: {score['formula']}. Evidence coverage {score['evidence_coverage']}.",
              "", "| dimension | weight | score | rule |", "|---|---|---|---|"]
    for d in score["dimensions"]:
        lines.append(f"| {d['name']} | {score['weights'][d['name']]} | {_v(d['score'])} | "
                     f"{d['rule'] if d['measured'] else 'not measured: ' + d.get('missing', '')} |")
    lines += [f"", f"Scope: {score['scope']}."] + [f"Qualification: {q}." for q in score["qualifications"]]
    lines += ["", "## Missing evidence and limitations", ""]
    lines += [f"- missing: {m}" for m in analysis["evidence_coverage"]["missing"]] or ["- nothing listed as missing"]
    lines += [f"- {m}" for m in analysis["limitations"]]
    lines += ["", "## AI narrative (interpretation, separate from the measurements above)", ""]
    if not narrative or narrative.get("status") != "complete":
        lines.append(f"Narrative {(narrative or {}).get('status', 'not requested')}: "
                     f"{(narrative or {}).get('error') or 'deterministic results above are complete'}")
    else:
        output = narrative["output"]
        lines += [output["summary"], ""]
        for item in output.get("impact", []):
            lines.append(f"- [{item['kind']}] {item['statement']} ({', '.join(item['evidence'])})")
        if output.get("disagreements"):
            lines += ["", "Disagreements with the controller:"]
            lines += [f"- {d['claim']} ({', '.join(d['evidence'])})" for d in output["disagreements"]]
        if output.get("cannot_conclude"):
            lines += ["", "Cannot be concluded:"] + [f"- {c}" for c in output["cannot_conclude"]]
    return "\n".join(lines) + "\n"
