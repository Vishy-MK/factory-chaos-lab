from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chaoslab import model
from chaoslab.adapters.factorysemantics import PlantError
from chaoslab.settings import ConfigError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REJECTED = 2
EXIT_FINDINGS = 3
EXIT_USAGE = 64


def _emit(payload, as_json: bool, text: str | None = None) -> None:
    if as_json or text is None:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(text.rstrip("\n") + "\n")


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")


def _warn(message: str) -> None:
    sys.stderr.write(message + "\n")


def _api(root: str | None):
    from chaoslab.api import LabApi

    return LabApi(Path(root) if root else None)


def _lifecycle(root: str | None):
    from chaoslab import settings as settings_module
    from chaoslab.lifecycle import Lifecycle

    return Lifecycle(settings_module.load(Path(root) if root else None))


def cmd_check(args) -> int:
    life = _lifecycle(args.root)
    checks = life.prerequisites()
    if args.json:
        _emit(checks, True)
    else:
        for check in checks:
            mark = "ok  " if check["ok"] else ("FAIL" if check["required"] else "warn")
            _say(f"[{mark}] {check['name']}: {check['detail']}")
    failed = [c for c in checks if c["required"] and not c["ok"]]
    return EXIT_FAILED if failed else EXIT_OK


def cmd_prepare(args) -> int:
    life = _lifecycle(args.root)
    actions = life.prepare(force=args.force)
    if args.json:
        _emit({"actions": actions}, True)
    else:
        for action in actions or ["nothing to do: the lab is already prepared"]:
            _say(action)
    return EXIT_OK


def cmd_start(args) -> int:
    life = _lifecycle(args.root)
    report = life.start(with_exporter=args.with_exporter)
    _emit(report, args.json, f"plant healthy after {report['seconds']}s"
          + (f"; exporter pid {report.get('exporter_pid')}" if report.get("exporter_pid") else ""))
    return EXIT_OK


def cmd_stop(args) -> int:
    life = _lifecycle(args.root)
    report = life.stop()
    ok = not report["remaining_components"]
    _emit(report, args.json, "plant stopped" if ok else f"components still running: {report['remaining_components']}")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_status(args) -> int:
    life = _lifecycle(args.root)
    status = life.status()
    if args.json:
        _emit(status, True)
    else:
        _say(f"plant {status['plant']} at {status['api_url']}: {'UP' if status['api_up'] else 'DOWN'}"
             + (f" ({status['api_latency_ms']} ms)" if status["api_latency_ms"] is not None else ""))
        if status["machines"]:
            m = status["machines"]
            _say(f"machines: {m.get('connected')}/{m.get('machines')} connected")
        if status["floor"] and status["floor"].get("seconds_since") is not None:
            _say(f"floor last recorded a check {status['floor'].get('seconds_since')}s ago")
        elif status["floor"]:
            _say("floor: no floor simulator in this deployment")
        for name, row in status["components"].items():
            _say(f"  {name:<10} {row['status']:<9} pid={row['pid']} rss={row['rss_mb']} MB")
        _say(f"plant RSS total {status['plant_rss_mb_total']} MB; host available "
             f"{status['host']['available_memory_mb']} MB, CPU {status['host']['cpu_percent']}%")
        _say(f"exporter pid: {status['exporter_pid']}")
    return EXIT_OK if status["api_up"] else EXIT_FAILED


def cmd_health(args) -> int:
    from chaoslab.lifecycle import is_healthy

    life = _lifecycle(args.root)
    status = life.status()
    machines = status["machines"] or {}
    healthy = is_healthy(status)
    _emit({"healthy": healthy, **status}, args.json,
          "HEALTHY" if healthy else "UNHEALTHY: " + json.dumps(
              {"api_up": status["api_up"], "machines": machines,
               "components": {k: v["status"] for k, v in status["components"].items()}}))
    return EXIT_OK if healthy else EXIT_FAILED


def cmd_faults(args) -> int:
    specs = _api(args.root).discover_fault_types()
    if args.json:
        _emit(specs, True)
        return EXIT_OK
    for spec in specs:
        state = "supported" if spec["supported"] else "UNSUPPORTED"
        _say(f"{spec['name']:<20} {spec['layer']:<15} {state}")
        _say(f"    {spec['summary']}")
        if not spec["supported"]:
            _say(f"    reason: {spec['unsupported_reason']}")
        for param in spec["parameters"]:
            bounds = ""
            if param["minimum"] is not None or param["maximum"] is not None:
                bounds = f" [{param['minimum']}..{param['maximum']}]"
            _say(f"      - {param['name']} ({param['type']}){bounds} default={param['default']}"
                 f"{' required' if param['required'] else ''}")
    return EXIT_OK


