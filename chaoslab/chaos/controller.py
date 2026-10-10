from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from chaoslab import __version__ as lab_version
from chaoslab import model, resources, settings as settings_module
from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
from chaoslab.adapters.logs import LogCollector
from chaoslab.adapters.metrics import HostSampler, MetricsAdapter, Sampler
from chaoslab.adapters.scoring import ScoringAdapter
from chaoslab.audit import AuditLog, Lease, now_iso
from chaoslab.chaos import faults as fault_registry
from chaoslab.chaos import watchdog
from chaoslab.chaos.cleanup import build_context, cleanup_records
from chaoslab.chaos.recovery import RecoveryVerifier
from chaoslab.model import Experiment, State
from chaoslab.policy import Decision, PolicyValidator
from chaoslab.settings import Settings
from chaoslab.store import ResultStore, new_run_id
from chaoslab.workload.generator import WorkloadGenerator, load_profile


class StateTracker:
    def __init__(self, experiment_id: str, audit: AuditLog, lease: Lease | None):
        self.experiment_id = experiment_id
        self.audit = audit
        self.lease = lease
        self.state = State.PENDING
        self.history: list[dict] = [{"state": State.PENDING.value, "at": now_iso()}]

    def to(self, new_state: State, **details) -> None:
        if new_state not in model.TRANSITIONS[self.state]:
            raise model.TransitionError(f"illegal transition {self.state.value} -> {new_state.value}")
        self.state = new_state
        self.history.append({"state": new_state.value, "at": now_iso(), **details})
        self.audit.record(self.experiment_id, "state", state=new_state.value, **details)
        if self.lease is not None and new_state not in model.TERMINAL:
            self.lease.update(state=new_state.value)


