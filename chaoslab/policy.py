from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from chaoslab.chaos import faults as fault_registry
from chaoslab.model import Experiment, Step
from chaoslab.settings import Settings


@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    normalised: list[dict] = field(default_factory=list)
    estimated_wall_s: float = 0.0

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons), "normalised_steps": self.normalised,
                "estimated_wall_s": round(self.estimated_wall_s, 1)}


class PolicyValidator:
    def __init__(self, settings: Settings, stations: tuple[str, ...] = ()):
        self.settings = settings
        self.policy = settings.policy
        self.allowlist = settings.allowlist
        self.stations = stations

    def validate(self, experiment: Experiment) -> Decision:
        reasons: list[str] = []
        limits = self.policy["limits"]

        steps = experiment.steps
        compound_enabled = bool(self.policy.get("compound", {}).get("enabled", False))
        if len(steps) > 1 and not compound_enabled:
            reasons.append("compound experiments are disabled by safety policy (compound.enabled: false)")
        max_steps = int(limits.get("max_steps_per_experiment", 1))
        if len(steps) > max_steps:
            reasons.append(f"{len(steps)} steps exceeds max_steps_per_experiment={max_steps}")

        baseline = experiment.baseline_s
        if experiment.category == "infrastructure":
            if baseline < int(limits.get("min_baseline_s", 0)):
                reasons.append(f"baseline_s={baseline} is below the minimum {limits['min_baseline_s']}")
            if baseline > int(limits.get("max_baseline_s", 60)):
                reasons.append(f"baseline_s={baseline} is above the maximum {limits['max_baseline_s']}")

        if experiment.workload not in self._known_workloads():
            reasons.append(f"workload profile {experiment.workload!r} is unknown")
        else:
            reasons.extend(self._workload_reasons(experiment.workload))

        normalised: list[dict] = []
        wall = float(baseline) if experiment.category == "infrastructure" else 0.0
        app_events = 0
        for index, step in enumerate(steps):
            step_reasons, resolved, step_wall = self._validate_step(experiment, step, f"step {index + 1}")
            reasons.extend(step_reasons)
            normalised.append({"target": dict(step.target), "fault": {"type": step.fault_type, "parameters": resolved}})
            wall += step_wall
            if step.fault_type != "none" and experiment.category == "application":
                app_events += 1

        if experiment.category == "application":
            reasons.extend(self._validate_application(experiment, app_events))
            app = experiment.application
            speed = float(app.get("speed", self.settings.lab["application_runs"]["default_speed"]) or 1)
            duration = float(app.get("duration_s", 0) or 0)
            wall = duration / speed + 60.0

        max_wall = float(limits["max_experiment_wall_s"])
        if wall > max_wall:
            reasons.append(f"estimated wall time {wall:.0f}s exceeds max_experiment_wall_s={max_wall:.0f}")

        recovery_timeout = experiment.recovery.get("timeout_s", limits["recovery_timeout_s"])
        if isinstance(recovery_timeout, bool) or not isinstance(recovery_timeout, (int, float)):
            reasons.append("recovery.timeout_s must be a number")
        elif recovery_timeout > limits["recovery_timeout_s"]:
            reasons.append(f"recovery.timeout_s={recovery_timeout} exceeds the policy limit "
                           f"{limits['recovery_timeout_s']}")
        elif recovery_timeout <= 0:
            reasons.append("recovery.timeout_s must be positive")

        return Decision(allowed=not reasons, reasons=reasons, normalised=normalised, estimated_wall_s=wall)

    def _known_workloads(self) -> set[str]:
        from chaoslab.workload.generator import available_profiles

        return set(available_profiles(self.settings))

    def _workload_reasons(self, name: str) -> list[str]:
        from chaoslab.workload.generator import load_profile

        reasons = []
        profile = load_profile(self.settings, name)
        limits = self.policy.get("workload", {})
        max_rps = float(limits.get("max_requests_per_second", 5))
        if float(profile.get("requests_per_second", 0)) > max_rps:
            reasons.append(f"workload {name!r} requests_per_second exceeds the policy limit {max_rps:g}")
        if profile.get("high_volume") and not limits.get("high_volume_enabled", False):
            reasons.append(f"workload {name!r} is high-volume, which is disabled by safety policy")
        return reasons

    def _validate_step(self, experiment: Experiment, step: Step, label: str) -> tuple[list[str], dict, float]:
        reasons: list[str] = []
        limits = self.policy["limits"]
        fault_name = step.fault_type

        if fault_name in fault_registry.UNSUPPORTED_BY_NAME:
            spec = fault_registry.UNSUPPORTED_BY_NAME[fault_name]
            return [f"{label}: fault {fault_name!r} is not supported: {spec.unsupported_reason}"], {}, 0.0
        if fault_name == "memory_pressure" or fault_name not in fault_registry.FAULTS:
            return [f"{label}: unknown fault type {fault_name!r}"], {}, 0.0

        fault = fault_registry.FAULTS[fault_name]
        spec = fault.spec
        layer_matches = (
            (experiment.category == "infrastructure" and spec.layer in ("infrastructure", "control"))
            or (experiment.category == "application" and spec.layer in ("simulator", "control"))
        )
        if not layer_matches:
            reasons.append(f"{label}: fault {fault_name!r} (layer {spec.layer}) cannot run in a "
                           f"{experiment.category} experiment")

        resolved, problems = fault_registry.validate_params(spec, step.parameters)
        reasons.extend(f"{label}: {p}" for p in problems)
        reasons.extend(self._validate_target(spec, step, label))

        wall = 0.0
        if spec.layer in ("infrastructure", "control"):
            hold = fault.hold_seconds(resolved)
            if hold > float(limits["max_fault_duration_s"]):
                reasons.append(f"{label}: fault duration {hold:g}s exceeds max_fault_duration_s="
                               f"{limits['max_fault_duration_s']}")
            if fault_name == "cpu_pressure":
                pressure = self.policy.get("cpu_pressure", {})
                if resolved.get("percent", 0) > pressure.get("max_percent", 80):
                    reasons.append(f"{label}: cpu_pressure percent exceeds policy maximum {pressure['max_percent']}")
                if resolved.get("duration_s", 0) > pressure.get("max_duration_s", 30):
                    reasons.append(f"{label}: cpu_pressure duration exceeds policy maximum "
                                   f"{pressure['max_duration_s']}")
            if fault_name == "database_latency":
                latency = self.policy.get("database_latency", {})
                if not latency.get("enabled", False):
                    reasons.append(f"{label}: database_latency is disabled by safety policy "
                                   "(database_latency.enabled: false)")
                if resolved.get("delay_ms", 0) > latency.get("max_delay_ms", 1000):
                    reasons.append(f"{label}: database_latency delay_ms exceeds policy maximum "
                                   f"{latency.get('max_delay_ms', 1000)}")
                if resolved.get("duration_s", 0) > latency.get("max_duration_s", 30):
                    reasons.append(f"{label}: database_latency duration exceeds policy maximum "
                                   f"{latency.get('max_duration_s', 30)}")
            wall = hold + float(limits.get("cleanup_grace_s", 15))
        else:
            reasons.extend(self._validate_simulator_params(fault, resolved, label, experiment))
        return reasons, resolved, wall

    def _validate_target(self, spec: fault_registry.FaultSpec, step: Step, label: str) -> list[str]:
        reasons: list[str] = []
        target = step.target
        allowed_keys = set(spec.target_keys)
        extra = set(target) - allowed_keys
        if extra:
            reasons.append(f"{label}: target keys {sorted(extra)} are not accepted by {spec.name}; "
                           f"allowed: {sorted(allowed_keys)}")
        for key in spec.target_keys:
            if key not in target:
                reasons.append(f"{label}: target is missing {key!r}")

        plant = target.get("plant")
        if plant is not None and plant not in self.allowlist["plants"]:
            reasons.append(f"{label}: plant {plant!r} is not in the allowlist {sorted(self.allowlist['plants'])}")
        for key in ("host", "address", "ip", "url", "port"):
            if key in target:
                reasons.append(f"{label}: target may not specify {key!r}; only allowlisted lab resources are valid")

        if spec.layer == "infrastructure" and plant in self.allowlist["plants"]:
            allowed_targets = self.allowlist["fault_targets"].get(spec.name, [])
            if "resource" in spec.target_keys:
                resource = target.get("resource")
                if resource != "database":
                    reasons.append(f"{label}: {spec.name} target.resource must be 'database'")
                elif "database" not in allowed_targets:
                    reasons.append(f"{label}: database is not an allowlisted target for {spec.name}")
            else:
                component = target.get("component")
                plant_components = self.allowlist["plants"][plant]["components"]
                if component not in plant_components:
                    reasons.append(f"{label}: component {component!r} is not an allowlisted component of "
                                   f"{plant}: {sorted(plant_components)}")
                elif component not in allowed_targets:
                    reasons.append(f"{label}: component {component!r} is not an allowlisted target for "
                                   f"{spec.name}: {allowed_targets}")

        if spec.layer == "simulator" and "station" in spec.target_keys:
            station = target.get("station")
            if self.stations and station not in self.stations:
                reasons.append(f"{label}: station {station!r} is not part of the line: {list(self.stations)}")
        return reasons

    def _validate_simulator_params(self, fault, resolved: dict, label: str, experiment: Experiment) -> list[str]:
        reasons: list[str] = []
        limits = self.policy["application"]
        app = experiment.application
        duration = app.get("duration_s")
        speed = float(app.get("speed", self.settings.lab["application_runs"]["default_speed"]) or 1)
        if "start_s" in resolved and "end_s" in resolved:
            if resolved["end_s"] <= resolved["start_s"]:
                reasons.append(f"{label}: end_s must be greater than start_s")
            window_wall = (resolved["end_s"] - resolved["start_s"]) / max(speed, 0.001)
            if window_wall > float(limits["max_scripted_fault_wall_s"]):
                reasons.append(f"{label}: scripted fault window is {window_wall:.1f}s of wall time at {speed:g}x, "
                               f"above max_scripted_fault_wall_s={limits['max_scripted_fault_wall_s']}")
            if duration and resolved["end_s"] > duration:
                reasons.append(f"{label}: end_s={resolved['end_s']} is beyond the run duration {duration}")
        if "at_s" in resolved and duration and resolved["at_s"] > duration:
            reasons.append(f"{label}: at_s={resolved['at_s']} is beyond the run duration {duration}")
        if "every_min_s" in resolved and resolved["every_min_s"] > resolved["every_max_s"]:
            reasons.append(f"{label}: every_min_s must not exceed every_max_s")
        if "duration_min_s" in resolved:
            if resolved["duration_min_s"] > resolved["duration_max_s"]:
                reasons.append(f"{label}: duration_min_s must not exceed duration_max_s")
            stop_wall = resolved["duration_max_s"] / max(speed, 0.001)
            if stop_wall > float(limits["max_scripted_fault_wall_s"]):
                reasons.append(f"{label}: micro-stop length exceeds the scripted fault wall limit")
        return reasons

    def _validate_application(self, experiment: Experiment, event_count: int) -> list[str]:
        reasons: list[str] = []
        limits = self.policy["application"]
        app = experiment.application
        known = {"duration_s", "speed", "seed", "measure"}
        extra = set(app) - known
        if extra:
            reasons.append(f"application: unknown keys {sorted(extra)}")
        duration = app.get("duration_s")
        if isinstance(duration, bool) or not isinstance(duration, int):
            reasons.append("application.duration_s is required and must be an integer number of line seconds")
        elif not 60 <= duration <= int(limits["max_line_duration_s"]):
            reasons.append(f"application.duration_s={duration} must be within 60..{limits['max_line_duration_s']}")
        speed = app.get("speed", self.settings.lab["application_runs"]["default_speed"])
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            reasons.append("application.speed must be a number")
        elif not float(limits["min_speed"]) <= float(speed) <= float(limits["max_speed"]):
            reasons.append(f"application.speed={speed} must be within {limits['min_speed']}..{limits['max_speed']}")
        seed = app.get("seed", self.settings.lab["application_runs"]["default_seed"])
        if isinstance(seed, bool) or not isinstance(seed, int):
            reasons.append("application.seed must be an integer")
        measure = app.get("measure", ["booking", "downtime", "oee"])
        allowed_measures = {"booking", "downtime", "oee", "connection", "latency"}
        if not isinstance(measure, list) or not measure or not set(measure) <= allowed_measures:
            reasons.append(f"application.measure must be a non-empty subset of {sorted(allowed_measures)}")
        if event_count > int(limits["max_events"]):
            reasons.append(f"{event_count} scripted events exceed max_events={limits['max_events']}")
        return reasons


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
