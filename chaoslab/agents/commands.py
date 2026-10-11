"""`chaoslab agent inject` and `chaoslab agent analyze`: one agent, one task, a transcript on disk."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from chaoslab import settings as settings_module
from chaoslab.agents.llm import ChatClient, LLMConfig
from chaoslab.agents.roles import ANALYST, INJECTOR
from chaoslab.agents.runtime import Agent, AgentRun, Approver, ToolBox, save_transcript, stderr_progress
from chaoslab.mcp_server import build_server
from chaoslab.store import ResultStore

EXIT_OK, EXIT_FAILED, EXIT_REJECTED, EXIT_FINDINGS = 0, 1, 2, 3
REFUSED_OUTCOMES = {"rejected", "denied", "plant_not_ready", "preflight_failed"}


def _agent(root: Path | None, role, approver: Approver, llm: ChatClient | None) -> Agent:
    server = build_server(root)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return Agent(role, llm or ChatClient(LLMConfig.from_env()), ToolBox(server, role.tools), approver,
                 progress=stderr_progress)


def inject(root: Path | None, experiment: str, approver: Approver, task: str | None = None,
           llm: ChatClient | None = None) -> tuple[int, dict]:
    task = task or f"Run the stored experiment named {experiment!r}."
    run = asyncio.run(_agent(root, INJECTOR, approver, llm).run(task))
    if run.output is not None:
        # What the agent did, from its own tool calls, not what it says it did.
        run.output["emergency_stop_called"] = any(c.get("tool") == "emergency_stop" for c in run.transcript)
    payload = _finish(root, run)
    output = run.output or {}
    if output.get("outcome") == "finished":
        result = output.get("overall_result")
        code = EXIT_OK if result == "recovered" else EXIT_FINDINGS if result == "recovered_with_data_findings" \
            else EXIT_FAILED
    elif output.get("outcome") in REFUSED_OUTCOMES:
        code = EXIT_REJECTED
    else:
        code = EXIT_FAILED
    return code, payload


def analyze(root: Path | None, experiment_id: str, llm: ChatClient | None = None) -> tuple[int, dict]:
    settings = settings_module.load(root)
    store = ResultStore(settings.results_dir)
    resolved = store.resolve(experiment_id)
    run = asyncio.run(_agent(root, ANALYST, lambda *_: False, llm).run(
        f"Analyse the finished experiment {resolved!r}."))
    if run.output is not None and run.output.get("baseline_compared"):
        compared = [c["arguments"] for c in run.transcript if c.get("tool") == "compare_runs"]
        if not any(run.output["baseline_compared"] in (a.get("first"), a.get("second")) for a in compared):
            # A model can name a baseline it never compared against; the record says so rather than repeat it.
            run.output["baseline_compared_unverified"] = run.output.pop("baseline_compared")
            run.output["baseline_compared"] = None
    payload = _finish(root, run)
    if run.output is not None:
        # Next to the lab's own record, never over it.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = store.directory(resolved) / f"agent-analysis-{stamp}.json"
        path.write_text(json.dumps(run.output, indent=2, default=str), encoding="utf-8")
        payload["analysis_file"] = str(path)
    return (EXIT_OK if run.output is not None else EXIT_FAILED), payload


def _finish(root: Path | None, run: AgentRun) -> dict:
    settings = settings_module.load(root)
    transcript = save_transcript(settings.state_dir / "agents", run)
    return {"role": run.role, "stopped": run.stopped, "rounds": run.rounds, "output": run.output,
            "final_text": None if run.output is not None else run.final_text, "transcript": str(transcript)}


def plan(root: Path | None, analysis_id: str, llm: ChatClient | None = None) -> tuple[int, dict]:
    """The Planner proposes follow-up experiments for one analysis. Nothing runs."""
    from chaoslab import planning
    from chaoslab.agents.restricted import PlannerToolBox
    from chaoslab.agents.roles import PLANNER
    from chaoslab.analysis import engine

    settings = settings_module.load(root)
    analysis = engine.read(settings, analysis_id)
    before = {p["proposal_id"] for p in planning.list_proposals(settings)}
    server = build_server(root)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    agent = Agent(PLANNER, llm or ChatClient(LLMConfig.from_env()), PlannerToolBox(server, PLANNER.tools),
                  lambda *_: False, progress=stderr_progress)
    run = asyncio.run(agent.run(f"Propose follow-up experiments for analysis {analysis['analysis_id']!r}."))
    created = [p for p in planning.list_proposals(settings) if p["proposal_id"] not in before]
    if run.output is not None:
        claimed = {p.get("proposal_id") for p in run.output.get("proposals") or []}
        real = {p["proposal_id"] for p in created}
        # What the agent saved, from the store, not what it says it saved.
        run.output["unverified_proposal_ids"] = sorted(claimed - real)
        run.output["saved_proposals"] = sorted(real)
    payload = _finish(root, run)
    payload["proposals"] = created
    return (EXIT_OK if run.output is not None else EXIT_FAILED), payload


def approve(root: Path | None, proposal_id: str, via_agent: bool = True, llm: ChatClient | None = None) -> tuple[int, dict]:
    """A person approved a proposal: hand it to the Injector (or run it directly) and record the outcome."""
    from chaoslab import planning

    settings = settings_module.load(root)
    _, record = planning.load(settings, proposal_id)
    if record["status"] != "proposed":
        raise ValueError(f"proposal {proposal_id} is {record['status']}, not proposed")
    planning.set_status(settings, record["proposal_id"], "approved")
    definition = record["definition"]
    if via_agent:
        task = ("A person approved this proposed experiment. Run exactly this definition, unchanged (pass it as the "
                "definition argument): " + json.dumps(definition, separators=(",", ":")))
        code, payload = inject(root, definition.get("name", "proposal"), lambda *_: True, task=task, llm=llm)
        output = payload.get("output") or {}
        experiment_id, overall = output.get("experiment_id"), output.get("overall_result")
    else:
        from chaoslab.api import LabApi

        result = LabApi(root).run(definition)
        experiment_id, overall = result.get("experiment_id"), result.get("overall_result")
        code = EXIT_OK if overall == "recovered" else EXIT_FAILED
        payload = {"result": {k: result.get(k) for k in ("experiment_id", "overall_result", "reasons")}}
    planning.set_status(settings, record["proposal_id"], "ran", experiment_id=experiment_id, overall_result=overall)
    payload["proposal_id"] = record["proposal_id"]
    return code, payload
