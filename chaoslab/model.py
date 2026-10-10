from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CATEGORIES = ("application", "infrastructure")
ALLOWED_TOP_LEVEL = {
    "name", "description", "category", "target", "fault", "steps", "application", "workload",
    "baseline_s", "preconditions", "expected_measurements", "cleanup", "recovery", "scoring",
}


class DefinitionError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class State(str, Enum):
    PENDING = "pending"
    PREFLIGHT = "preflight"
    BASELINE = "baseline"
    INJECTING = "injecting"
    FAULT_ACTIVE = "fault_active"
    CLEANUP = "cleanup"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ABORTED = "aborted"
    FAILED = "failed"


TERMINAL = {State.COMPLETED, State.REJECTED, State.ABORTED, State.FAILED}

TRANSITIONS: dict[State, set[State]] = {
    State.PENDING: {State.PREFLIGHT, State.REJECTED, State.ABORTED},
    State.PREFLIGHT: {State.BASELINE, State.REJECTED, State.ABORTED, State.FAILED},
    State.BASELINE: {State.INJECTING, State.ABORTED, State.FAILED},
    State.INJECTING: {State.FAULT_ACTIVE, State.CLEANUP, State.ABORTED, State.FAILED},
    State.FAULT_ACTIVE: {State.INJECTING, State.CLEANUP, State.ABORTED, State.FAILED},
    State.CLEANUP: {State.RECOVERING, State.INJECTING, State.FAILED, State.ABORTED},
    State.RECOVERING: {State.VERIFYING, State.FAILED, State.ABORTED},
    State.VERIFYING: {State.COMPLETED, State.INJECTING, State.FAILED, State.ABORTED},
    State.COMPLETED: set(),
    State.REJECTED: set(),
    State.ABORTED: set(),
    State.FAILED: set(),
}


class TransitionError(Exception):
    pass


@dataclass
class Step:
    target: dict[str, Any]
    fault_type: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"target": dict(self.target), "fault": {"type": self.fault_type, "parameters": dict(self.parameters)}}


@dataclass
class Experiment:
    name: str
    description: str
    category: str
    steps: list[Step]
    application: dict[str, Any] = field(default_factory=dict)
    workload: str = "baseline"
    baseline_s: int = 10
    preconditions: list[str] = field(default_factory=list)
    expected_measurements: list[str] = field(default_factory=list)
    cleanup: str = "automatic"
    recovery: dict[str, Any] = field(default_factory=dict)
    scoring: str = "none"
    source: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "steps": [step.as_dict() for step in self.steps],
            "application": dict(self.application),
            "workload": self.workload,
            "baseline_s": self.baseline_s,
            "preconditions": list(self.preconditions),
            "expected_measurements": list(self.expected_measurements),
            "cleanup": self.cleanup,
            "recovery": dict(self.recovery),
            "scoring": self.scoring,
        }


def _step_from(raw: Any, where: str, problems: list[str]) -> Step | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping with target and fault")
        return None
    target = raw.get("target")
    fault = raw.get("fault")
    if not isinstance(target, dict) or not target:
        problems.append(f"{where}: target must be a non-empty mapping")
        return None
    if not isinstance(fault, dict) or "type" not in fault:
        problems.append(f"{where}: fault must be a mapping with a type")
        return None
    parameters = fault.get("parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        problems.append(f"{where}: fault.parameters must be a mapping")
        return None
    unknown = set(fault) - {"type", "parameters"}
    if unknown:
        problems.append(f"{where}: unknown fault keys {sorted(unknown)}")
    if not isinstance(fault["type"], str):
        problems.append(f"{where}: fault.type must be a string")
        return None
    return Step(target=target, fault_type=fault["type"], parameters=parameters)


def from_mapping(raw: Any, source: str | None = None) -> Experiment:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise DefinitionError(["experiment definition must be a mapping"])
    unknown = set(raw) - ALLOWED_TOP_LEVEL
    if unknown:
        problems.append(f"unknown top-level keys: {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        problems.append("name must match [a-z0-9][a-z0-9._-]{0,63}")
        name = str(name)
    category = raw.get("category")
    if category not in CATEGORIES:
        problems.append(f"category must be one of {list(CATEGORIES)}")
    description = raw.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be a string")
        description = ""

    steps: list[Step] = []
    if "steps" in raw and ("fault" in raw or "target" in raw):
        problems.append("use either steps or target+fault, not both")
    if "steps" in raw:
        if not isinstance(raw["steps"], list) or not raw["steps"]:
            problems.append("steps must be a non-empty list")
        else:
            for index, item in enumerate(raw["steps"]):
                step = _step_from(item, f"steps[{index}]", problems)
                if step:
                    steps.append(step)
    else:
        step = _step_from({"target": raw.get("target"), "fault": raw.get("fault")}, "experiment", problems)
        if step:
            steps.append(step)

    baseline_s = raw.get("baseline_s", 10)
    if isinstance(baseline_s, bool) or not isinstance(baseline_s, int):
        problems.append("baseline_s must be an integer number of seconds")
        baseline_s = 10

    application = raw.get("application", {})
    if not isinstance(application, dict):
        problems.append("application must be a mapping")
        application = {}
    recovery = raw.get("recovery", {})
    if not isinstance(recovery, dict):
        problems.append("recovery must be a mapping")
        recovery = {}
    for key in ("preconditions", "expected_measurements"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"{key} must be a list of strings")
    workload = raw.get("workload", "baseline")
    if not isinstance(workload, str):
        problems.append("workload must be a string naming a profile")
        workload = "baseline"
    scoring = raw.get("scoring", "none")
    if not isinstance(scoring, str):
        problems.append("scoring must be a string")
        scoring = "none"
    cleanup = raw.get("cleanup", "automatic")
    if not isinstance(cleanup, str):
        problems.append("cleanup must be a string")
        cleanup = "automatic"

    if problems:
        raise DefinitionError(problems)

    return Experiment(
        name=name,
        description=description,
        category=category,
        steps=steps,
        application=application,
        workload=workload,
        baseline_s=baseline_s,
        preconditions=list(raw.get("preconditions", [])),
        expected_measurements=list(raw.get("expected_measurements", [])),
        cleanup=cleanup,
        recovery=recovery,
        scoring=scoring,
        source=source,
    )


def load_file(path: Path) -> Experiment:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DefinitionError([f"experiment file not found: {path}"]) from exc
    except yaml.YAMLError as exc:
        raise DefinitionError([f"{path} is not valid YAML: {exc}"]) from exc
    return from_mapping(raw, source=str(path))
