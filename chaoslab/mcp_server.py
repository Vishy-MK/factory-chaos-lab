"""The lab's AI contract (`LabApi`) and plant lifecycle, exposed as MCP tools.

The tools are thin: argument handling and one call into `LabApi`, `Lifecycle` or the resource gate. Nothing
here decides whether an experiment is safe. `PolicyValidator`, the lease, the watchdog and cleanup do that
exactly as they do for the CLI, so an agent can propose anything and still only run what the policy allows.

`run_experiment` returns as soon as the experiment is accepted; `wait_for_experiment` blocks until it finishes, so
an agent does not spend a model turn on every poll.
"""

from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from chaoslab import model, resources, settings as settings_module
from chaoslab.adapters.factorysemantics import PlantError
from chaoslab.api import CONTRACT_VERSION, LabApi
from chaoslab.lifecycle import Lifecycle, is_healthy
from chaoslab.settings import ConfigError

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
MAX_WAIT_S = 900
POLL_S = 2.0

WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

# Refusals the caller can act on. Their text reaches the agent; anything else is a crash and stays on the server.
EXPECTED = (FileNotFoundError, ValueError, model.DefinitionError, PlantError, ConfigError)

INSTRUCTIONS = (
    "Drive the factory chaos lab around FactorySemantics MES. Discover fault types and targets, propose an "
    "experiment definition, validate it against the safety policy, run it, poll its status, then read its "
    "metrics, logs, scoring and recovery evidence. The policy decides what may run: a rejected proposal "
    "comes back with reasons, never a partial run. Only one experiment runs at a time. Unavailable values "
    "are null, never zero. Infrastructure experiments need the plant running (plant_status, plant_start); "
    "application experiments build their own ephemeral plant. If anything looks wrong mid-run, call "
    "emergency_stop."
)


def _plain(value: Any) -> Any:
    """JSON-safe copy: results carry paths and timestamps that structured output cannot."""
    return json.loads(json.dumps(value, default=str))


