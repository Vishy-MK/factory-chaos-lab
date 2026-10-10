# Factory chaos lab

A local, bounded chaos-engineering laboratory around FactorySemantics MES, built for the hackathon project
*AI-Driven Chaos Engineering for Industrial Resilience*. It runs the target application, injects controlled faults,
measures the consequences, verifies recovery from application evidence, and records every run as JSON.

There is **no AI engine here**. The lab is the deterministic target environment and fault controller a future AI
system would call (see `docs/ARCHITECTURE.md`, part D).

## What it does

1. Runs FactorySemantics MES (upstream, unmodified) as a persistent simulated bottling plant on SQLite, without Docker.
2. Plays **simulated manufacturing faults** (breakdown, changeover, counter reset, micro-stops, scrap burst, starve,
   block, OPC disconnect) through the application's own simulator and scoring harness (`fsmes lab run`).
3. Injects **real infrastructure faults** into the live plant: killing and restarting the API, OPC UA server, OPC agent
   or floor simulator; freezing a component; holding the database write lock; CPU contention on the API.
4. Checks a safety policy before, during and after every experiment, cleans up, and refuses to claim recovery it
   cannot show.
5. Exports lab metrics for Prometheus, ships Grafana dashboards, and collects log excerpts per experiment.

The two fault categories test different things and are never mixed up: a simulated fault changes *what the plant
reports about the line*; an infrastructure fault changes *whether the software works*. Every result states which
layer was exercised.

## Requirements

Ubuntu 24.04, Python 3.12+, git, `python3-venv`, about 1.5 GB of free RAM beyond the desktop (the plant uses about
0.6 GB; an application experiment adds about 0.6 GB for 2 minutes). Docker is **not** needed.
Measured numbers are in `docs/RESOURCE_ASSESSMENT.md`.

## Setup

```bash
./scripts/setup.sh
```

Creates `.venv`, installs the pinned lab requirements and the upstream package (editable, from the pinned commit in
`UPSTREAM_COMMIT`), copies the bottling pack into `runtime/fleet`, generates its replay data, initialises the plant
database, and runs the prerequisite check. It never overwrites `results/`.

## Start and stop

```bash
./scripts/start.sh                    # plant only
./scripts/start.sh --with-exporter    # plant and the lab exporter on 127.0.0.1:9108
./scripts/status.sh
./scripts/health-check.sh             # exit 0 only if fully healthy
./scripts/stop.sh
```

Start refuses to run when available memory is below `resources.min_available_memory_mb` (1500 MB).

## Running experiments

```bash
python -m chaoslab experiments         # definitions
python -m chaoslab faults              # fault types, parameters and bounds; unsupported ones state why
python -m chaoslab targets             # the allowlist
python -m chaoslab validate experiments/definitions/api-process-outage-10s.yaml

./scripts/run-experiment.sh experiments/definitions/baseline-observe.yaml
./scripts/run-experiment.sh experiments/definitions/api-process-outage-10s.yaml
./scripts/run-experiment.sh experiments/definitions/sim-machine-breakdown-rd.yaml
```

Infrastructure experiments need the plant running (`scripts/start.sh`). Application experiments build their own
ephemeral plant and take about two minutes. Add `--dry-run` to validate and run preflight without executing.

| Experiment | Layer | What it does |
|---|---|---|
| `baseline-observe` | control | observe the healthy plant, no fault |
| `api-process-outage-10s` | infrastructure | SIGKILL the API for 10 s, restart it |
| `api-freeze-10s` | infrastructure | SIGSTOP the API for 10 s, resume |
| `opc-server-outage-10s` | infrastructure | kill the OPC UA server for 10 s, restart |
| `opc-agent-outage-10s` | infrastructure | kill the OPC agent for 10 s, restart |
| `database-write-lock-10s` | infrastructure | hold the SQLite write lock for 10 s, roll back |
| `api-cpu-pressure-15s` | infrastructure | contend for the API's CPU for 15 s |
| `compound-api-then-opc-outage` | infrastructure | two faults in sequence; refused unless `compound.enabled` |
| `baseline-manufacturing-workflow` | application | 25 line-minutes, no fault, scored |
| `sim-machine-breakdown-rd` | application | scripted 2-minute breakdown of the rotary denester |
| `sim-micro-stops-palletiser` | application | scripted micro-stops |
| `sim-counter-reset-rd` | application | scripted counter reset |
| `sim-changeover` | application | scripted whole-line changeover |
| `sim-opc-disconnect` | application | scripted loss of the OPC UA layer |

