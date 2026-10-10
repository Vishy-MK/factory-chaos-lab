# Target application: FactorySemantics MES

This document records what was inspected in the checked-out revision and what was run on this machine.
Statements are marked **Verified** (read in source or observed running here) or **Assumed / not verified**.

- Repository: `https://github.com/factorysemantics/factorysemantics-mes`
- Revision inspected: `b90ca1c81e3b520be629999fff8bd9ffb82ad514` (recorded in `UPSTREAM_COMMIT`)
- Package: `factorysemantics-mes` 0.2.0, import name `fsmes`, console script `fsmes`
- Licence: Apache-2.0, `LICENSE` and `NOTICE` preserved in `factorysemantics-mes/`
- Status declared by the project: pre-alpha, simulated plants only. Nothing in it has run on a real plant.

## 1. What the application is

A Python MES (manufacturing execution system): work orders and routings, OPC UA machine connectivity, OEE,
quality/SPC, maintenance, scheduling, genealogy, a REST API with a plain-HTML operator UI, an ERP connector and an
MCP tool surface. It ships its own deterministic line simulator and a scoring harness that measures whether the MES
"told the truth" about the simulated plant. **Verified** (source and README).

## 2. Deployment modes

| Mode | What runs | External services | Status on this machine |
|---|---|---|---|
| `fsmes demo` | API, OPC UA simulator, OPC agent, mock ERP, ERP sync, all in one process, SQLite | none | **Verified working** (see 3.1) |
| `fsmes plant <name> start` | one OS process each for the OPC UA replay server, OPC agent, API and the simulated floor, plus a log sink; SQLite file per plant | none | **Verified working**; this is what the lab uses |
| `fsmes lab run` / `fsmes score` | an ephemeral plant on its own ports (API 8100, OPC 4900), played at a compressed clock, then scored | none | **Verified working** |
| Docker Compose (`docker/docker-compose.yml`) | PostgreSQL 16, migrate job, API behind nginx, OPC sim, OPC agent, mock ERP, ERP sync | Docker, PostgreSQL, nginx | **Not run**: Docker is not installed |
| systemd units (`deploy/`) | the `fsmes plant` supervisor as a service | systemd | Not used |

The full multi-service deployment was not justified: the lightweight mode contains every component needed for the
experiments that can be run safely (OPC UA server, agent, API, floor, database file).

## 3. Interfaces used by the lab

### 3.1 Commands (all **Verified** by reading `src/fsmes/cli.py` and running them)

| Command | Purpose | Used by the lab |
|---|---|---|
| `fsmes --version` | version | yes |
| `fsmes demo --duration N` | one order through ERP, MES, machines, ERP | run once to verify the install |
| `fsmes init-db`, `fsmes seed`, `fsmes db-status` | schema and demo seed | indirectly |
| `fsmes sim-generate <line.json> --out <dir>` | generate a line's replay CSVs deterministically from a description | yes (`prepare`) |
| `fsmes plant <name> init/start/stop/status/run/migrate [--speed N]` | run a pack as a plant | yes |
| `fsmes run-api`, `run-opc-sim --replay`, `run-opc-agent`, `run-operations` | the individual components | yes, to restart one component |
| `fsmes lab run <plan.toml> --results <dir>` | build an ephemeral plant, play a scripted scenario, measure | yes (application experiments) |
| `fsmes score <plant>` | one scripted hour, scored | not run; `lab run` is used instead (both call `fsmes.sim.runner.scored_run`, read in source) |
| `fsmes sweep`, `fsmes runs` | vary a knob and score each variant | not used |

Command names were taken from `--help` and source, not from documentation prose.

### 3.2 HTTP API (**Verified** from the running plant's `/openapi.json`)

Public (no credential): `GET /health`, `GET /metrics`, `GET /shadow`, `GET /pack`.
Authenticated (`POST /auth/login` with a code and password, bearer token): everything else. The lab signs in with the
built-in read-only `ANALYST` account (capabilities `plant.read`, `audit.read`) and never writes.