def build_server(root: Path | None = None) -> MCPServer:
    api = LabApi(root)
    settings = settings_module.load(root)
    life = Lifecycle(settings)
    mcp = MCPServer(name="chaoslab", instructions=INSTRUCTIONS, version=CONTRACT_VERSION)

    def tool(annotations: ToolAnnotations) -> Callable:
        def register(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def call(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except EXPECTED as exc:
                    raise ToolError(str(exc)) from exc

            return mcp.tool(annotations=annotations, structured_output=True)(call)

        return register

    # ------------------------------------------------------------ discovery

    @tool(READ)
    def list_fault_types(name: str | None = None) -> dict[str, Any]:
        """Every fault type with its parameters and bounds; unsupported types say why. Pass name for one fault's
        full detail: expected effects, cleanup, recovery criteria and preconditions."""
        faults = api.discover_fault_types()
        if name is not None:
            match = [f for f in faults if f["name"] == name]
            if not match:
                raise ToolError(f"unknown fault type {name!r}; call list_fault_types without a name")
            return {"fault_types": _plain(match)}
        # Compact by default so the whole catalogue fits in one agent tool result.
        compact = [{"name": f["name"], "layer": f["layer"], "supported": f["supported"], "summary": f["summary"],
                    "unsupported_reason": f["unsupported_reason"], "target_keys": f["target_keys"],
                    "parameters": [{k: p[k] for k in ("name", "type", "minimum", "maximum", "default", "required",
                                                      "choices") if p[k] not in (None, False)}
                                   for p in f["parameters"]]}
                   for f in faults]
        return {"fault_types": _plain(compact),
                "note": "call list_fault_types with name for one fault's effects, cleanup and recovery criteria"}

    @tool(READ)
    def list_targets() -> dict[str, Any]:
        """The allowlist: hosts, plants and their components, stations, fault targets, workload profiles."""
        return _plain(api.discover_targets())

    @tool(READ)
    def get_safety_policy() -> dict[str, Any]:
        """The safety policy every proposal is validated against."""
        return _plain(api.safety_policy())

    @tool(READ)
    def list_experiments() -> dict[str, Any]:
        """The stored experiment definitions, with category and fault types, and problems for invalid ones."""
        return {"experiments": _plain(api.list_experiments())}

    @tool(READ)
    def get_experiment(name: str) -> dict[str, Any]:
        """One stored definition by name, as the mapping run_experiment and validate_experiment accept."""
        return _plain(api.get_experiment(name))

    # ------------------------------------------------------------ proposing and running

    @tool(READ)
    def validate_experiment(definition: dict) -> dict[str, Any]:
        """Check a proposed definition against the schema and the safety policy without running anything.

        Returns allowed, the stage that decided (schema or policy) and the reasons.
        """
        return _plain(api.validate(definition))

    @tool(WRITE)
    def run_experiment(definition: dict | None = None, name: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Run an experiment: pass a definition mapping, or the name of a stored one.

        Without dry_run this injects a real fault into the lab plant. It validates first and refuses while
        another experiment is active; when accepted it returns an experiment_id at once - poll
        experiment_status. With dry_run it validates and runs preflight only, and blocks until that is done.
        """
        if (definition is None) == (name is None):
            raise ValueError("pass exactly one of definition or name")
        if definition is None:
            definition = api.get_experiment(name)
        if dry_run:
            return _plain(api.run(definition, dry_run=True))
        return _plain(api.submit(definition))

    @tool(READ)
    def experiment_status(experiment_id: str | None = None) -> dict[str, Any]:
        """State of one experiment, or with no id: the active experiment and the latest result."""
        return _plain(api.status(experiment_id))

    @tool(READ)
    def wait_for_experiment(experiment_id: str, timeout_s: int = 300) -> dict[str, Any]:
        """Block until an experiment has finished, then return its status. Gives up after timeout_s (at most
        900) and says so; the experiment keeps running either way."""
        deadline = time.monotonic() + min(max(timeout_s, 1), MAX_WAIT_S)
        started = time.monotonic()
        while True:
            status = api.status(experiment_id)
            if status.get("finished") or time.monotonic() >= deadline:
                break
            time.sleep(POLL_S)
        return _plain({**status, "timed_out": not status.get("finished"),
                       "waited_s": round(time.monotonic() - started, 1)})

    @tool(WRITE)
    def emergency_stop() -> dict[str, Any]:
        """Stop the active experiment and clean up: restart killed components, resume frozen ones, stop
        CPU burners, release the database lock, sweep stray helpers. Safe to call when nothing is running."""
        return _plain(api.emergency_stop())

    # ------------------------------------------------------------ results

    @tool(READ)
    def list_runs(limit: int = 20) -> dict[str, Any]:
        """Recorded results, newest first, with category and overall result."""
        rows = []
        for run_id in api.store.list_runs()[:limit]:
            result = api.store.read_result(run_id)
            rows.append({"experiment_id": run_id, "overall_result": result.get("overall_result"),
                         "category": (result.get("definition") or {}).get("category")})
        return {"runs": rows}

    @tool(READ)
    def get_result(experiment_id: str) -> dict[str, Any]:
        """The full result record of one run. An id fragment is enough if it is unambiguous."""
        return _plain(api.result(experiment_id))

    @tool(READ)
    def get_metrics(experiment_id: str, include_samples: bool = False, limit: int = 500) -> dict[str, Any]:
        """Observed metrics and production measurements, optionally with per-second samples."""
        return _plain(api.metrics(experiment_id, include_samples=include_samples, limit=limit))

    @tool(READ)
    def get_logs(experiment_id: str, max_lines_per_file: int = 100) -> dict[str, Any]:
        """Log excerpts collected for the experiment window, and the lab's audit trail for it."""
        return _plain(api.logs(experiment_id, max_lines_per_file=max_lines_per_file))

    @tool(READ)
    def get_scoring(experiment_id: str) -> dict[str, Any]:
        """The upstream harness score of an application experiment (null for infrastructure runs)."""
        return _plain(api.scoring(experiment_id))

    @tool(READ)
    def get_analysis(analysis_id: str, section: str = "summary") -> dict[str, Any]:
        """A stored failure impact analysis (results/analysis/). section: summary, baseline, comparisons,
        recovery, findings, recommendations, score, coverage or refs. Read only."""
        from chaoslab.analysis import engine as analysis_engine
        from chaoslab.analysis.narrative import analysis_section
        from chaoslab.analysis.evidence import EvidenceError

        try:
            return _plain(analysis_section(analysis_engine.read(settings, analysis_id), section))
        except EvidenceError as exc:
            raise ToolError(str(exc)) from exc

    @tool(READ)
    def get_recovery(experiment_id: str) -> dict[str, Any]:
        """Recovery evidence, cleanup outcome, final health and overall result of one run."""
        return _plain(api.recovery(experiment_id))

    @tool(READ)
    def compare_runs(first: str, second: str) -> dict[str, Any]:
        """Metric-by-metric comparison of two runs, with deltas."""
        return _plain(api.compare(first, second))

    # ------------------------------------------------------------ the plant

    @tool(READ)
    def plant_status() -> dict[str, Any]:
        """The lab plant: healthy, API up and latency, machines connected, each component, host memory/CPU."""
        status = life.status()
        return _plain({"healthy": is_healthy(status), **status})

    @tool(READ)
    def check_resources() -> dict[str, Any]:
        """The memory and CPU gate. Experiments and plant start are refused while it says no."""
        return _plain(resources.gate_report(settings.policy))

    @tool(WRITE)
    def plant_start(with_exporter: bool = False) -> dict[str, Any]:
        """Start the persistent lab plant and wait until it is healthy. Refused when memory is low."""
        return _plain(life.start(with_exporter=with_exporter))

    @tool(WRITE)
    def plant_stop() -> dict[str, Any]:
        """Stop the lab plant and the exporter. Interrupts anything that depends on the plant."""
        return _plain(life.stop())

    return mcp


def main(root: Path | None = None) -> None:
    build_server(root).run("stdio")
