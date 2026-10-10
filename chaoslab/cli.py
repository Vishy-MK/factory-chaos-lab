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
        if status["floor"]:
            _say(f"floor last recorded a check {status['floor'].get('seconds_since')}s ago")
        for name, row in status["components"].items():
            _say(f"  {name:<10} {row['status']:<9} pid={row['pid']} rss={row['rss_mb']} MB")
        _say(f"plant RSS total {status['plant_rss_mb_total']} MB; host available "
             f"{status['host']['available_memory_mb']} MB, CPU {status['host']['cpu_percent']}%")
        _say(f"exporter pid: {status['exporter_pid']}")
    return EXIT_OK if status["api_up"] else EXIT_FAILED


def cmd_health(args) -> int:
    life = _lifecycle(args.root)
    status = life.status()
    machines = status["machines"] or {}
    healthy = bool(status["api_up"] and machines.get("machines")
                   and machines.get("connected") == machines.get("machines")
                   and all(c["alive"] for c in status["components"].values()))
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
    gate = resources.ResourceGate(settings.policy["resources"])
    allowed, reasons, snapshot = gate.check()
    payload = {"experiments_allowed": allowed, "reasons": reasons, "host": snapshot.as_dict(),
               "threshold_available_mb": settings.policy["resources"]["min_available_memory_mb"]}
    life = _lifecycle(args.root)
    payload["plant_rss_mb_total"] = life.status()["plant_rss_mb_total"]
    _emit(payload, args.json, (
        f"available memory {snapshot.available_mb:.0f} MB (threshold "
        f"{payload['threshold_available_mb']} MB), CPU {snapshot.cpu_percent:.0f}%, "
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
    from chaoslab.observability.exporter import serve

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