Routes the lab reads:

| Route | Content used |
|---|---|
| `GET /health` | status, `watching` (machines, connected, disconnected, unknown), `floor.seconds_since` |
| `GET /metrics` | Prometheus text, see 3.3 |
| `GET /equipment/states` | state per machine |
| `GET /equipment/connections` | connection state per machine |
| `GET /workorders/summary` | status counts, `good_qty`, `scrap_qty` |
| `GET /kpis/orders` | per order ordered/good/scrap |
| `GET /kpis/oee/{code}?hours=` | availability, performance, quality, OEE, coverage and the reason a figure is withheld |
| `GET /analysis/downtime` | downtime pareto and unknown share |
| `GET /ops/services` | per-component log size and last write |

### 3.3 Prometheus metrics (**Verified** in `src/fsmes/api/routers/system.py` and by scraping)

The application *does* expose Prometheus text at `GET /metrics`. The series are deliberately few:

| Series | Meaning |
|---|---|
| `mes_plant_info` | plant identity |
| `mes_work_orders{status}` | count per status |
| `mes_erp_messages_pending` | outbox depth |
| `mes_tag_values_max_id` | highest tag value id; monotonic, so `rate()` is the ingestion rate |
| `mes_audit_entries_max_id` | highest audit id |
| `mes_equipment_coverage{equipment}` | share of the last 8 h the MES watched the machine |
| `mes_equipment_availability{equipment}` | **absent** when the MES cannot state it |
| `mes_equipment_not_observed_seconds{equipment,cause}` | unobserved seconds by cause |
| `mes_coverage_floor` | coverage the pack requires before a KPI is stated |
| `mes_replay_factor` | present only on a plant replaying faster than real time |

There is **no performance or OEE series**, by the project's explicit decision (the source comment says a figure the MES
withholds cannot be exported without its explanation). The lab reads OEE from `/kpis/oee/{code}` instead and reports
`null` when the MES withholds it.

There are no API request counters or latency histograms in the application. API latency and success in the lab are
measured by the lab's own probe, not by the application.

### 3.4 Logs (**Verified**)

Each component writes structured JSON lines to `MES_LOG_DIR/<component>.jsonl` with `timestamp` (UTC ISO),
`level`, `logger`, `event` and event-specific fields, and a combined coloured console log to the plant's log sink
(`plant.log`, capped at 200 MB by the sink). The lab's run directory is `runtime/work/logs/bottling/`.

### 3.5 Events

`GET /line/events` (line view feed), `GET /audit` (append-only audit, needs `audit.read`), the ERP outbox, and an
MQTT unified-namespace publisher (`fsmes uns`, needs the optional `mqtt` extra). The lab does not consume any of
them: it correlates logs, metrics and results by timestamp and experiment id.

## 4. Simulator and existing failure scenarios

### 4.1 How scenarios work (**Verified**)

A line is described by a JSON file (`labs/multiplant/bottling/line.json`: six stations, buffers, rates, and a list of
scripted events). `fsmes sim-generate` turns it into one CSV per station at one row per second, deterministically
from `seed`. `fsmes run-opc-sim --replay` serves those CSVs over OPC UA. The recording loops when it ends.

**Scenario events are data in the recording, not commands to a running plant.** There is no API that injects a
breakdown into a live plant. To play a fault you generate a different recording (or run `fsmes lab run`, which does
that in an ephemeral plant). This is why the lab has two experiment categories (section 6).

Event vocabulary (`src/fsmes/sim/generate.py`, `EVENT_TYPES`): `down`, `changeover`, `micro_stops`, `drift`,
`offset`, `quiet`, `scrap_burst`, `rate`, `counter_reset`, `starve`, `block`, `disconnect`.
`disconnect` is a fault of the observer: the replay closes its OPC UA endpoint while the line runs on.

