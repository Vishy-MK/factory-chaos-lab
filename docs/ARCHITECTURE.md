# Architecture

Four parts, kept apart on purpose.

## A. FactorySemantics MES (the target, unmodified)

`factorysemantics-mes/` is an unmodified clone at a pinned commit, installed into `.venv` with
`pip install -e`. The lab never edits it. Its configuration is done through environment variables and a **copy**
of the bottling pack (`runtime/fleet/packs/bottling`, copied by `chaoslab prepare`, with replay data regenerated
there), so no generated file is written inside the upstream tree.

The persistent lab plant is started with the application's own supervisor, `fsmes plant bottling start`:

```
runtime/fleet/fleet.toml  ->  packs/bottling   (copy of the upstream pack, own replay data)
runtime/data/bottling.db      SQLite
processes:  fsmes run-opc-sim --replay   OPC UA server replaying the recording on :4841
            fsmes run-opc-agent          subscribes, books production
            fsmes run-api                REST API and /metrics on 127.0.0.1:8010
            fsmes run-operations         simulated shop floor (inspections, issues, stop labels)
            fsmes run-log-sink           console log with a size ceiling
```

## B. The laboratory and fault controller (this repository, `chaoslab/`)

```
chaoslab/
  settings.py        loads config/*.yaml; paths and derived values
  model.py           experiment definition (schema), state machine
  policy.py          deterministic policy validator (allowlist, bounds, categories)
  resources.py       host snapshot and the memory/CPU gate
  adapters/
    factorysemantics.py   the only module that knows the application's HTTP surface and process layout
    metrics.py            one sample format; unavailable is None; phase summaries; samplers
    scoring.py            plan generation and parsing for `fsmes lab run`
    logs.py               time-window log extraction with redaction
  chaos/
    faults.py        fault registry: parameters, bounds, preconditions, inject, cleanup, removal check
    holders.py       self-limiting helper processes (CPU burner, SQLite lock holder, cgroup limit reset)
    cgroups.py       lab-owned user-systemd scopes: create, write and lift cgroup v2 limits, read counters
    controller.py    lifecycle orchestration and result assembly
    recovery.py      staged recovery verification
    watchdog.py      independent cleanup process
    cleanup.py       cleanup from lease records (shared by controller, watchdog and emergency stop)
  plant_names/       loaded into plant processes via PYTHONPATH: sitecustomize (process names) and
                     chaoslab_dbhook (dormant SQLAlchemy latency hook in the database clients)
  workload/          bounded read-only request generator and profiles
  audit.py           audit log and the lease
  store.py           results, CSV export, comparison
  lifecycle.py       prepare / start / stop / status of the plant and exporter
  api.py             the contract for a future caller (see below)
  cli.py             `python -m chaoslab ...`
  report.py          parameters, outcome and per-phase metrics table of one result (`chaoslab report`)
  analysis/          read-only failure impact analysis: evidence, baseline, comparisons, findings, fixes, score,
                     read-only narrative agent (docs/FAILURE_ANALYSIS.md); writes only results/analysis/
```

Control flow of an infrastructure experiment:

```
validate (schema, policy) -> resource gate -> lease + watchdog -> preflight (health, preconditions)
 -> fault preparation (e.g. move a component into its cgroup scope) -> baseline (sampler, workload)
 -> intent record -> inject -> hold -> cleanup + removal check
 -> recovery verification (stable for 3 samples) -> post window -> result.json
```

`database_latency` injects by writing one activation file that the hook inside the api and opc-agent reads; the
hook expires the delay at its own monotonic deadline. Details: `docs/DATABASE_LATENCY.md`.

An application experiment replaces inject/hold with one call to `fsmes lab run` on a generated plan and derives its
evidence from the harness's `scores.json` and per-look station states.

Dependency direction: `cli` and `api` -> `controller` -> `faults`, `adapters`, `policy`. Adapters depend on the
application; nothing else does. The controller depends on no AI component, Grafana or Prometheus.

## C. Observability

| Piece | Where | Notes |
|---|---|---|
| Exporter | `chaoslab/observability/exporter.py`, `127.0.0.1:9108` | reads the same adapter; `chaoslab_*` series |
| Application metrics | `127.0.0.1:8010/metrics` | scraped directly, unmodified |
| Prometheus | `runtime/monitoring/prometheus-*` | 5 s / 10 s scrape, 3 days or 512 MB retention, loopback |
| Grafana | optional, `runtime/monitoring/grafana-*` | dashboards provisioned from `observability/grafana/dashboards` |
| Logs | `runtime/work/logs/bottling/*.jsonl`, `runtime/logs/` | excerpts copied into each result |
| Results | `results/<id>/result.json`, `samples.jsonl`, `logs/` | the primary record |

The controller works with none of these running.

## D. Future AI resilience engine (not part of this repository)

Nothing in this repository is an AI component. What exists for it is `chaoslab/api.py` (`LabApi`), a deterministic
local Python interface (and the equivalent CLI with `--json`):

| Need | Call |
|---|---|
| discover fault types | `discover_fault_types()` |
| discover valid targets | `discover_targets()` |
| retrieve definitions | `list_experiments()`, `get_experiment(name)` |
| check a proposal | `validate(definition)` |
| submit | `submit(definition)` (validates, then runs detached) or `run(definition)` (blocking) |
| status | `status(id)` |
| metrics and logs | `metrics(id, include_samples)`, `logs(id)` |
| scoring | `scoring(id)` |
| recovery verification | `recovery(id)` |
| compare | `compare(a, b)` |
| stop | `emergency_stop()` |

A proposing system supplies a definition; `PolicyValidator` decides. There is no HTTP server, no shell and no
Docker access behind this interface.
