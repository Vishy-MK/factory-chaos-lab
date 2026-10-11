from __future__ import annotations

from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
from chaoslab.adapters.docker_plant import make_adapter
from chaoslab.audit import AuditLog
from chaoslab.chaos.faults import FAULTS, Check, FaultContext
from chaoslab.settings import Settings


def build_context(settings: Settings) -> FaultContext:
    return FaultContext(settings=settings, plant=make_adapter(settings))


def cleanup_records(settings: Settings, records: list[dict], audit: AuditLog | None, experiment_id: str | None,
                    actor: str) -> list[dict]:
    ctx = build_context(settings)
    outcomes: list[dict] = []
    for record in reversed(records):
        fault = FAULTS.get(record.get("type", ""))
        if fault is None:
            outcomes.append({"record": record, "ok": False, "detail": f"unknown fault type {record.get('type')!r}"})
            continue
        try:
            check = fault.cleanup(ctx, record)
            removed = fault.removed(ctx, record)
        except Exception as exc:
            check = Check("cleanup", False, f"cleanup raised {type(exc).__name__}: {exc}")
            removed = Check("fault_removed", False, "not verified")
        outcome = {"type": record.get("type"), "ok": check.ok and removed.ok, "cleanup": check.as_dict(),
                   "removed": removed.as_dict(), "by": actor}
        outcomes.append(outcome)
        if audit:
            audit.record(experiment_id, "cleanup", actor=actor, outcome=outcome)
    return outcomes