def cmd_targets(args) -> int:
    targets = _api(args.root).discover_targets()
    if args.json:
        _emit(targets, True)
        return EXIT_OK
    _say(f"hosts: {', '.join(targets['hosts'])}")
    for name, plant in targets["plants"].items():
        _say(f"plant {name}: components {', '.join(plant['components'])}; database {plant['database']}")
    _say(f"stations: {', '.join(targets['stations'])}")
    for fault, allowed in targets["fault_targets"].items():
        _say(f"  {fault}: {', '.join(allowed)}")
    _say("workload profiles: " + ", ".join(f"{p['name']} ({p['requests_per_second']} rps)"
                                          for p in targets["workload_profiles"]))
    return EXIT_OK


def cmd_experiments(args) -> int:
    entries = _api(args.root).list_experiments()
    if args.json:
        _emit(entries, True)
        return EXIT_OK
    for entry in entries:
        if entry.get("valid"):
            _say(f"{entry['name']:<34} {entry['category']:<15} {','.join(entry['fault_types'])}")
            _say(f"    {entry['description']}")
        else:
            _say(f"{entry['name']:<34} INVALID: {'; '.join(entry['problems'])}")
    return EXIT_OK


def cmd_validate(args) -> int:
    api = _api(args.root)
    try:
        experiment = model.load_file(Path(args.file))
    except model.DefinitionError as exc:
        _emit({"allowed": False, "stage": "schema", "reasons": exc.problems}, args.json,
              "REJECTED (schema): " + "; ".join(exc.problems))
        return EXIT_REJECTED
    decision = api.controller.validate(experiment)
    _emit(decision.as_dict(), args.json,
          ("ALLOWED" if decision.allowed else "REJECTED (policy): " + "; ".join(decision.reasons)))
    return EXIT_OK if decision.allowed else EXIT_REJECTED


def _summary_text(result: dict) -> str:
    recovery = result.get("recovery") or {}
    lines = [
        f"experiment {result['experiment_id']}",
        f"result: {result['overall_result']}",
    ]
    if result.get("reasons"):
        for reason in result["reasons"]:
            lines.append(f"  - {reason}")
    injection = result.get("fault_injection") or {}
    lines.append(f"fault injected: {injection.get('injected')}")
    cleanup = result.get("cleanup") or {}
    lines.append(f"cleanup ok: {cleanup.get('ok')}")
    if recovery:
        lines.append(f"recovered: {recovery.get('recovered')} (duration {recovery.get('duration_s')}s)")
        for name, stage in (recovery.get("stages") or {}).items():
            lines.append(f"    {name:<24} {'ok' if stage['ok'] else 'NOT OK'}: {stage['detail']}")
    lines.append(f"result file: results/{result['experiment_id']}/result.json")
    return "\n".join(lines)


def cmd_run(args) -> int:
    from chaoslab import proctitle

    proctitle.enable(experiments=True)
    api = _api(args.root)
    try:
        experiment = model.load_file(Path(args.file))
    except model.DefinitionError as exc:
        _emit({"overall_result": "rejected", "stage": "schema", "reasons": exc.problems}, args.json,
              "REJECTED (schema): " + "; ".join(exc.problems))
        return EXIT_REJECTED
    result = api.controller.run(experiment, dry_run=args.dry_run)
    _emit(result, args.json, _summary_text(result))
    return _exit_code(result)


def _exit_code(result: dict) -> int:
    outcome = result.get("overall_result")
    if outcome in ("recovered", "dry_run_ok"):
        return EXIT_OK
    if outcome == "recovered_with_data_findings":
        return EXIT_FINDINGS
    if outcome in ("rejected", "rejected_preconditions"):
        return EXIT_REJECTED
    return EXIT_FAILED


def cmd_execute(args) -> int:
    from chaoslab import proctitle

    proctitle.enable(experiments=True)
    proctitle.experiment(args.run_id, "starting")
    api = _api(args.root)
    result = api.execute_prepared(Path(args.file), args.run_id)
    return _exit_code(result)


