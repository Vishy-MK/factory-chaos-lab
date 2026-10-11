# Failure impact analysis (read only)

`chaoslab analysis` analyses a failure that something else performed. It assesses the pre-failure baseline,
compares the same metrics during and after the failure, assesses impact and recovery, ranks weaknesses by severity,
recommends fixes, and computes an explainable resilience score. It never injects, selects, schedules or approves a
fault, never cleans up, and never changes the plant, its configuration or an experiment record.

## Commands

```bash
# A. Baseline assessment (saved under results/analysis/baselines/)
python -m chaoslab analysis baseline <run-id>                 # e.g. a baseline-observe run
python -m chaoslab analysis baseline <report-or-artifact.json>
python -m chaoslab analysis baseline --observe 30             # bounded read-only observation of the running plant

# B. Post-failure analysis (saved under results/analysis/<analysis-id>/)
python -m chaoslab analysis analyze <run-id>                  # baseline = the run's own pre-fault window
python -m chaoslab analysis analyze <run-id> --baseline <baseline-assessment-id | run-id>
python -m chaoslab analysis analyze injections/chaos-opcua/results/<report>.json
python -m chaoslab analysis analyze <run-id> --fault-meta fault.json   # metadata the injecting system did not record
python -m chaoslab analysis analyze <run-id> --no-narrative            # deterministic only, no LLM
python -m chaoslab analysis show [<analysis-id> | latest]
```

The narrative step needs an LLM key (`MISTRAL_API_KEY`, or `CHAOSLAB_LLM_PROVIDER=groq` with `GROQ_API_KEY` and
`CHAOSLAB_LLM_MODEL`). Without one, the deterministic analysis is saved and the narrative is marked `incomplete`.

## Supported inputs

| Input | How it is recognised | What it provides |
|---|---|---|
| lab run | a run id or fragment under `results/` | `result.json` (fault record with timing, cleanup, recovery stages, integrity) and `samples.jsonl` (per-second samples) |
| chaos-opcua report | a `.json` with `schema_version: 1`, `timestamps`, `samples` | independent OPC probe, MES up/down and connectivity, telemetry age and ids, production counters, phase timestamps |
| measurement artifact | a `.json` with `"format": "chaoslab-measurements/v1"` | `measurement_method`, `identity`, optional `fault` (`type`, `target`, `parameters`, `timing.active_from/active_until`), optional `windows`, and `samples` using the normalised fields below |
| fault metadata | `--fault-meta` JSON: `type`, `target`, `parameters`, `timing` | fills only what the evidence does not record; marks attribution `uncertain` |

Normalised sample fields: `t` (epoch s), `label`, `api_up`, `api_latency_ms`, `api_error`, `auth_ok`,
`machines_total`, `machines_connected`, `tag_id`, `floor_age_s`, `telemetry_age_s`, `target_reachable`,
`production` (counter name to value), `database` and `cgroups` (lab hook and scope counters), `host_cpu_percent`,
`host_available_mb`, `components`. An absent field is unmeasured, never zero.

## Windows and attribution

- **baseline**: the run's own pre-fault samples (default), or another run's or artifact's baseline. A run without
  a fault is baseline throughout.
- **fault**: placed only from recorded timing. For lab runs that is the fault's own activation time
  (`activated_at` for database latency, `applied_at` for resource limits) or the controller's injection end plus
  `hold_s`. For chaos-opcua it is `fault_confirmed_utc .. restored_utc`. The controller's "fault" phase also spans
  cleanup and is not used.
- **recovery**: from the end of the fault window to the last sample. **post**: the controller's post-recovery
  samples, or, when the source has none, samples from the first stable healthy point onwards.
- **attribution**: `confirmed_by_measurement` (the fault's effect is measured at its target: delayed statements,
  cgroup throttling, the component not alive, the target unreachable), `recorded_by_controller` (an injection
  record without direct measurement), `uncertain` (no timing, or timing supplied by a person), `not_injected`.
  A filename or description is never taken as proof that a fault occurred.

## Metrics and comparison

Definitions with units are in every `analysis.json` under `metric_definitions` (from `chaoslab/analysis/measure.py`).
The rules:

- Percentiles are nearest-rank within one window (the value at rank `ceil(q x n)`), reported with the sample
  count, and never averaged across windows.
- Ratios (success, connectivity, staleness) are compared in percentage points; scalars as absolute change and
  relative percentage when the baseline is non-zero (zero baseline: relative change `null`).