class Controller:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or settings_module.load()
        self.audit = AuditLog(self.settings.audit_file)
        self.lease = Lease(self.settings.lease_file, self.settings.stop_flag)
        self.store = ResultStore(self.settings.results_dir)
        self.plant = FactorySemanticsAdapter(self.settings)
        self.metrics = MetricsAdapter(self.plant)
        self.scoring = ScoringAdapter(self.settings)
        self.logs = LogCollector(self.settings)
        self.gate = resources.ResourceGate(self.settings.policy["resources"])

    def stations(self) -> tuple[str, ...]:
        line = self.settings.pack_source / "line.json"
        try:
            data = json.loads(line.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        return tuple(s["name"] for s in data.get("stations", []))

    def validator(self) -> PolicyValidator:
        return PolicyValidator(self.settings, self.stations())

    def validate(self, experiment: Experiment) -> Decision:
        return self.validator().validate(experiment)

    def application_version(self) -> str | None:
        try:
            from importlib.metadata import version

            return version("factorysemantics-mes")
        except Exception:
            return None

    def _commit(self) -> str | None:
        import subprocess

        try:
            out = subprocess.run(["git", "-C", str(self.settings.upstream_dir), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=5, check=False)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def _new_result(self, run_id: str, experiment: Experiment) -> dict:
        return {
            "experiment_id": run_id,
            "started_at": now_iso(),
            "finished_at": None,
            "lab_version": lab_version,
            "application": {
                "name": "FactorySemantics MES",
                "version": self.application_version(),
                "commit": self._commit(),
                "deployment_mode": "lightweight: fsmes plant (SQLite, OPC UA replay), no Docker",
                "plant": self.settings.plant_name,
            },
            "definition": experiment.as_dict(),
            "definition_source": experiment.source,
            "state_history": [],
            "initial_health": None,
            "initial_resources": None,
            "fault_definition": None,
            "workload": None,
            "fault_injection": {"injected": False, "steps": []},
            "observed_metrics": {},
            "production_measurements": None,
            "scoring": None,
            "logs": None,
            "cleanup": {"ok": None, "steps": []},
            "recovery": None,
            "final_health": None,
            "overall_result": None,
            "reasons": [],
            "limitations": [],
        }

    def _finish(self, result: dict, tracker: StateTracker, final_state: State, overall: str,
                reasons: list[str] | None = None) -> dict:
        if tracker.state not in model.TERMINAL:
            tracker.to(final_state)
        result["state_history"] = tracker.history
        result["overall_result"] = overall
        result["reasons"] = list(reasons or result.get("reasons", []))
        result["finished_at"] = now_iso()
        self.store.write_result(result["experiment_id"], result)
        self.audit.record(result["experiment_id"], "finished", overall_result=overall, reasons=result["reasons"])
        return result

    def run(self, experiment: Experiment, dry_run: bool = False, run_id: str | None = None) -> dict:
        self.settings.ensure_dirs()
        run_id = run_id or new_run_id(experiment.name)
        self.store.create(run_id)
        result = self._new_result(run_id, experiment)
        tracker = StateTracker(run_id, self.audit, None)
        self.audit.record(run_id, "submitted", name=experiment.name, category=experiment.category,
                          source=experiment.source, dry_run=dry_run)
        tracker.to(State.PREFLIGHT)

        decision = self.validate(experiment)
        result["fault_definition"] = decision.normalised
        if not decision.allowed:
            return self._finish(result, tracker, State.REJECTED, "rejected", decision.reasons)

        allowed, reasons, host = self.gate.check()
        result["initial_resources"] = {"host": host.as_dict()}
        if not allowed:
            return self._finish(result, tracker, State.REJECTED, "rejected_preconditions", reasons)

        if dry_run:
            result["reasons"] = ["dry run: definition and preconditions are valid; nothing was executed"]
            return self._finish(result, tracker, State.REJECTED, "dry_run_ok")

        deadline = time.time() + decision.estimated_wall_s + float(self.settings.limits["cleanup_grace_s"]) \
            + float(self.settings.limits["recovery_timeout_s"]) + 60
        if not self.lease.acquire(run_id, experiment.name, deadline):
            active = self.lease.read() or {}
            return self._finish(result, tracker, State.REJECTED, "rejected_preconditions",
                                [f"another experiment is active: {active.get('experiment_id')}"])
        tracker.lease = self.lease
        watchdog_pid = watchdog.spawn(self.settings.root, run_id)
        self.lease.update(watchdog_pid=watchdog_pid)
        self.audit.record(run_id, "lease_acquired", deadline=deadline, watchdog_pid=watchdog_pid)

        try:
            if experiment.category == "infrastructure":
                return self._run_infrastructure(experiment, decision, run_id, result, tracker)
            return self._run_application(experiment, decision, run_id, result, tracker)
        except Exception as exc:
            result["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "trace": traceback.format_exc(limit=8)}
            records = (self.lease.read() or {}).get("records", [])
            outcomes = cleanup_records(self.settings, records, self.audit, run_id, "controller-error-path")
            result["cleanup"] = {"ok": all(o["ok"] for o in outcomes) if outcomes else True, "steps": outcomes}
            overall = "error" if result["cleanup"]["ok"] else "unsafe_cleanup_failed"
            if tracker.state not in model.TERMINAL:
                tracker.state = State.FAILED
                tracker.history.append({"state": State.FAILED.value, "at": now_iso(), "error": str(exc)})
            return self._finish(result, tracker, State.FAILED, overall, [f"{type(exc).__name__}: {exc}"])
        finally:
            self.lease.release()

    def _sleep(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.lease.stop_requested():
                return True
            time.sleep(min(0.25, max(0.0, end - time.monotonic())))
        return self.lease.stop_requested()

    def _run_infrastructure(self, experiment: Experiment, decision: Decision, run_id: str, result: dict,
                            tracker: StateTracker) -> dict:
        limits = self.settings.limits
        run_dir = self.store.directory(run_id)
        started_at = datetime.now(timezone.utc)

        health = self.plant.health()
        components = self.plant.process_table()
        initial_ok = bool(health.ok and isinstance(health.body, dict) and health.body.get("status") == "ok")
        watching = (health.body or {}).get("watching") if health.ok and isinstance(health.body, dict) else None
        all_connected = bool(watching and watching.get("machines") and
                             watching.get("connected") == watching.get("machines"))
        result["initial_health"] = {
            "api_up": initial_ok,
            "latency_ms": round(health.latency_s * 1000, 1) if health.ok else None,
            "machines_connected": watching,
            "components": components,
        }
        host_snapshot = resources.host_snapshot()
        result["initial_resources"] = {
            "host": host_snapshot.as_dict(),
            "plant_components": components,
            "plant_rss_mb_total": round(sum(c["rss_mb"] or 0 for c in components.values()), 1),
        }
        problems = []
        if not initial_ok:
            problems.append("plant API is not healthy; start it with scripts/start.sh")
        elif not all_connected:
            problems.append(f"not every machine is connected: {watching}")
        missing = [name for name, c in components.items() if not c["alive"]]
        if missing:
            problems.append(f"plant components not running: {missing}")
        max_rss = float(self.settings.policy["resources"].get("max_plant_rss_mb", 1e9))
        if result["initial_resources"]["plant_rss_mb_total"] > max_rss:
            problems.append(f"plant memory {result['initial_resources']['plant_rss_mb_total']} MB exceeds "
                            f"max_plant_rss_mb={max_rss:g}")
        ctx = build_context(self.settings)
        ctx.stations = self.stations()
        for index, step in enumerate(decision.normalised):
            fault = fault_registry.FAULTS[step["fault"]["type"]]
            for check in fault.preconditions(ctx, step["target"], step["fault"]["parameters"]):
                if not check.ok:
                    problems.append(f"step {index + 1} precondition {check.name}: {check.detail}")
        if problems:
            return self._finish(result, tracker, State.REJECTED, "rejected_preconditions", problems)

        integrity, integrity_detail = self.plant.database_integrity()
        result["initial_resources"]["database_integrity"] = {"ok": integrity, "detail": integrity_detail}

        profile = load_profile(self.settings, experiment.workload)
        generator = WorkloadGenerator(
            self.plant, profile, float(self.settings.policy["workload"]["max_requests_per_second"]))
        sampler = Sampler(self.metrics, float(self.settings.lab["sampling"]["interval_s"]))
        orders_before = self.plant.orders()
        result["production_measurements"] = {
            "orders_before": orders_before.body if orders_before.ok else None,
        }

        aborted = False
        step_results: list[dict] = []
        cleanup_steps: list[dict] = []
        recovery_report: dict | None = None
        injected_any = False
        recovery_timeout = float(experiment.recovery.get("timeout_s", limits["recovery_timeout_s"]))
        verifier = RecoveryVerifier(self.plant, list(self.settings.allowlist["plants"][self.settings.plant_name]
                                                     ["components"]))
        baseline_max_id: int | None = None

        try:
            sampler.start()
            generator.start()
            tracker.to(State.BASELINE)
            sampler.set_phase("baseline")
            aborted = self._sleep(experiment.baseline_s)
            baseline_samples = sampler.by_phase("baseline")
            ids = [s["tag_values_max_id"] for s in baseline_samples if s["tag_values_max_id"] is not None]
            baseline_max_id = max(ids) if ids else None

            for index, step in enumerate(decision.normalised):
                if aborted:
                    break
                fault = fault_registry.FAULTS[step["fault"]["type"]]
                params = step["fault"]["parameters"]
                target = step["target"]
                hold = min(fault.hold_seconds(params), float(limits["max_fault_duration_s"]))
                step_entry: dict[str, Any] = {"index": index + 1, "type": fault.spec.name, "target": target,
                                              "parameters": params, "hold_s": hold, "injected": False}
                step_results.append(step_entry)

                tracker.to(State.INJECTING, step=index + 1)
                sampler.set_phase("fault")
                intent = fault.intent(target, params)
                self.lease.add_record(intent)
                step_entry["injection_started_at"] = now_iso()
                try:
                    record = fault.inject(ctx, target, params)
                except Exception as exc:
                    step_entry["error"] = f"{type(exc).__name__}: {exc}"
                    step_entry["injection_finished_at"] = now_iso()
                    self.audit.record(run_id, "injection_failed", step=index + 1, error=step_entry["error"])
                    tracker.to(State.CLEANUP, step=index + 1)
                    outcomes = cleanup_records(self.settings, [intent], self.audit, run_id, "controller")
                    cleanup_steps.extend(outcomes)
                    self.lease.clear_records()
                    break
                step_entry["injected"] = fault.spec.name != "none"
                injected_any = injected_any or step_entry["injected"]
                step_entry["record"] = record
                step_entry["injection_finished_at"] = now_iso()
                records = (self.lease.read() or {}).get("records", [])
                if records:
                    records[-1] = record
                    self.lease.update(records=records)
                self.audit.record(run_id, "fault_injected", step=index + 1, fault=fault.spec.name, record=record)

                tracker.to(State.FAULT_ACTIVE, step=index + 1)
                aborted = self._sleep(hold) or aborted

                tracker.to(State.CLEANUP, step=index + 1)
                cleanup_started = time.monotonic()
                outcomes = cleanup_records(self.settings, [record], self.audit, run_id, "controller")
                cleanup_steps.extend(outcomes)
                step_entry["cleanup_finished_at"] = now_iso()
                step_entry["cleanup_duration_s"] = round(time.monotonic() - cleanup_started, 2)
                step_entry["cleanup_ok"] = all(o["ok"] for o in outcomes)
                self.lease.clear_records()
                if not step_entry["cleanup_ok"]:
                    break
                if index < len(decision.normalised) - 1 and not aborted:
                    sampler.set_phase("recovery")
                    tracker.to(State.RECOVERING)
                    tracker.to(State.VERIFYING)
                    interim = verifier.wait(sampler.snapshot, {"ok": True, "detail": "step cleanup verified"},
                                            recovery_timeout, baseline_max_id,
                                            should_stop=self.lease.stop_requested)
                    step_entry["interim_recovery"] = {k: interim[k] for k in ("recovered", "duration_s")}
                    aborted = self.lease.stop_requested() or aborted
                    if not interim["recovered"]:
                        break
        except BaseException:
            sampler.stop()
            generator.stop()
            raise

        cleanup_ok = all(o["ok"] for o in cleanup_steps) if cleanup_steps else True
        removed_detail = "; ".join(f"{o.get('type')}: {o['removed']['detail']}" for o in cleanup_steps
                                    if "removed" in o) or "no fault was injected"
        injection_failed = any(s.get("error") for s in step_results)

        if aborted:
            tracker.to(State.ABORTED, reason="emergency stop requested")
        elif not cleanup_ok:
            tracker.to(State.FAILED, reason="cleanup could not be verified")
        else:
            if tracker.state not in (State.RECOVERING, State.VERIFYING):
                tracker.to(State.RECOVERING)
            sampler.set_phase("recovery")
            if tracker.state != State.VERIFYING:
                tracker.to(State.VERIFYING)
            recovery_report = verifier.wait(sampler.snapshot, {"ok": True, "detail": removed_detail},
                                            recovery_timeout, baseline_max_id,
                                            should_stop=self.lease.stop_requested)
            if recovery_report["recovered"] and not injection_failed:
                sampler.set_phase("post")
                self._sleep(float(self.settings.lab["sampling"].get("post_s", 10)))

        all_samples = sampler.stop()
        workload_report = generator.stop()
        finished_at = datetime.now(timezone.utc)

        result["fault_injection"] = {"injected": injected_any, "steps": step_results}
        result["workload"] = workload_report
        result["cleanup"] = {"ok": cleanup_ok, "steps": cleanup_steps}
        result["observed_metrics"] = {
            phase: self.metrics.summarise([s for s in all_samples if s["phase"] == phase])
            for phase in ("baseline", "fault", "recovery", "post")
        }
        result["samples_file"] = "samples.jsonl"
        (run_dir / "samples.jsonl").write_text(
            "\n".join(json.dumps(s, separators=(",", ":")) for s in all_samples) + "\n", encoding="utf-8")

        orders_after = self.plant.orders()
        result["production_measurements"]["orders_after"] = orders_after.body if orders_after.ok else None
        result["production_measurements"]["good_qty_baseline_start"] = next(
            (s["work_orders"]["good_qty"] for s in all_samples if s["work_orders"]["good_qty"] is not None), None)
        result["production_measurements"]["good_qty_end"] = next(
            (s["work_orders"]["good_qty"] for s in reversed(all_samples)
             if s["work_orders"]["good_qty"] is not None), None)
        result["production_measurements"]["oee"] = self._oee_snapshot()
        result["scoring"] = {
            "method": experiment.scoring,
            "note": ("The upstream scoring harness replays a scripted hour in its own ephemeral plant; it cannot "
                     "score a live persistent plant, so it is not applied to infrastructure faults."),
            "applied": False,
        }
        result["logs"] = self.logs.collect(started_at - timedelta(seconds=5),
                                           finished_at + timedelta(seconds=2), run_dir / "logs")

        final = self.plant.health()
        final_components = self.plant.process_table()
        final_watching = (final.body or {}).get("watching") if final.ok and isinstance(final.body, dict) else None
        result["final_health"] = {
            "api_up": bool(final.ok and isinstance(final.body, dict) and final.body.get("status") == "ok"),
            "latency_ms": round(final.latency_s * 1000, 1) if final.ok else None,
            "machines_connected": final_watching,
            "components": final_components,
            "host": resources.host_snapshot().as_dict(),
        }
        result["recovery"] = recovery_report if recovery_report else {
            "recovered": False, "duration_s": None, "note": "recovery not verified (experiment aborted)"}
        result["limitations"] = self._infrastructure_limitations(experiment)

        injection_errors = [s.get("error") for s in step_results if s.get("error")]
        if aborted:
            overall = "aborted" if cleanup_ok else "unsafe_cleanup_failed"
            return self._finish(result, tracker, State.ABORTED, overall,
                                ["emergency stop requested"] + ([] if cleanup_ok else ["cleanup failed"]))
        if not cleanup_ok:
            return self._finish(result, tracker, State.FAILED, "unsafe_cleanup_failed",
                                ["cleanup or fault removal could not be verified"])
        if injection_errors:
            return self._finish(result, tracker, State.FAILED, "injection_failed", injection_errors)
        report = result["recovery"]
        stages = report["stages"]
        core = all(stages[s]["ok"] for s in ("fault_removed", "service_available", "operation_succeeds",
                                              "manufacturing_behaviour"))
        reasons = [f"{name}: {stage['detail']}" for name, stage in stages.items() if not stage["ok"]]
        if core and report["recovered"]:
            return self._finish(result, tracker, State.COMPLETED, "recovered", [])
        if core:
            return self._finish(result, tracker, State.COMPLETED, "recovered_with_data_findings", reasons)
        return self._finish(result, tracker, State.COMPLETED, "not_recovered", reasons)

    def _oee_snapshot(self) -> dict | None:
        states = self.plant.equipment_states()
        if not states.ok or not isinstance(states.body, list):
            return None
        snapshot = {}
        for row in states.body:
            code = row["equipment"]
            response = self.plant.oee(code, 0.1)
            if not response.ok or not isinstance(response.body, dict):
                snapshot[code] = None
                continue
            body = response.body
            snapshot[code] = {
                "availability": body.get("availability"),
                "performance": body.get("performance"),
                "quality": body.get("quality"),
                "oee": body.get("oee"),
                "coverage": body.get("coverage"),
                "coverage_note": body.get("coverage_note"),
                "window_hours": body.get("window_hours"),
            }
        return snapshot

    def _infrastructure_limitations(self, experiment: Experiment) -> list[str]:
        return [
            "Single-host lightweight deployment: processes share one machine, so faults are process-level, not "
            "network- or container-level.",
            "Restarted components write their console output to runtime/logs/component-*.log instead of the "
            "plant's log sink; their structured JSONL logs continue in runtime/work/logs.",
            "The plant replays a recorded line at the configured speed; a restarted OPC server restarts its "
            "replay from the beginning of the recording.",
            "Availability and OEE are withheld by the application when coverage is below the pack's floor; "
            "such values are reported as unavailable, not zero.",
            "The upstream scoring harness only scores its own ephemeral replay and is not applied here.",
        ]

    def _run_application(self, experiment: Experiment, decision: Decision, run_id: str, result: dict,
                         tracker: StateTracker) -> dict:
        run_dir = self.store.directory(run_id)
        app = experiment.application
        speed = float(app.get("speed", self.settings.lab["application_runs"]["default_speed"]))
        seed = int(app.get("seed", self.settings.lab["application_runs"]["default_seed"]))
        duration = int(app["duration_s"])
        measure = list(app.get("measure", ["booking", "downtime", "oee"]))
        events = []
        for step in decision.normalised:
            fault = fault_registry.FAULTS[step["fault"]["type"]]
            if isinstance(fault, fault_registry.SimulatorFault):
                events.append(fault.event(step["target"], step["fault"]["parameters"]))

        tracker.to(State.BASELINE)
        result["initial_health"] = {"api_up": None, "note": "application experiments run in an ephemeral plant "
                                    "built by the upstream harness; the persistent plant is not involved"}
        result["workload"] = {"profile": "upstream floor simulator",
                              "description": "the harness's own simulated floor, replay and OPC agent",
                              "speed": speed, "seed": seed, "duration_line_seconds": duration, "measure": measure,
                              "note": "latency is added to the harness measurements when no other measurement "
                                      "enables its per-look watcher, because recovery evidence is read from "
                                      "the per-look station states"}
        watcher_measures = {"latency", "console", "connection"}
        harness_measure = list(measure)
        if not watcher_measures & set(measure):
            harness_measure.append("latency")
        plan = self.scoring.build_plan(run_dir / "harness", experiment.name, experiment.description, duration,
                                       speed, seed, harness_measure, events)
        result["fault_injection"] = {
            "injected": bool(events),
            "layer": "simulator",
            "note": "scripted into the replayed line; this is a simulated manufacturing fault, not an "
                    "infrastructure fault",
            "events": events,
            "plan": str(plan.relative_to(self.settings.root)) if plan.is_relative_to(self.settings.root) else str(plan),
        }

        host_sampler = HostSampler(2.0)
        started_at = datetime.now(timezone.utc)
        tracker.to(State.INJECTING)
        host_sampler.start()
        tracker.to(State.FAULT_ACTIVE)
        timeout = float(self.settings.lab["application_runs"]["timeout_s"])
        returncode, harness_dir, reason = self.scoring.run(
            plan, run_dir / "harness" / "results", timeout, run_dir / "harness.log",
            stop_requested=self.lease.stop_requested)
        host_samples = host_sampler.stop()
        finished_at = datetime.now(timezone.utc)

        tracker.to(State.CLEANUP)
        leftovers = self.scoring.leftover_processes()
        terminated = []
        for pid in leftovers:
            try:
                os.kill(pid, 15)
                terminated.append(pid)
            except OSError:
                pass
        time.sleep(1.0 if terminated else 0.0)
        still = self.scoring.leftover_processes()
        cleanup_ok = not still
        result["cleanup"] = {
            "ok": cleanup_ok,
            "steps": [{"type": "ephemeral_plant_teardown", "ok": cleanup_ok,
                       "detail": f"leftover processes after harness: {len(leftovers)}, terminated: "
                                 f"{len(terminated)}, remaining: {len(still)}"}],
        }
        result["observed_metrics"] = {"run": HostSampler.summarise(host_samples)}
        (run_dir / "host-samples.jsonl").write_text(
            "\n".join(json.dumps(s, separators=(",", ":")) for s in host_samples) + "\n", encoding="utf-8")

        scores = self.scoring.load_scores(harness_dir) if harness_dir else None
        watched = self.scoring.load_watched(harness_dir, self.settings.plant_name) if harness_dir else None
        derived = self.scoring.derive(scores, watched, events, duration) if scores else {
            "available": False, "reason": "the harness produced no scores.json"}
        derived["method"] = "fsmes lab run (upstream scoring harness)"
        derived["harness_exit_code"] = returncode
        derived["harness_end_reason"] = reason
        derived["harness_directory"] = (
            str(harness_dir.relative_to(self.settings.root)) if harness_dir and harness_dir.is_relative_to(
                self.settings.root) else str(harness_dir))
        result["scoring"] = derived

        production = {"truth_line_good": None, "mes_booked_last_station": None, "mes_booked_all_stations": None}
        if derived.get("booking") and derived["booking"].get("line"):
            line = derived["booking"]["line"]
            production = {"truth_line_good": line.get("truth_good"),
                          "mes_booked_last_station": line.get("mes_good_last_station"),
                          "mes_booked_all_stations": line.get("mes_good_all_stations")}
        result["production_measurements"] = production

        result["logs"] = {
            "available": bool(harness_dir),
            "harness_log": "harness.log",
            "note": "the ephemeral plant's component logs are kept by the harness only with --keep-evidence; the "
                    "harness console log and the line-by-line watched states are in the run directory",
            "window": [started_at.isoformat(), finished_at.isoformat()],
        }
        result["final_health"] = {"applicable": False,
                                  "note": "ephemeral plant discarded; leftover processes: " + str(len(still))}
        result["limitations"] = [
            "Simulated manufacturing faults are scripted events in a replayed line, not infrastructure failures.",
            "The scenario runs inside the upstream harness's own ephemeral plant at a compressed clock; it does "
            "not touch the persistent lab plant.",
            "The booking comparison carries an overlap band the harness states; differences inside it are not "
            "evidence of error.",
            "Station states are sampled once per wall-clock second by the harness, not continuously.",
        ]

        tracker.to(State.RECOVERING)
        tracker.to(State.VERIFYING)
        result["recovery"] = self._application_recovery(events, derived, scores is not None, returncode, duration,
                                                         cleanup_ok, reason)
        if reason == "stopped":
            return self._finish(result, tracker, State.ABORTED, "aborted", ["emergency stop requested"])
        if not cleanup_ok:
            return self._finish(result, tracker, State.FAILED, "unsafe_cleanup_failed",
                                ["ephemeral plant processes remain after the harness ended"])
        if reason == "timeout" or returncode not in (0,) or scores is None:
            return self._finish(result, tracker, State.FAILED, "error",
                                [f"harness did not complete (exit {returncode}, {reason})"])
        stages = result["recovery"]["stages"]
        core = all(stages[s]["ok"] for s in ("fault_removed", "service_available", "operation_succeeds",
                                              "manufacturing_behaviour"))
        reasons = [f"{n}: {s['detail']}" for n, s in stages.items() if not s["ok"]]
        if core and result["recovery"]["recovered"]:
            return self._finish(result, tracker, State.COMPLETED, "recovered", [])
        if core:
            return self._finish(result, tracker, State.COMPLETED, "recovered_with_data_findings", reasons)
        return self._finish(result, tracker, State.COMPLETED, "not_recovered", reasons)

    def _application_recovery(self, events: list[dict], derived: dict, have_scores: bool, returncode: int | None,
                              duration: int, cleanup_ok: bool, reason: str = "completed") -> dict:
        timeline = derived.get("state_timeline") or {}
        windows = timeline.get("windows") or []
        watched_failures_known = timeline.get("available", False)
        ended_inside = all((e.get("end", e.get("at", 0)) or 0) <= duration for e in events)
        if reason == "stopped":
            fault_removed = {"ok": cleanup_ok, "detail": "run stopped early; ephemeral plant torn down and "
                                                          "verified" if cleanup_ok else "teardown problem"}
        else:
            fault_removed = {"ok": ended_inside and cleanup_ok,
                             "detail": "scripted windows ended inside the replayed time; ephemeral plant torn down"
                             if ended_inside and cleanup_ok else "scripted window or teardown problem"}

        service = {"ok": bool(have_scores and returncode == 0 and watched_failures_known),
                   "detail": f"harness exit {returncode}; scores.json present: {have_scores}; per-look states "
                             f"recorded: {watched_failures_known}"}

        oee = derived.get("oee") or {}
        booking = derived.get("booking") or {}
        answered = bool(oee.get("stations_total")) and oee.get("stations_answered") == oee.get("stations_total") \
            and booking.get("stations_answered") == booking.get("stations_total")
        operation = {"ok": answered,
                     "detail": f"OEE answered {oee.get('stations_answered')}/{oee.get('stations_total')} stations; "
                               f"booking answered {booking.get('stations_answered')}/"
                               f"{booking.get('stations_total')}"}

        behaviour_ok: bool | None = True
        details = []
        for window in windows:
            if window.get("looks_after_window") in (None, 0) and "window_line_seconds" in window:
                behaviour_ok = False if behaviour_ok else behaviour_ok
                details.append(f"{window.get('event')}: no looks after the window, recovery not observable")
                continue
            if "window_line_seconds" not in window:
                final = timeline.get("final_states") or {}
                if not final:
                    behaviour_ok = False
                    details.append(f"{window.get('event')}: no per-look states to judge")
                    continue
                bad = {e: s for e, s in final.items() if s in ("down", "stopped", "changeover")}
                if bad:
                    behaviour_ok = False
                    details.append(f"{window.get('event')}: at the last look still {bad}")
                else:
                    details.append(f"{window.get('event')} (no fixed window): states at the last look {final}")
                continue
            final = window.get("states_at_last_look_after_window")
            if final is None:
                behaviour_ok = False
                details.append(f"{window.get('event')}: no state recorded after the window")
                continue
            bad = {e: s for e, s in final.items() if s in ("down", "stopped", "changeover")}
            if bad:
                behaviour_ok = False
                details.append(f"{window.get('event')}: after the window still {bad}")
            else:
                details.append(f"{window.get('event')}: states after window {final}")
        if not windows:
            final = timeline.get("final_states") or {}
            bad = {e: s for e, s in final.items() if s in ("down", "stopped", "changeover")}
            behaviour_ok = bool(final) and not bad
            details.append(f"final states {final}" if final else "no per-look states available")
        behaviour = {"ok": bool(behaviour_ok), "detail": "; ".join(details) or "no scripted window"}

        data_ok = True
        data_details = []
        judged = booking.get("stations_judged")
        if judged:
            in_band = booking.get("stations_in_band")
            data_ok = in_band == judged
            data_details.append(f"booking: {in_band}/{judged} stations inside the harness's replay band")
        else:
            data_ok = False
            data_details.append("booking comparison unavailable")
        downtime = derived.get("downtime") or {}
        misclassified = downtime.get("planned_stops_misclassified_as_downtime")
        if misclassified:
            data_ok = False
            data_details.append(f"{misclassified} planned stop(s) misclassified as downtime")
        data = {"ok": data_ok, "detail": "; ".join(data_details), "integrity_available": None}

        stages = {"fault_removed": fault_removed, "service_available": service, "operation_succeeds": operation,
                  "manufacturing_behaviour": behaviour, "data_consistency": data}
        recovered = all(s["ok"] for s in stages.values())
        return {"recovered": recovered, "duration_s": None,
                "note": "application-level evidence from the harness run; no wall-clock recovery duration is "
                        "defined because the plant is replayed at a compressed clock",
                "stages": stages}