def cmd_submit(args) -> int:
    api = _api(args.root)
    import yaml

    definition = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    response = api.submit(definition)
    _emit(response, args.json, json.dumps(response))
    return EXIT_OK if response["accepted"] else EXIT_REJECTED


def cmd_active(args) -> int:
    status = _api(args.root).status()
    active = status["active"]
    if args.json:
        _emit(status, True)
    elif active:
        _say(f"active: {active['experiment_id']} state={active['state']} controller_pid={active['controller_pid']}")
    else:
        _say("no experiment is active")
    return EXIT_OK


def cmd_last(args) -> int:
    api = _api(args.root)
    try:
        result = api.result("latest")
    except FileNotFoundError as exc:
        _warn(str(exc))
        return EXIT_FAILED
    _emit(result, args.json, _summary_text(result))
    return EXIT_OK


def cmd_show(args) -> int:
    api = _api(args.root)
    try:
        result = api.result(args.experiment)
    except FileNotFoundError as exc:
        _warn(str(exc))
        return EXIT_FAILED
    _emit(result, args.json, _summary_text(result))
    return EXIT_OK


def cmd_report(args) -> int:
    from chaoslab.report import render

    api = _api(args.root)
    try:
        result = api.result(args.experiment)
    except FileNotFoundError as exc:
        _warn(str(exc))
        return EXIT_FAILED
    _emit(result, args.json, render(result))
    return EXIT_OK


def cmd_runs(args) -> int:
    api = _api(args.root)
    runs = api.store.list_runs()
    rows = []
    for run_id in runs[: args.limit]:
        result = api.store.read_result(run_id)
        rows.append({"experiment_id": run_id, "overall_result": result.get("overall_result"),
                     "category": (result.get("definition") or {}).get("category")})
    if args.json:
        _emit(rows, True)
    else:
        for row in rows:
            _say(f"{row['experiment_id']:<60} {row['category']:<15} {row['overall_result']}")
        if not rows:
            _say("no results yet")
    return EXIT_OK


def cmd_compare(args) -> int:
    api = _api(args.root)
    try:
        comparison = api.compare(args.first, args.second)
    except FileNotFoundError as exc:
        _warn(str(exc))
        return EXIT_FAILED
    if args.json:
        _emit(comparison, True)
    else:
        _say(f"{comparison['first']}  vs  {comparison['second']}")
        for row in comparison["rows"]:
            if row["metric"] in ("experiment_id",):
                continue
            first = "unavailable" if row["first"] is None else row["first"]
            second = "unavailable" if row["second"] is None else row["second"]
            delta = "" if row["delta"] is None else f"  (delta {row['delta']:+g})"
            _say(f"  {row['metric']:<36} {first!s:<28} {second!s:<28}{delta}")
    return EXIT_OK


def cmd_resources(args) -> int:
    from chaoslab import resources, settings as settings_module

    settings = settings_module.load(Path(args.root) if args.root else None)
    payload = resources.gate_report(settings.policy)
    allowed, reasons, host = payload["experiments_allowed"], payload["reasons"], payload["host"]
    life = _lifecycle(args.root)
    payload["plant_rss_mb_total"] = life.status()["plant_rss_mb_total"]
    _emit(payload, args.json, (
        f"available memory {host['available_memory_mb']:.0f} MB (threshold "
        f"{payload['threshold_available_mb']} MB), CPU {host['cpu_percent']:.0f}%, "
        f"plant RSS {payload['plant_rss_mb_total']} MB: "
        + ("experiments allowed" if allowed else "experiments refused: " + "; ".join(reasons))))
    return EXIT_OK if allowed else EXIT_FAILED


def cmd_emergency_stop(args) -> int:
    report = _api(args.root).emergency_stop()
    summary = (f"emergency stop: active experiment {report['active_experiment']}; "
               f"forced cleanup {report['forced_cleanup']}; ok={report['ok']}")
    _emit(report, args.json, summary)
    return EXIT_OK if report["ok"] else EXIT_FAILED


def cmd_collect_logs(args) -> int:
    from datetime import datetime, timedelta, timezone

    from chaoslab import settings as settings_module
    from chaoslab.adapters.logs import LogCollector

    settings = settings_module.load(Path(args.root) if args.root else None)
    collector = LogCollector(settings)
    until = datetime.now(timezone.utc)
    since = until - timedelta(minutes=args.minutes)
    destination = settings.results_dir / "_logs" / until.strftime("%Y%m%dT%H%M%SZ")
    summary = collector.collect(since, until, destination)
    _emit(summary, args.json, f"logs collected in {destination}: "
          + json.dumps(summary.get("files", summary)))
    return EXIT_OK if summary.get("available") else EXIT_FAILED