### 4.2 Scenario inventory

| Scenario | Invoked as | Expected observable effect | State / metrics | Reset | Deterministic | Recovery check |
|---|---|---|---|---|---|---|
| Machine breakdown (`down`) | event in a plan | station reports `down`; neighbours starve then block | `/equipment/states`, downtime measurement | window ends | yes, same seed | station state after window; `breakdown_recall` |
| Changeover | `changeover` event | whole line `changeover`, order advances | states, downtime `planned_stops` | window ends | yes | not misclassified as downtime |
| Counter reset | `counter_reset` at second N | counters snap to 0 | booking comparison | instant | yes | MES books nothing for the reset |
| Micro-stops | `micro_stops` | short stops, pre-rolled from seed | OEE performance, booking | each stop ends | yes | stations running between stops |
| Scrap burst | `scrap_burst` | scrap rate raised | scrap counts, quality | window ends | yes | scrap returns to baseline |
| Starve / block | `starve`, `block` | idle without breakdown | states, downtime | window ends | yes | no invented breakdown |
| Disconnect | `disconnect` | agent reports machines disconnected | `/health.watching`, connection measurement | window ends | yes | agent reconnects |
| Drift / offset / quiet / rate | events | process value moves or goes silent | tag history | window ends | yes | not used by the lab |

### 4.3 Scoring harness (**Verified**)

`fsmes lab run` and `fsmes score` replay a scripted line in an ephemeral plant and compare what the MES recorded
with what the simulator knows it did. Measurements the lab uses: `booking` (units booked against units made,
inside a stated replay-overlap band), `downtime` (scripted breakdowns detected, planned stops not misclassified),
`oee` (availability, performance, quality per station against the script), `connection` and `latency`.

What it does **not** do: it scores the MES's account of a *simulated manufacturing hour*. It cannot score a live
persistent plant and says nothing about infrastructure recovery (process restarts, database locks). The lab does not
use it for infrastructure experiments (`scoring.applied: false` in those results).

## 5. Resource requirements (**Verified** by measurement, see `RESOURCE_ASSESSMENT.md`)

The persistent plant is five processes (about 550 to 600 MB RSS total). An ephemeral harness plant adds a similar
amount for 1.5 to 2 minutes. No GPU, no browser, no Docker.

## 6. Which interfaces can safely support automated experiments

| Need | Interface | Safe because |
|---|---|---|
| Play a scripted manufacturing fault and score it | `fsmes lab run` with a generated plan | ephemeral plant, own ports and database, never touches the persistent plant |
| Observe the live plant | `/health`, `/metrics`, read-only API | read-only account, loopback |
| Process-level faults on the live plant | signals to the plant's own component processes, restart with the plant's own environment | allowlisted component names, own user, reversible |
| Database fault | a second SQLite connection holding the write lock | no data change, rolled back, bounded |

## 7. Things found while running it

- `fsmes demo` fails on this machine with its default settings: the API defaults to port 8000, which another
  process on this host holds, and the uvicorn startup failure is reported only as a traceback. Setting
  `MES_API_PORT` fixes it.
- With an editable install and a working directory outside the checkout, the default relative config paths
  (`config/tag_map.json`, ...) do not resolve and the simulator has no line: the demo then "runs" but the order never
  starts. The lab sets absolute `MES_TAG_MAP_FILE`, `MES_LINE_LAYOUT_FILE`, `MES_INBOUND_MAPPING_FILE` and
  `MES_INBOUND_SQL_FILE`. A wheel install carries these files as package data.
- `fsmes plant <name> start` leaves detached children that inherit its stdout. Piping it (`... | tail`) never
  returns. The lab redirects to a file.

## 8. Not verified

- The upstream test suite was not run.
- The Docker Compose deployment, PostgreSQL mode, the ERPNext connector, MQTT publisher and both MCP servers
  were not run.
- Behaviour with more than one plant running at once (the lab runs one persistent plant).