- Counters (tag ids, production counters) are summed per segment between resets; a backward step is counted as a
  reset, not as negative throughput.
- A window with fewer than `measurement.min_samples_per_window` (5) samples, or that does not cover its span,
  qualifies every comparison made from it. A metric missing on either side is `unavailable`, not compared.
- Database percentiles from the lab hook are histogram-bucket upper bounds; database means are exact.
- Recovery time is measured from the end of the fault window to the first of 3 consecutive samples meeting every
  measured criterion: API up, authenticated operation ok, every machine connected, ingestion advancing over 3 s,
  floor checked within 45 s, telemetry fresher than 12 s. Unmeasured criteria are listed. The controller measures
  from the end of cleanup, so the two durations differ by the cleanup time.

## Severity rubric (`config/analysis.yaml` `severity`)

Points are summed per finding. Level: critical >= 9, high >= 6, medium >= 3, low >= 1, else info.

| Criterion | Points |
|---|---|
| availability loss (percentage points of API or authenticated success) | >= 20: 4, >= 5: 3, >= 0.5: 2 |
| core service stopped (API not answering, authenticated operations failing) | 1 |
| latency degradation (p95 ratio) | >= 20x: 3, >= 5x: 2, >= 1.5x: 1 |
| throughput drop (ingestion or database statement rate) | >= 80%: 3, >= 40%: 2, >= 15%: 1 |
| ingestion stall | >= 30 s: 3, >= 10 s: 2, >= 2 s: 1 |
| observation loss (machines disconnected, telemetry stale, counter reset) | 2 |
| duration of the degradation inside the fault window | >= 30 s: 2, >= 5 s: 1 |
| still degraded after recovery | 2 |
| recovery failed | 9 |
| slow recovery (>= 30 s) | 2 |
| scope: each additional affected area | 1 |

**Confidence is separate.** It starts high for a direct measurement at the fault target (medium for downstream
only) and drops one step for a small or truncated window and one for an unhealthy baseline. It is always low when
the baseline is incompatible or attribution is uncertain. A severe finding can carry low confidence.

Positive findings (availability held, ingestion continued, no database errors, fast recovery, integrity ok) are
reported when measured. No weakness is produced without a measured comparison behind it.

For OPC UA interruptions, missing ingestion while the data source is down is reported as **lost observation**, not
as an ingestion defect, and never as evidence that production stopped. For database latency, direct statement
delay, its amplification through SQLite's write lock (labelled unverified: the hook does not time lock waits), and
the API slowdown are separate findings. `sqlite_write_lock` is a different fault.

## Resilience score (`config/analysis.yaml` `score`)

| Dimension | Weight | 100 at | 0 at | Needs |
|---|---|---|---|---|
| availability | 0.25 | min(API, authenticated) success >= 0.999 during the fault | <= 0.5 | fault-window samples |
| latency | 0.15 | p95 ratio <= 1.5x | >= 50x (log-linear) | API latency in baseline and fault windows |
| throughput | 0.20 | ingestion >= 90% of baseline | <= 20%; minus 1 per stalled second (cap 30) | ingestion in both windows |
| recovery | 0.20 | stable recovery <= 5 s after the fault | >= 90 s or not recovered | samples after a known fault window |
| integrity | 0.10 | integrity check passed (at most 50 if tag ids went backwards) | check failed | a recorded integrity check |
| cleanup | 0.10 | removal verified by the injecting system | removal failed | a recorded removal check |

`overall = sum(weight x score) / sum(weights)` over all six dimensions, and only when every required dimension (all
six by default) was measured. Otherwise the overall is `insufficient_evidence` with the measured dimensions shown.
Missing weight is never redistributed. With an uncertain fault attribution the overall is withheld, and an unhealthy
or incompatible baseline makes it `qualified`. The defaults are lab choices: 0.999 availability tolerates a single
failed probe in a long window, 1.5x p95 is the noise seen between healthy baselines, and the 90 s recovery bound is
the controller's recovery timeout. The score describes resilience to the supplied failure under the observed
conditions, not the application in general.

## Read-only boundary

- **The analysis code** reads `results/<id>/` and external reports and writes only under `results/analysis/`. A
  test fails if it calls the controller, `LabApi.run`, `submit`, `emergency_stop` or plant start/stop, and another
  checks that experiment records are byte-for-byte unchanged.