def cmd_export(args) -> int:
    from chaoslab import settings as settings_module
    from chaoslab.store import ResultStore

    settings = settings_module.load(Path(args.root) if args.root else None)
    store = ResultStore(settings.results_dir)
    destination = Path(args.out) if args.out else settings.results_dir / "summary.csv"
    count = store.export_csv(destination)
    _emit({"rows": count, "file": str(destination)}, args.json, f"wrote {count} row(s) to {destination}")
    return EXIT_OK


def cmd_exporter(args) -> int:
    from chaoslab import proctitle
    from chaoslab.observability.exporter import serve

    proctitle.set_name("cl:exporter")
    return serve(Path(args.root) if args.root else None)


def cmd_workload(args) -> int:
    import time

    from chaoslab import settings as settings_module
    from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter
    from chaoslab.workload.generator import WorkloadGenerator, load_profile

    settings = settings_module.load(Path(args.root) if args.root else None)
    limit = float(settings.policy["limits"]["max_fault_duration_s"]) * 5
    if args.seconds > limit:
        _warn(f"refusing: --seconds {args.seconds} exceeds the bound {limit:g}")
        return EXIT_REJECTED
    profile = load_profile(settings, args.profile)
    max_rps = float(settings.policy["workload"]["max_requests_per_second"])
    if profile.get("high_volume") and not settings.policy["workload"].get("high_volume_enabled"):
        _warn("refusing: high-volume workload profiles are disabled by safety policy")
        return EXIT_REJECTED
    plant = FactorySemanticsAdapter(settings)
    generator = WorkloadGenerator(plant, profile, max_rps)
    generator.start()
    time.sleep(args.seconds)
    report = generator.stop()
    _emit(report, args.json, json.dumps(report, indent=2))
    return EXIT_OK



def cmd_mcp(args) -> int:
    from chaoslab import proctitle
    from chaoslab.mcp_server import main as serve

    proctitle.set_name("cl:mcp")
    serve(Path(args.root) if args.root else None)
    return EXIT_OK


def cmd_agent(args) -> int:
    from chaoslab import proctitle
    from chaoslab.agents import commands
    from chaoslab.agents.llm import LLMError
    from chaoslab.agents.runtime import approve_all, ask_terminal

    root = Path(args.root) if args.root else None
    proctitle.enable()
    try:
        if args.agent_command == "inject":
            code, payload = commands.inject(root, args.experiment, approve_all if args.approve else ask_terminal,
                                            task=args.task)
        else:
            code, payload = commands.analyze(root, args.experiment_id)
    except (LLMError, FileNotFoundError) as exc:
        _warn(str(exc))
        return EXIT_FAILED
    _emit(payload, True)
    return code

def cmd_deploy(args) -> int:
    from chaoslab import proctitle, settings as settings_module

    settings = settings_module.load(Path(args.root) if args.root else None)
    proctitle.enable()
    if args.command == "gateway":
        from chaoslab.deploy import gateway

        proctitle.set_name("cl:gateway")
        return gateway.run(settings)
    if args.command == "control-api":
        from chaoslab.deploy import control_api

        proctitle.set_name("cl:control")
        try:
            return control_api.serve(settings)
        except ValueError as exc:
            _warn(str(exc))
            return EXIT_FAILED
    from chaoslab.deploy import secure

    report = secure.rotate(settings)
    _emit(report, args.json, f"rotated: {', '.join(report['rotated']) or 'none'}; failed: "
                             f"{report['failed'] or 'none'}; unchanged (fixed by the floor simulator): "
                             f"{', '.join(report['not_rotated'])}")
    return EXIT_OK if not report["failed"] else EXIT_FAILED


