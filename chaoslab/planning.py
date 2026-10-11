"""Experiment proposals: written by the Planner agent, run only after a person approves them.

A proposal is saved only when the safety policy allows its definition. Nothing here runs an experiment;
`chaoslab plan approve <id>` (a person) is the only path from a proposal to a run.
"""

from __future__ import annotations

import getpass
import json
import time
import uuid
from pathlib import Path

STATUSES = ("proposed", "approved", "rejected", "ran")


def directory(settings) -> Path:
    return Path(settings.results_dir) / "analysis" / "proposals"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def propose(settings, api, definition: dict, rationale: str, analysis_id: str | None, finding_ids: list[str],
            expected_outcome: str = "") -> dict:
    if not isinstance(definition, dict):
        return {"saved": False, "reasons": ["definition must be an object"]}
    if not isinstance(rationale, str) or len(rationale.strip()) < 20:
        return {"saved": False, "reasons": ["a rationale of at least 20 characters is required"]}
    decision = api.validate(definition)
    if not decision.get("allowed"):
        return {"saved": False, "stage": decision.get("stage", "policy"), "reasons": decision.get("reasons")}
    if analysis_id:
        from chaoslab.analysis import engine
        from chaoslab.analysis.evidence import EvidenceError

        try:
            analysis = engine.read(settings, analysis_id)
        except EvidenceError as exc:
            return {"saved": False, "reasons": [str(exc)]}
        known = {f["id"] for f in analysis["findings"]}
        unknown = [f for f in finding_ids if f not in known]
        if unknown:
            return {"saved": False, "reasons": [f"finding ids not in analysis {analysis_id}: {unknown}"]}
        analysis_id = analysis["analysis_id"]
    name = "".join(c if c.isalnum() or c == "-" else "-" for c in str(definition.get("name", "proposal")))[:40]
    proposal_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-proposal-{name}-{uuid.uuid4().hex[:6]}"
    record = {"proposal_id": proposal_id, "status": "proposed", "created_at": _now(), "proposed_by": "planner agent",
              "definition": definition, "rationale": rationale.strip(), "analysis_id": analysis_id,
              "finding_ids": finding_ids, "expected_outcome": expected_outcome,
              "policy": {"allowed": True, "estimated_wall_s": decision.get("estimated_wall_s")},
              "history": [{"status": "proposed", "at": _now(), "by": "planner agent"}]}
    folder = directory(settings)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{proposal_id}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return {"saved": True, "proposal_id": proposal_id, "status": "proposed",
            "next": f"a person approves with `chaoslab plan approve {proposal_id}`; nothing runs before that"}


def list_proposals(settings, status: str | None = None) -> list[dict]:
    folder = directory(settings)
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if status and record.get("status") != status:
            continue
        out.append({k: record.get(k) for k in ("proposal_id", "status", "created_at", "analysis_id", "finding_ids",
                                               "rationale", "expected_outcome", "experiment_id")}
                   | {"name": (record.get("definition") or {}).get("name"),
                      "fault": ((record.get("definition") or {}).get("fault") or {}).get("type")})
    return out


def load(settings, proposal_id: str) -> tuple[Path, dict]:
    folder = directory(settings)
    matches = sorted(p for p in folder.glob("*.json") if proposal_id in p.stem) if folder.is_dir() else []
    if len(matches) != 1:
        raise FileNotFoundError(f"proposal {proposal_id!r}: {'not found' if not matches else 'ambiguous'}")
    return matches[0], json.loads(matches[0].read_text(encoding="utf-8"))


def set_status(settings, proposal_id: str, status: str, **extra) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    path, record = load(settings, proposal_id)
    record["status"] = status
    record.update(extra)
    try:
        who = getpass.getuser()
    except Exception:
        who = "unknown"
    record["history"].append({"status": status, "at": _now(), "by": extra.pop("by", None) or who})
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record
