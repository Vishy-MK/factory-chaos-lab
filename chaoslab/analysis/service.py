"""Entry points: baseline assessment and post-failure analysis. Used by the CLI and by tests."""

from __future__ import annotations

import json
from pathlib import Path

from chaoslab.analysis import baseline as baseline_module
from chaoslab.analysis import engine, narrative as narrative_module, report
from chaoslab.analysis.evidence import EvidenceError, load
from chaoslab.settings import Settings


def assess_baseline(settings: Settings, source: str | None = None, observe_s: float | None = None) -> dict:
    """Assess a baseline from a run id, a report or artifact path, or a bounded read-only observation."""
    if observe_s:
        source = str(baseline_module.observe(settings, observe_s))
    if not source:
        raise EvidenceError("give a run id, a report or artifact path, or --observe SECONDS")
    config = engine.load_config(settings)
    evidence = load(settings, source)
    assessment = baseline_module.assess(evidence, config)
    path = baseline_module.persist(settings, assessment)
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {"baseline_id": stored["baseline_id"], "path": str(path), "readiness": stored["readiness"],
            "failed_checks": stored["failed_checks"], "unmeasured_checks": stored["unmeasured_checks"],
            "samples": stored["window"]["samples"], "source": stored["source"]["ref"]}


def analyse_failure(settings: Settings, experiment: str, baseline: str = "own", fault_meta: str | None = None,
                    with_narrative: bool = True, llm=None) -> dict:
    meta = json.loads(Path(fault_meta).read_text(encoding="utf-8")) if fault_meta else None
    analysis = engine.analyse(settings, experiment, baseline, meta)
    directory = engine.persist(settings, analysis, None, report.render(analysis, None))
    stored = json.loads((directory / "analysis.json").read_text(encoding="utf-8"))
    narrative = {"status": "not requested"}
    if with_narrative:
        evidence = load(settings, experiment)
        narrative = narrative_module.run(
            settings.root, stored, lambda ref: engine.resolve_ref(ref, stored, evidence, settings), llm=llm)
        (directory / "narrative.json").write_text(json.dumps(narrative, indent=2, default=str), encoding="utf-8")
        (directory / "report.md").write_text(report.render(stored, narrative).replace("{analysis_id}",
                                                                                      stored["analysis_id"]),
                                             encoding="utf-8")
    return {"analysis_id": stored["analysis_id"], "directory": str(directory),
            "report": str(directory / "report.md"), "analysis": str(directory / "analysis.json"),
            "narrative_status": narrative.get("status"), "score": stored["score"]["overall"],
            "recovery": stored["recovery"]["verdict"], "baseline_readiness": stored["baseline"]["readiness"],
            "attribution": stored["inputs"]["experiment"]["fault"]["activation"]["status"],
            "findings": [{k: f[k] for k in ("id", "severity", "confidence", "symptom")} for f in stored["findings"]],
            "evidence_ok": stored["evidence_check"]["ok"]}