- **The optional observation** (`--observe`) uses the controller's own read-only probes (health, an authenticated
  read, equipment states, `/metrics`, `/proc`, hook counters) for at most 120 s, with no workload and no fault.
- **The Failure Impact Analyst** (`chaoslab/analysis/narrative.py`) holds six tools: `get_analysis`, `get_result`,
  `get_recovery`, `get_metrics`, `get_logs` and `list_runs`. `ReadOnlyToolBox` refuses to offer or call any tool the
  MCP server does not annotate read-only, and any tool in `FORBIDDEN_TOOLS`. A role that lists one fails before the
  model sees anything, and the approver denies every request. The prompt is a second line, not the enforcement.
- **The narrative** must match a schema, and every evidence reference must resolve to a real record (the analysis,
  the run's `result.json`, its samples or log files). Invalid answers are retried at most 3 times, then the narrative
  is marked `incomplete`. Model failure never affects the deterministic results and never triggers anything.

## Artifacts

`results/analysis/<analysis-id>/`: `analysis.json` (deterministic, machine-readable), `report.md` (summary and
details), and `narrative.json` (the AI interpretation, kept separate, with attempts and transcript). Baseline
assessments are in `results/analysis/baselines/`, observations in `results/analysis/observations/`. The
controller's original verdict is copied under `controller_verdict` and never edited; disagreement appears only in
the narrative with evidence.

## Architecture

```
evidence.py   load + normalise (lab run | chaos-opcua report | artifact), windows, fault timing, attribution
baseline.py   baseline quality checks and persistence; bounded read-only observation
measure.py    per-window metrics (nearest-rank percentiles, counter segments, hook and cgroup deltas)
compare.py    compatibility of baseline and run; per-metric comparisons
recovery.py   recovery time from samples; recovery evidence checklist
findings.py   weakness and positive rules; severity points; confidence
fixes.py      recommendations with source locations verified against the upstream checkout at analysis time
score.py      dimensions, weights, insufficient_evidence
engine.py     pipeline, evidence-reference resolution, persistence
narrative.py  read-only analyst role, ReadOnlyToolBox, schema validation, bounded retries
report.py     Markdown report;  service.py: entry points for the CLI
```

The pipeline is a fixed sequence of deterministic steps followed by one optional model step, so it is plain code
rather than a LangGraph graph: a graph would add a dependency to the lab's environment without adding a branch.
The LLM never computes a number.

## Example (generated 2026-10-11 from recorded runs; the reports are under `results/analysis/`)

| Run | Baseline | Attribution | Recovery (this analysis) | Score | Top findings |
|---|---|---|---|---|---|
| `database-latency-200ms-10s-4d0f29` | own, healthy | confirmed (43 delayed statements in the window) | recovered 0.12 s | insufficient_evidence (ingestion unmeasured during the fault: `/metrics` probes timed out) | W01 medium: DB statement rate -90.8%; W02 medium (low confidence, 2 samples): API p95 77x; W03 low: delayed statements 282.6 ms vs 200.2 ms injected (write-lock amplification, unverified); W04: 3 statements finished after the window at 2780 ms each |
| `opc-server-outage-10s-54707e` | separate baseline-observe run, compatible | confirmed (opc-sim not alive) | recovered 5.88 s | 79.8 (throughput 0: no ingestion while the source was down) | W01 medium: machines connected in 30% of fault samples (lost observation, not a production stop) |
| chaos-opcua `5ae80c5ade84` | own, healthy | confirmed (probe could not reach the server) | recovered 8.02 s | insufficient_evidence (no API latency, no integrity check in the report) | W01 medium: machines connected 19%, telemetry stale 62%; W02 low: production counters reset after the restart |
| `api-cpu-limit-20pct-15s-0eef45` | own, healthy | confirmed (93% of periods throttled) | recovered 0.06 s | 87.2 | W01 medium: API p95 27x; W02 low: CPU throttling |

## Limitations

- The lab samples once per second and each sample's API calls are themselves slowed by some faults, so fault
  windows can hold few samples. Such comparisons are marked qualified.
- The API latency metric is one health-check probe per sample, not the application's full request mix.
- The database hook does not time lock waits; write-lock amplification is inferred from delayed-statement time and
  labelled unverified.
- An integrity pass shows file consistency, not that no business operation was lost. Process liveness is never
  used as recovery evidence.
- Recommended fixes are not applied and their acceptance criteria are not tested by this tool.
- The AI narrative has not been run against a real model in this environment (no LLM key). It is tested with
  deterministic scripted model responses.