def cmd_plan(args) -> int:
    from chaoslab import planning, proctitle, settings as settings_module
    from chaoslab.agents import commands
    from chaoslab.agents.llm import LLMError

    root = Path(args.root) if args.root else None
    settings = settings_module.load(root)
    proctitle.enable()
    try:
        if args.plan_command == "propose":
            code, payload = commands.plan(root, args.analysis_id)
            output = payload.get("output") or {}
            lines = [f"planner saved {len(payload['proposals'])} proposal(s) for {args.analysis_id}"]
            lines += [f"  {p['proposal_id']}: {p['name']} ({p['fault']}) for {p['finding_ids']}\n    {p['rationale']}"
                      for p in payload["proposals"]]
            lines += [f"  not proposed: {n.get('idea')} ({n.get('reason')})" for n in output.get("not_proposed", [])]
            lines.append("approve one with: python -m chaoslab plan approve <proposal-id>")
            _emit(payload, args.json, "\n".join(lines))
            return code
        if args.plan_command == "list":
            proposals = planning.list_proposals(settings, args.status)
            _emit({"proposals": proposals}, args.json, "\n".join(
                f"{p['status']:<9} {p['proposal_id']}  {p['name']} ({p['fault']})" for p in proposals) or "no proposals")
            return EXIT_OK
        if args.plan_command == "show":
            _, record = planning.load(settings, args.proposal_id)
            _emit(record, True)
            return EXIT_OK
        if args.plan_command == "reject":
            record = planning.set_status(settings, args.proposal_id, "rejected", reason=args.reason)
            _emit(record, args.json, f"rejected {record['proposal_id']}")
            return EXIT_OK
        code, payload = commands.approve(root, args.proposal_id, via_agent=not args.direct)
        _emit(payload, True)
        return code
    except (LLMError, FileNotFoundError, ValueError) as exc:
        _warn(str(exc))
        return EXIT_FAILED