Details, parameters and what each experiment has actually shown are in `docs/EXPERIMENTS.md`.

## MCP tools for agents

```bash
.venv/bin/python -m chaoslab mcp       # stdio; .mcp.json registers it for Claude Code in this directory
```

The same contract as `chaoslab/api.py`, as 21 MCP tools. Only four change anything and are marked destructive:
`run_experiment`, `emergency_stop`, `plant_start`, `plant_stop`. The rest read.

| Group | Tools |
|---|---|
| discovery | `list_fault_types`, `list_targets`, `get_safety_policy`, `list_experiments`, `get_experiment` |
| proposing and running | `validate_experiment`, `run_experiment` (definition or stored name; `dry_run`), `experiment_status`, `wait_for_experiment`, `emergency_stop` |
| results | `list_runs`, `get_result`, `get_metrics`, `get_logs`, `get_scoring`, `get_recovery`, `compare_runs` |
| the plant | `plant_status`, `check_resources`, `plant_start`, `plant_stop` |

`run_experiment` goes through the same policy, lease and cleanup as the CLI and returns an `experiment_id` at once;
`wait_for_experiment` blocks until it finishes. A rejected proposal returns its reasons; an unknown run id or definition is a tool error
with its reason.

## Agents

Two LLM agents drive the lab through the MCP tools above, called in-process. Mistral is the default provider;
any OpenAI-compatible provider works the same way.

```bash
export MISTRAL_API_KEY=...
python -m chaoslab agent inject api-process-outage-10s   # asks y/N at the terminal before the real fault
python -m chaoslab agent analyze <experiment-id>         # verdict saved as results/<id>/agent-analysis-*.json
```

| Agent | Does | Tools |
|---|---|---|
| Injector | checks health and resources, validates, dry-runs, runs one experiment, waits, emergency-stops if it hangs | plant/resource status, discovery, `validate_experiment`, `run_experiment`, `experiment_status`, `wait_for_experiment`, `emergency_stop` |
| Recovery Analyst | judges recovery from recovery stages, metrics, logs, scoring and a baseline comparison; may disagree with the lab only with evidence | result tools, `list_runs`, `compare_runs`, `plant_status` (read only) |

Enforced in code, not left to the prompt: an agent is offered only its own tools and a call to any other is
refused; `run_experiment` without `dry_run` needs a person's approval (`--approve` gives it in advance; with no
terminal the answer is no); the safety policy still decides what runs. Transcripts are saved in
`runtime/state/agents/`. Exit codes of `inject` match `run`: 0 recovered, 3 recovered with data findings,
2 refused (rejected, denied, plant not ready, preflight failed), 1 anything else.

| Variable | Default | Purpose |
|---|---|---|
| `CHAOSLAB_LLM_PROVIDER` | `mistral` | `mistral` or `groq` |
| `MISTRAL_API_KEY` / `GROQ_API_KEY` | none | key for the chosen provider |
| `CHAOSLAB_LLM_MODEL` | `ministral-14b-latest` for Mistral (the free tier allows no larger model); required for Groq | model name |
| `CHAOSLAB_LLM_BASE_URL` | the provider's | another OpenAI-compatible endpoint |

## Watching it in htop

```bash
./scripts/watch.sh      # htop, tree view, only the plant and the lab
```

Every process the lab runs has a name, so a fault is visible at the OS level as it happens: a killed component
disappears and comes back with a new PID, a frozen one shows state `T`, the CPU burner sits at its percentage.

| Shown as | Process | Live state in the title |
|---|---|---|
| `bottling/api`, `/opcsim`, `/opcagt`, `/floor`, `/logs` | the MES components of the lab plant | no |
| `cl:experiment` | the experiment runner | `[baseline]`, `[fault_active step 1]`, `[recovering]`... |
| `cl:watchdog` | the watchdog guarding a run | which run |
| `cl:injector`, `cl:analyst` | an agent | round, tool and experiment; `WAITING FOR APPROVAL` |
| `cl:fault:cpu`, `cl:fault:dblock` | fault helpers | no |
| `cl:exporter`, `cl:mcp` | the exporter, the MCP server | no |

