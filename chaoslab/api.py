from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from chaoslab import model, settings as settings_module
from chaoslab.audit import AuditLog, Lease
from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos.cleanup import cleanup_records
from chaoslab.chaos.controller import Controller
from chaoslab.settings import PACKAGE_ROOT
from chaoslab.store import ResultStore, compare as compare_results, new_run_id

CONTRACT_VERSION = "1"


class LabApi:
    def __init__(self, root: Path | None = None):
        self.settings = settings_module.load(root)
        self.controller = Controller(self.settings)
        self.store = ResultStore(self.settings.results_dir)
        self.lease = Lease(self.settings.lease_file, self.settings.stop_flag)
        self.audit = AuditLog(self.settings.audit_file)

    def discover_fault_types(self) -> list[dict]:
        return [spec.describe() for spec in fault_registry.all_specs()]

    def discover_targets(self) -> dict:
        allowlist = self.settings.allowlist
        plants = {}
        for name, plant in allowlist["plants"].items():
            plants[name] = {"components": sorted(plant["components"]), "database": plant.get("database"),
                            "api_port": plant.get("api_port"), "opc_port": plant.get("opc_port")}
        return {
            "hosts": allowlist["hosts"],
            "plants": plants,
            "stations": list(self.controller.stations()),
            "fault_targets": allowlist["fault_targets"],
            "resources": ["database"],
            "workload_profiles": self._workload_profiles(),
        }

    def _workload_profiles(self) -> list[dict]:
        from chaoslab.workload.generator import available_profiles, load_profile

        return [{"name": n, "requests_per_second": load_profile(self.settings, n).get("requests_per_second"),
                 "description": load_profile(self.settings, n).get("description")}
                for n in available_profiles(self.settings)]

    def safety_policy(self) -> dict:
        return self.settings.policy

    def definitions_dir(self) -> Path:
        return self.settings.root / "experiments" / "definitions"

    def list_experiments(self) -> list[dict]:
        entries = []
        for path in sorted(self.definitions_dir().glob("*.yaml")):
            try:
                experiment = model.load_file(path)
            except model.DefinitionError as exc:
                entries.append({"name": path.stem, "path": str(path), "valid": False, "problems": exc.problems})
                continue
            entries.append({
                "name": experiment.name, "path": str(path), "valid": True, "category": experiment.category,
                "description": experiment.description,
                "fault_types": [s.fault_type for s in experiment.steps],
            })
        return entries

    def get_experiment(self, name: str) -> dict:
        for entry in self.list_experiments():
            if entry["name"] == name and entry.get("valid"):
                return yaml.safe_load(Path(entry["path"]).read_text(encoding="utf-8"))
        raise FileNotFoundError(f"no experiment definition named {name!r}")

    def validate(self, definition: dict) -> dict:
        try:
            experiment = model.from_mapping(definition)
        except model.DefinitionError as exc:
            return {"allowed": False, "stage": "schema", "reasons": exc.problems}
        decision = self.controller.validate(experiment)
        payload = decision.as_dict()
        payload["stage"] = "policy"
        return payload

    def run(self, definition: dict | Path | str, dry_run: bool = False) -> dict:
        experiment = self._load(definition)
        return self.controller.run(experiment, dry_run=dry_run)

    def _load(self, definition: dict | Path | str) -> model.Experiment:
        if isinstance(definition, dict):
            return model.from_mapping(definition)
        return model.load_file(Path(definition))

    def submit(self, definition: dict) -> dict:
        try:
            experiment = model.from_mapping(definition)
        except model.DefinitionError as exc:
            return {"accepted": False, "stage": "schema", "reasons": exc.problems}
        decision = self.controller.validate(experiment)
        if not decision.allowed:
            return {"accepted": False, "stage": "policy", "reasons": decision.reasons}
        if self.lease.active():
            active = self.lease.active()
            return {"accepted": False, "stage": "lease", "reasons": [
                f"experiment {active['experiment_id']} is already active"]}
        run_id = new_run_id(experiment.name)
        submissions = self.settings.state_dir / "submissions"
        submissions.mkdir(parents=True, exist_ok=True)
        file = submissions / f"{run_id}.yaml"
        file.write_text(yaml.safe_dump(definition, sort_keys=False), encoding="utf-8")
        log = self.settings.log_dir / f"submission-{run_id}.log"
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as handle:
            subprocess.Popen(
                [sys.executable, "-m", "chaoslab", "--root", str(self.settings.root), "execute", "--run-id", run_id,
                 str(file)],
                cwd=str(PACKAGE_ROOT), stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.audit.record(run_id, "submitted_async", file=str(file))
        return {"accepted": True, "experiment_id": run_id, "estimated_wall_s": decision.estimated_wall_s}

    def execute_prepared(self, path: Path, run_id: str) -> dict:
        experiment = model.load_file(path)
        return self.controller.run(experiment, run_id=run_id)

    def status(self, experiment_id: str | None = None) -> dict:
        active = self.lease.active()
        if experiment_id is None:
            return {"active": active, "latest_result": self.store.latest()}
        try:
            resolved = self.store.resolve(experiment_id)
        except FileNotFoundError:
            if active and active.get("experiment_id") == experiment_id:
                return {"experiment_id": experiment_id, "state": active.get("state"), "finished": False}
            return {"experiment_id": experiment_id, "state": "unknown", "finished": False}
        result = self.store.read_result(resolved)
        return {"experiment_id": resolved, "state": "finished", "finished": True,
                "overall_result": result.get("overall_result"), "reasons": result.get("reasons")}

    def result(self, experiment_id: str) -> dict:
        return self.store.read_result(self.store.resolve(experiment_id))

    def metrics(self, experiment_id: str, include_samples: bool = False, limit: int = 500) -> dict:
        resolved = self.store.resolve(experiment_id)
        result = self.store.read_result(resolved)
        payload: dict[str, Any] = {
            "experiment_id": resolved,
            "observed_metrics": result.get("observed_metrics"),
            "production_measurements": result.get("production_measurements"),
            # Fault-specific evidence (direct database or cgroup measurements), when the fault produces any.
            "fault_evidence": {k: result[k] for k in ("database_latency", "resource_limit") if result.get(k)} or None,
        }
        if include_samples:
            path = self.store.directory(resolved) / "samples.jsonl"
            if path.is_file():
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                payload["samples"] = rows[:limit]
                payload["samples_truncated"] = len(rows) > limit
        return payload

    def logs(self, experiment_id: str, max_lines_per_file: int = 100) -> dict:
        resolved = self.store.resolve(experiment_id)
        directory = self.store.directory(resolved) / "logs"
        files = {}
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                files[path.name] = lines[-max_lines_per_file:]
        return {"experiment_id": resolved, "files": files,
                "audit": self.audit.read(resolved)}

    def scoring(self, experiment_id: str) -> dict:
        return {"experiment_id": self.store.resolve(experiment_id),
                "scoring": self.store.read_result(self.store.resolve(experiment_id)).get("scoring")}

    def recovery(self, experiment_id: str) -> dict:
        result = self.store.read_result(self.store.resolve(experiment_id))
        return {"experiment_id": result["experiment_id"], "recovery": result.get("recovery"),
                "cleanup": result.get("cleanup"), "final_health": result.get("final_health"),
                "preparation": result.get("preparation"), "overall_result": result.get("overall_result")}

    def compare(self, first: str, second: str) -> dict:
        return compare_results(self.result(first), self.result(second))

    def emergency_stop(self, wait_s: float = 20.0) -> dict:
        active = self.lease.read()
        self.lease.request_stop()
        self.audit.record(active.get("experiment_id") if active else None, "emergency_stop_requested")
        report: dict[str, Any] = {"requested": True, "active_experiment": active.get("experiment_id") if active
                                  else None, "actions": []}
        deadline = time.monotonic() + wait_s
        while active and active.get("state") != "released" and time.monotonic() < deadline:
            time.sleep(0.5)
            active = self.lease.read()
        remaining = self.lease.read() or {}
        if remaining and remaining.get("state") != "released":
            outcomes = cleanup_records(self.settings, remaining.get("records", []), self.audit,
                                       remaining.get("experiment_id"), "emergency-stop")
            report["actions"] = outcomes
            report["forced_cleanup"] = True
            self.lease.update(state="released", cleaned_by="emergency-stop", records=[])
        else:
            report["forced_cleanup"] = False
        sweep = self._sweep_known_faults()
        report["sweep"] = sweep
        report["ok"] = all(a.get("ok") for a in report["actions"]) and sweep["ok"]
        self.audit.record(report["active_experiment"], "emergency_stop_done", ok=report["ok"])
        if self.settings.stop_flag.exists() and not (self.lease.read() or {}).get("state") == "pending":
            self.settings.stop_flag.unlink(missing_ok=True)
        return report

    def _sweep_known_faults(self) -> dict:
        import os
        import signal

        import psutil

        from chaoslab.chaos.faults import FAULTS, FaultContext

        ctx = FaultContext(settings=self.settings, plant=self.controller.plant)
        actions = []
        for name, proc in ctx.plant.find_components().items():
            try:
                if proc.status() == psutil.STATUS_STOPPED:
                    os.kill(proc.pid, signal.SIGCONT)
                    actions.append({"action": "SIGCONT", "component": name, "pid": proc.pid})
            except (psutil.NoSuchProcess, OSError):
                continue
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if len(cmdline) >= 4 and cmdline[1:3] == ["-m", "chaoslab.chaos.holders"]:
                    os.kill(proc.pid, signal.SIGTERM)
                    actions.append({"action": "SIGTERM", "helper": cmdline[3], "pid": proc.pid})
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        removal = FAULTS["database_latency"].cleanup(ctx, {"type": "database_latency"})
        if removal.detail != "no activation present":
            actions.append({"action": "remove database latency activation", "ok": removal.ok,
                            "detail": removal.detail})
        from chaoslab.chaos import cgroups

        lifted_ok = True
        for unit in cgroups.lab_units():
            try:
                if not cgroups.is_unlimited(cgroups.read_limits(unit)):
                    cgroups.lift(unit)
                    actions.append({"action": "lift cgroup limits", "unit": unit, "ok": True})
            except (OSError, cgroups.CgroupError) as exc:
                lifted_ok = False
                actions.append({"action": "lift cgroup limits", "unit": unit, "ok": False, "detail": str(exc)})
        return {"ok": removal.ok and lifted_ok, "actions": actions}