def cmd_analysis(args) -> int:
    from chaoslab import settings as settings_module
    from chaoslab.analysis import engine as analysis_engine, service
    from chaoslab.analysis.evidence import EvidenceError

    settings = settings_module.load(Path(args.root) if args.root else None)
    try:
        if args.analysis_command == "baseline":
            payload = service.assess_baseline(settings, args.source, args.observe)
            text = (f"baseline {payload['baseline_id']}: {payload['readiness']} ({payload['samples']} samples from "
                    f"{payload['source']})\n  failed checks: {payload['failed_checks'] or 'none'}; not measured: "
                    f"{payload['unmeasured_checks'] or 'none'}\n  saved {payload['path']}")
        elif args.analysis_command == "analyze":
            payload = service.analyse_failure(settings, args.experiment, args.baseline, args.fault_meta,
                                              with_narrative=not args.no_narrative)
            score = payload["score"]
            lines = [f"analysis {payload['analysis_id']}",
                     f"  baseline readiness {payload['baseline_readiness']}; attribution {payload['attribution']}; "
                     f"recovery {payload['recovery']}",
                     f"  score {score['status']}" + (f" {score['score']}" if score.get("score") is not None
                                                     else f" (missing {score.get('missing_dimensions')})")]
            lines += [f"  {f['id']} {f['severity']:<8} confidence {f['confidence']:<6} {f['symptom']}"
                      for f in payload["findings"]]
            lines += [f"  narrative {payload['narrative_status']}; evidence references resolve: {payload['evidence_ok']}",
                      f"  report {payload['report']}"]
            text = "\n".join(lines)
        elif args.analysis_command == "record":
            import os

            os.environ["CHAOSLAB_DEPLOYMENT"] = "docker"
            from chaoslab.analysis import recorder

            settings = settings_module.load(Path(args.root) if args.root else None)
            print(f"recording the Docker stack read-only for {args.seconds:.0f} s; perform the fault now", flush=True)
            path = recorder.record(settings, args.seconds, args.label)
            data = json.loads(path.read_text(encoding="utf-8"))
            found = data["detection"]
            payload = {"artifact": str(path), "detection": found, "fault": data["fault"]}
            text = (f"recording {path}\n  samples {len(data['samples'])}; " +
                    (f"detected {found['kinds']} on {found['components']} for {found['end'] - found['start']:.1f} s"
                     if found["detected"] else f"no fault detected ({found.get('reason')})") +
                    f"\n  analyse it: python -m chaoslab analysis analyze {path}")
        elif args.analysis_command == "recommend":
            result = service.recommend(settings, args.analysis_id)
            payload = result
            lines = [f"recommender {result['status']}" + (f": {result.get('error')}" if result.get("error") else "")]
            for rec in (result.get("output") or {}).get("recommendations", []):
                lines.append(f"  {rec['finding_id']} {rec['title']} (effort {rec['effort']}, confidence "
                             f"{rec['confidence']}; {len(rec.get('locations') or [])} verified locations)")
            for item in (result.get("output") or {}).get("not_recommended", []):
                lines.append(f"  {item['finding_id']} not recommended: {item['reason']}")
            lines.append(f"  attempts {len(result.get('attempts', []))}")
            text = "\n".join(lines)
        else:
            stored = analysis_engine.read(settings, args.analysis_id)
            directory = Path(settings.results_dir) / "analysis" / stored["analysis_id"]
            payload = stored
            text = (directory / "report.md").read_text(encoding="utf-8")
    except (EvidenceError, FileNotFoundError, ValueError) as exc:
        _warn(str(exc))
        return EXIT_FAILED
    _emit(payload, args.json, text)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chaoslab", description="FactorySemantics chaos laboratory controller")
    parser.add_argument("--root", help="lab root directory (default: the checkout containing this package)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify prerequisites").set_defaults(func=cmd_check)
    prepare = sub.add_parser("prepare", help="build the lab pack, replay data and plant database")
    prepare.add_argument("--force", action="store_true", help="rebuild the lab pack and replay data")
    prepare.set_defaults(func=cmd_prepare)
    start = sub.add_parser("start", help="start the persistent lab plant")
    start.add_argument("--with-exporter", action="store_true", help="also start the Prometheus exporter")
    start.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the lab plant and exporter").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="plant and host status").set_defaults(func=cmd_status)
    sub.add_parser("health", help="exit 0 only if the plant is fully healthy").set_defaults(func=cmd_health)
    sub.add_parser("faults", help="list fault types").set_defaults(func=cmd_faults)
    sub.add_parser("targets", help="list valid targets").set_defaults(func=cmd_targets)
    sub.add_parser("experiments", help="list experiment definitions").set_defaults(func=cmd_experiments)
    validate = sub.add_parser("validate", help="validate an experiment definition against the safety policy")
    validate.add_argument("file")
    validate.set_defaults(func=cmd_validate)
    run = sub.add_parser("run", help="run an experiment (blocking)")
    run.add_argument("file")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)
    execute = sub.add_parser("execute", help=argparse.SUPPRESS)
    execute.add_argument("--run-id", required=True)
    execute.add_argument("file")
    execute.set_defaults(func=cmd_execute)
    submit = sub.add_parser("submit", help="validate and run an experiment in the background")
    submit.add_argument("file")
    submit.set_defaults(func=cmd_submit)
    sub.add_parser("active", help="show the active experiment").set_defaults(func=cmd_active)
    sub.add_parser("last", help="show the most recent result").set_defaults(func=cmd_last)
    show = sub.add_parser("show", help="show one result")
    show.add_argument("experiment")
    show.set_defaults(func=cmd_show)
    runs = sub.add_parser("runs", help="list recorded results")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)
    compare = sub.add_parser("compare", help="compare two results")
    compare.add_argument("first")
    compare.add_argument("second")
    compare.set_defaults(func=cmd_compare)
    report = sub.add_parser("report", help="parameters, outcome and per-phase metrics of one result")
    report.add_argument("experiment", nargs="?", default="latest", help="experiment id (default: the latest)")
    report.set_defaults(func=cmd_report)
    sub.add_parser("resources", help="check resource usage and the memory gate").set_defaults(func=cmd_resources)
    sub.add_parser("emergency-stop", help="stop active fault injection and clean up").set_defaults(
        func=cmd_emergency_stop)
    logs = sub.add_parser("collect-logs", help="collect recent plant logs")
    logs.add_argument("--minutes", type=int, default=15)
    logs.set_defaults(func=cmd_collect_logs)
    export = sub.add_parser("export-results", help="export all results as CSV")
    export.add_argument("--out")
    export.set_defaults(func=cmd_export)
    sub.add_parser("exporter", help="run the Prometheus exporter in the foreground").set_defaults(
        func=cmd_exporter)
    sub.add_parser("mcp", help="serve the lab as MCP tools over stdio").set_defaults(func=cmd_mcp)
    agent = sub.add_parser("agent", help="run an LLM agent (Mistral by default) against the lab")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    inject = agent_sub.add_parser("inject", help="the Injector runs one experiment, asking before the real fault")
    inject.add_argument("experiment", help="name of a stored experiment definition")
    inject.add_argument("--task", help="free-text task instead of 'run this stored experiment'")
    inject.add_argument("--approve", action="store_true",
                        help="approve the real fault in advance instead of asking at the terminal")
    inject.set_defaults(func=cmd_agent)
    analyze = agent_sub.add_parser("analyze", help="the Recovery Analyst judges one finished experiment")
    analyze.add_argument("experiment_id")
    analyze.set_defaults(func=cmd_agent)
    plan = sub.add_parser("plan", help="Planner agent proposals; nothing runs until a person approves")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    propose = plan_sub.add_parser("propose", help="the Planner proposes follow-up experiments for an analysis")
    propose.add_argument("analysis_id")
    plan_list = plan_sub.add_parser("list", help="list proposals")
    plan_list.add_argument("--status", choices=["proposed", "approved", "rejected", "ran"])
    plan_show = plan_sub.add_parser("show", help="show one proposal")
    plan_show.add_argument("proposal_id")
    plan_approve = plan_sub.add_parser("approve", help="approve a proposal: the Injector runs it")
    plan_approve.add_argument("proposal_id")
    plan_approve.add_argument("--direct", action="store_true", help="run with the controller instead of the Injector")
    plan_reject = plan_sub.add_parser("reject", help="reject a proposal")
    plan_reject.add_argument("proposal_id")
    plan_reject.add_argument("--reason", default="")
    for parser_ in (propose, plan_list, plan_show, plan_approve, plan_reject):
        parser_.set_defaults(func=cmd_plan)
    sub.add_parser("gateway", help="LAN gateway in front of the loopback services (scripts/deploy-lan.sh)").set_defaults(
        func=cmd_deploy)
    sub.add_parser("control-api", help="token-protected HTTP control API on loopback").set_defaults(func=cmd_deploy)
    sub.add_parser("deploy-secure-plant", help="replace the MES's public lab passwords with deployment secrets"
                   ).set_defaults(func=cmd_deploy)
    analysis = sub.add_parser("analysis", help="read-only failure impact analysis of a finished failure")
    analysis_sub = analysis.add_subparsers(dest="analysis_command", required=True)
    base = analysis_sub.add_parser("baseline", help="assess a baseline and save the assessment")
    base.add_argument("source", nargs="?", help="run id, chaos-opcua report path or measurement artifact path")
    base.add_argument("--observe", type=float, metavar="SECONDS",
                      help="instead, observe the running plant read-only for up to 120 s")
    base.set_defaults(func=cmd_analysis)
    analyze_cmd = analysis_sub.add_parser("analyze", help="analyse an externally performed failure")
    analyze_cmd.add_argument("experiment", help="run id, chaos-opcua report path or measurement artifact path")
    analyze_cmd.add_argument("--baseline", default="own",
                             help="'own' (the run's own pre-fault window, default), a baseline assessment id, or a "
                                  "run id / path")
    analyze_cmd.add_argument("--fault-meta", help="JSON with fault type/target/parameters/timing when not recorded")
    analyze_cmd.add_argument("--no-narrative", action="store_true", help="deterministic analysis only, no LLM")
    analyze_cmd.set_defaults(func=cmd_analysis)
    rec_cmd = analysis_sub.add_parser("record", help="record the Docker stack read-only while an external tool "
                                                      "injects a fault; detects the fault window")
    rec_cmd.add_argument("--seconds", type=float, default=120, help="recording length (10..600)")
    rec_cmd.add_argument("--label", default="external")
    rec_cmd.set_defaults(func=cmd_analysis)
    rec = analysis_sub.add_parser("recommend", help="the read-only Recommender agent: fixes with verified locations")
    rec.add_argument("analysis_id")
    rec.set_defaults(func=cmd_analysis)
    show_analysis = analysis_sub.add_parser("show", help="print a stored analysis report")
    show_analysis.add_argument("analysis_id", nargs="?", default="latest")
    show_analysis.set_defaults(func=cmd_analysis)
    workload = sub.add_parser("workload", help="run a bounded workload against the lab plant")
    workload.add_argument("--profile", default="baseline")
    workload.add_argument("--seconds", type=int, default=30)
    workload.set_defaults(func=cmd_workload)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, PlantError, model.DefinitionError) as exc:
        _warn(f"error: {exc}")
        return EXIT_FAILED
    except FileNotFoundError as exc:
        _warn(f"error: {exc}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        _warn("interrupted")
        return 130
