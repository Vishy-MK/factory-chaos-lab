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