The MES is not modified: the lab starts the plant with `chaoslab/plant_names` on `PYTHONPATH`, whose
`sitecustomize.py` sets each component's process name. Plant components and fault helpers get only a process name
(`/proc/<pid>/comm`): the lab finds them by their command line and environment, which stay untouched. Full titles
are only for processes nothing looks up. Restart the plant once (`scripts/stop.sh`, `scripts/start.sh`) for its
components to pick up their names. `scripts/watch.sh` uses its own htop configuration
(`observability/htop/htoprc`) and leaves yours alone.

## Looking at results

```bash
python -m chaoslab last                # summary of the most recent result
python -m chaoslab runs
python -m chaoslab show <id-or-fragment>
python -m chaoslab compare <a> <b>
python -m chaoslab active              # experiment in flight, if any
python -m chaoslab resources           # memory gate and plant footprint
./scripts/export-results.sh            # results/summary.csv
./scripts/collect-logs.sh --minutes 30
```

A result directory `results/<id>/` holds `result.json` (the record), `samples.jsonl` (per-second samples, infra
runs), `logs/` (excerpts for the experiment window) and, for application runs, `harness/` (plan and the upstream
harness output). Results are never overwritten. Unavailable values are `null` / `unavailable`, never zero.

Exit codes of `run`: 0 recovered, 3 recovered with data findings, 2 rejected, 1 anything else.

## Monitoring (optional)

```bash
./scripts/install-monitoring.sh                 # Prometheus, checksum-verified, into runtime/monitoring
./scripts/install-monitoring.sh --with-grafana  # also Grafana
./scripts/start-monitoring.sh [--with-grafana]
./scripts/stop-monitoring.sh
```

Prometheus listens on `127.0.0.1:9090` and scrapes the lab exporter (`:9108`, `chaoslab_*`) and the application's own
`/metrics` (`:8010`). Retention is 3 days or 512 MB. Dashboards are provisioned from files in
`observability/grafana/dashboards` (regenerate with `observability/grafana/build_dashboards.py`). Start refuses when
memory is low. The controller does not depend on any of this.

## Emergency stop

```bash
./scripts/emergency-stop.sh
```

Stops the active experiment, runs its cleanup (restart a killed component, resume a frozen one, stop the CPU burner,
release the lock), and sweeps for stray helper processes. See `docs/SAFETY.md`.

## Tests

```bash
.venv/bin/python -m pytest tests
```

The suite is unit and component level (policy, state machine, cleanup failure reporting, emergency stop, store,
metrics, scoring parser, recovery logic, workload, watchdog, exporter, dashboards) using a stub plant and real helper
processes. The integration runs against the real plant are the experiments themselves; their results are in
`results/` and summarised in `docs/EXPERIMENTS.md`.

## Resetting

Stop the plant, delete `runtime/`, run `scripts/setup.sh`. `results/` and `factorysemantics-mes/` are untouched.

## Licensing

| Component | Licence |
|---|---|
| FactorySemantics MES (`factorysemantics-mes/`) | Apache-2.0, `LICENSE` and `NOTICE` kept |
| This lab (`chaoslab/`, scripts, configs, docs) | **no licence chosen yet**; add one before publishing |
| Python dependencies | mixed; see `docs/LICENSES.md`. Notably `asyncua` is LGPL-3.0-or-later |
| Prometheus, Grafana (optional, downloaded) | Apache-2.0 and AGPL-3.0 respectively; not redistributed here |

## Known limitations

- Single host, process-level faults only. No network partition, packet loss, container or PostgreSQL faults.
- Database *outage* and database/API *latency* faults are not supported in lightweight mode and are listed as
  unsupported with reasons (`python -m chaoslab faults`).
- Application experiments score a replay at a compressed clock inside an ephemeral plant; they say nothing about
  infrastructure resilience.
- The application withholds OEE availability when its observation coverage is below the pack's floor, so many
  availability figures are `null` in short runs. That is reported, not filled in.
- The lab's own CPU/RSS figures depend on a recorded line replayed from memory: restarting the OPC server restarts
  the replay from the start of the recording.
- Not implemented: HTTP interface for the AI contract (MCP over stdio exists), Loki, alerting, PostgreSQL deployment, multi-plant faults.
