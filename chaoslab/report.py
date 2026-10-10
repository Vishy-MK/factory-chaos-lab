"""Human-readable report of one result: parameters, outcome and a per-phase metrics table.

Used by `chaoslab report` and the scripts/inject-*.sh wrappers. Values the lab could not measure print as n/a,
never as zero.
"""

from __future__ import annotations

from typing import Any, Callable

PHASES = ("baseline", "fault", "recovery", "post")


def _show(value: Any, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        value = f"{value:.3f}".rstrip("0").rstrip(".") if abs(value) < 1000 else f"{value:.0f}"
    return f"{value}{unit}"


def _dig(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _table(title: str, columns: list[tuple[str, dict | None]],
           rows: list[tuple[str, Callable[[dict], Any], str]]) -> list[str]:
    header = [title] + [name for name, _ in columns]
    body = [[label] + [_show(get(data), unit) if data else "n/a" for _, data in columns] for label, get, unit in rows]
    widths = [max(len(r[i]) for r in [header] + body) for i in range(len(header))]
    line = lambda r: "  ".join(c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(r))  # noqa
    return [line(header), line(["-" * w for w in widths])] + [line(r) for r in body]


def _downstream_rows() -> list[tuple[str, Callable[[dict], Any], str]]:
    return [
        ("samples", lambda d: d.get("samples"), ""),
        ("API success ratio", lambda d: d.get("api_success_ratio"), ""),
        ("authenticated success ratio", lambda d: d.get("authenticated_success_ratio"), ""),
        ("API latency p50", lambda d: _dig(d, "api_latency_ms", "p50"), " ms"),
        ("API latency p95", lambda d: _dig(d, "api_latency_ms", "p95"), " ms"),
        ("tag ingestion", lambda d: d.get("tag_ingest_per_second"), " /s"),
        ("longest ingestion stall", lambda d: d.get("tag_ingest_stall_s_max"), " s"),
        ("machines connected (min)", lambda d: d.get("machines_connected_min"), ""),
    ]


def _parameters(result: dict) -> list[str]:
    definition = result.get("definition") or {}
    lines = ["parameters"]
    for index, step in enumerate(definition.get("steps") or [], 1):
        fault = step.get("fault") or {}
        normalised = (result.get("fault_definition") or [{}] * index)[index - 1] if result.get("fault_definition") \
            else {}
        params = _dig(normalised, "fault", "parameters") or fault.get("parameters") or {}
        target = ", ".join(f"{k}={v}" for k, v in (step.get("target") or {}).items())
        lines.append(f"  fault            {fault.get('type')}  ({target})")
        for name, value in params.items():
            lines.append(f"  {name:<16} {_show(value)}")
    lines.append(f"  baseline_s       {_show(definition.get('baseline_s'))}")
    lines.append(f"  workload         {_show(definition.get('workload'))}")
    for prepared in result.get("preparation") or []:
        lines.append(f"  preparation      {'ok' if prepared.get('ok') else 'FAILED'}: {prepared.get('detail')}")
    limit = result.get("resource_limit")
    if limit:
        lines.append(f"  cgroup scope     {limit.get('unit')}")
        lines.append(f"  applied          {limit.get('applied')}")
    return lines


def _outcome(result: dict) -> list[str]:
    recovery = result.get("recovery") or {}
    lines = ["outcome",
             f"  result           {result.get('overall_result')}",
             f"  injected         {_show(_dig(result, 'fault_injection', 'injected'))}",
             f"  cleanup ok       {_show(_dig(result, 'cleanup', 'ok'))}",
             f"  recovered        {_show(recovery.get('recovered'))}",
             f"  time to recover  {_show(recovery.get('duration_s'), ' s')}"]
    for name, stage in (recovery.get("stages") or {}).items():
        lines.append(f"  {'ok    ' if stage.get('ok') else 'NOT OK'} {name}: {stage.get('detail')}")
    for reason in result.get("reasons") or []:
        lines.append(f"  reason: {reason}")
    return lines


def _database_section(evidence: dict) -> list[str]:
    direct = evidence.get("direct_database") or {}
    columns = [("baseline", direct.get("baseline")), ("fault", direct.get("fault")),
               ("fault window", direct.get("fault_window")), ("post", direct.get("post"))]
    rows = [
        ("statements", lambda d: d.get("statements"), ""),
        ("statements per second", lambda d: d.get("statements_per_second"), ""),
        ("mean statement time", lambda d: d.get("statement_ms_mean"), " ms"),
        ("mean driver time", lambda d: d.get("driver_ms_mean"), " ms"),
        ("statement time p95 (bound)", lambda d: _dig(d, "statement_ms", "p95_upper_bound"), " ms"),
        ("delayed statements", lambda d: d.get("injected_statements"), ""),
        ("injected delay per statement", lambda d: d.get("injected_delay_ms_mean"), " ms"),
        ("delayed statement time", lambda d: d.get("delayed_statement_ms_mean"), " ms"),
        ("errors", lambda d: d.get("errors"), ""),
        ("database is locked errors", lambda d: d.get("lock_errors"), ""),
    ]
    lines = ["", "direct database measurements (lab hook in api and opc-agent)"]
    lines += _table("metric", columns, rows)
    lines += ["", f"configured delay      {_show(evidence.get('configured_delay_ms'), ' ms')}",
              f"measured added delay  {_show(evidence.get('measured_added_statement_ms_mean'), ' ms')} per delayed "
              f"statement (includes waiting for the write lock behind other delayed transactions)"]
    return lines


def _cgroup_section(evidence: dict) -> list[str]:
    scoped = evidence.get("cgroup") or {}
    columns = [("baseline", scoped.get("baseline")), ("fault", scoped.get("fault")),
               ("limit window", scoped.get("limit_window")), ("post", scoped.get("post"))]
    rows = [
        ("samples", lambda d: d.get("samples"), ""),
        ("CPU used (of one CPU)", lambda d: d.get("cpu_percent_mean"), " %"),
        ("throttled periods", lambda d: d.get("cpu_throttled_periods"), ""),
        ("throttled ratio", lambda d: d.get("cpu_throttled_ratio"), ""),
        ("CPU pressure stall", lambda d: d.get("cpu_stall_ms"), " ms"),
        ("memory (max)", lambda d: d.get("memory_current_mb_max"), " MB"),
        ("memory (mean)", lambda d: d.get("memory_current_mb_mean"), " MB"),
        ("memory.high events", lambda d: d.get("memory_high_events"), ""),
        ("memory.max events", lambda d: d.get("memory_max_events"), ""),
        ("OOM kills", lambda d: d.get("oom_kills"), ""),
        ("memory pressure stall", lambda d: d.get("memory_stall_ms"), " ms"),
    ]
    lines = ["", f"cgroup measurements ({evidence.get('component')} in {evidence.get('unit')})"]
    lines += _table("metric", columns, rows)
    configured = evidence.get("configured") or {}
    lines += ["", "configured limits     " + ", ".join(f"{k}={_show(v)}" for k, v in configured.items())]
    return lines


def render(result: dict) -> str:
    definition = result.get("definition") or {}
    lines = [f"run      {result.get('experiment_id')}",
             f"name     {definition.get('name')}  ({definition.get('category')})", ""]
    lines += _parameters(result) + [""] + _outcome(result)
    if result.get("database_latency"):
        lines += _database_section(result["database_latency"])
    if result.get("resource_limit"):
        lines += _cgroup_section(result["resource_limit"])
    evidence = result.get("database_latency") or result.get("resource_limit")
    if evidence and evidence.get("downstream"):
        downstream = evidence["downstream"]
    else:
        downstream = {phase: (result.get("observed_metrics") or {}).get(phase) for phase in PHASES}
        downstream = {k: v for k, v in downstream.items() if v and v.get("samples")}
    if downstream and definition.get("category") == "infrastructure":
        lines += ["", "downstream effects (lab API probes and plant /metrics)"]
        lines += _table("metric", [(phase, downstream.get(phase)) for phase in PHASES], _downstream_rows())
    lines += ["", f"report   results/{result.get('experiment_id')}/result.json"]
    return "\n".join(lines)
