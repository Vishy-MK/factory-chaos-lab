# Database latency fault (`database_latency`)

Adds a bounded delay before every SQL statement the plant's database clients execute against the plant database,
for a bounded window, then verifies the delay is gone and database latency is back to baseline.

```bash
scripts/start.sh                     # the plant must be (re)started by the lab so the hook loads
python -m chaoslab run experiments/definitions/database-latency-200ms-10s.yaml
python -m chaoslab run experiments/definitions/database-latency-200ms-10s.yaml --dry-run   # validate + preflight only
```

## Why a hook and not a proxy

The MES opens one SQLAlchemy engine (`fsmes/db.py`, `make_engine`) on the stdlib `pysqlite` driver, in-process,
against `runtime/data/bottling.db` in WAL mode. Every transaction begins `BEGIN IMMEDIATE` (or a read-only `BEGIN`
with `PRAGMA query_only`). There is no network hop, so a proxy or traffic shaping cannot delay it, and host network
shaping is prohibited anyway. The lab therefore delays statements inside the database clients with its own code,
leaving the upstream source unchanged.

## Mechanism

- `chaoslab/plant_names/chaoslab_dbhook.py` registers class-level SQLAlchemy listeners (`before_cursor_execute`,
  `after_cursor_execute`, `handle_error`) on `Engine`.
- It is loaded by the lab's existing `sitecustomize` (already on the plant's `PYTHONPATH` for process naming), only
  when the lab sets `CHAOSLAB_DB_HOOK_DIR`, `CHAOSLAB_DB_HOOK_DATABASE` and `CHAOSLAB_DB_HOOK_COMPONENTS`, and only
  in the components listed there: `database_clients` in `config/service-allowlist.yaml` (`api`, `opc-agent`).
  `opc-sim`, `floor`, the log sink and every process outside the lab plant never load it.
- It acts only on engines whose SQLite file is the plant database (`realpath` comparison).
- It is dormant until `runtime/state/db-latency/control.json` exists and names this plant and database, carries this
  boot's id, has a delay of 1..2000 ms and a CLOCK_MONOTONIC deadline in the future at most 120 s after activation.
  The file is re-checked about every 100 ms. CLOCK_MONOTONIC is shared by all processes on the host, so the deadline
  written by the lab is the deadline the plant enforces. Outside that window the injected delay is zero.

### What is delayed

| Delayed | Not delayed |
|---|---|
| every statement SQLAlchemy sends through a DBAPI cursor to the plant database: SELECT, INSERT/UPDATE/DELETE, `BEGIN`/`BEGIN IMMEDIATE`, SAVEPOINT/RELEASE, PRAGMAs sent with `exec_driver_sql` | connection creation and the connect-time PRAGMAs (raw DBAPI calls) |
| reads and writes alike | COMMIT and ROLLBACK (DBAPI `commit()`/`rollback()`, not cursor statements) |
| | code that opens `sqlite3` directly (backups, `ai_status` read-only probe) and the lab's own integrity check |

Statements executed after `BEGIN IMMEDIATE` sleep while their transaction holds SQLite's single write lock, so other
writers queue behind them. The live run below shows this: delayed statements took 66 ms longer than the injected
200 ms on average. That is the expected amplification on SQLite, not measurement error.

A statement already sleeping when the window closes finishes its sleep, so the fault can outlast `duration_s` by at
most one `delay_ms`.

## Parameters and limits

| Parameter | Default | Fault bounds | Policy (`safety-policy.yaml` `database_latency`) | Hook clamp |
|---|---|---|---|---|
| `delay_ms` | 200 | 1..1000 | `max_delay_ms: 1000` | 2000 |
| `duration_s` | 10 | 1..60 | `max_duration_s: 30` | 120 |

`database_latency.enabled: false` refuses the fault. The target is `{plant: bottling, resource: database}`; a path,
another plant, or a component target is rejected.

## Lifecycle

validation → resource gate → lease + watchdog → preflight → baseline → persisted intent → injection → bounded fault
window → cleanup → independent removal verification → recovery → results, as for every infrastructure fault.

- **Preflight** (`hook_loaded`): every database client's current pid must have a stats file written by the hook at
  startup. A plant started before this feature, or by hand without the lab, fails with an instruction to restart it
  (`chaoslab stop && chaoslab start`). **Prerequisite: restart the plant once through the lab before the first run.**
  The restart happens before the experiment, never during it. Components restarted by other faults (`process_outage`)
  get the hook again because the lab restarts them with the same environment.
- **Preflight** (`no_activation_present`): a leftover activation file refuses the run; `chaoslab emergency-stop`
  removes it. Stale files cannot delay anything anyway (expired deadline or another boot id).
- **Injection** writes the activation file atomically. The intent record is in the lease first, so a crash between
  the two still leaves something to clean up.
- **Cleanup** removes the activation file. It is idempotent and needs nothing from the record, so it works after
  partial activation, from the watchdog after the controller dies, and from the emergency-stop sweep even when no
  lease records the fault. Even with no cleanup at all, the hook stops at the deadline.
- **Removal verification**: the file is absent; after waiting out one delay plus two flush intervals, no hooked
  process reports any further delayed statement over another two flush intervals.
- **Recovery**: the existing stable-sample checks (authenticated API access, all machines connected, ingestion
  advancing, floor recording, database integrity), plus `database_latency_restored`: no injected statement after
  recovery, and the post-recovery mean statement duration at most baseline mean + max(25 ms, 50 % of baseline mean).
  If that stage fails, the result is `not_recovered`.

## Evidence in `result.json`

`observed_metrics.<phase>.database` and `database_latency` hold the direct database measurements from the hook:
statement counts by kind (read, write, transaction, other), rate, errors, `database is locked` errors, injected
statements, injected delay, exact means, and histogram percentiles. Percentiles are bucket upper bounds, null above
the last bucket. `database_latency.downstream` holds the effects the lab sees from outside: API success ratio and
latency distribution, authenticated success, tag ingestion rate, longest ingestion stall, floor freshness, and
machines connected. `delayed_statement_ms_mean` and `measured_added_statement_ms_mean` are exact because the hook
times each delayed statement. `fault_window` is the sample-delimited activation window and is approximate at its
edges. Unavailable values are `null`, never zero.

## Live result on this host (2026-10-10, `20261010T164227Z-database-latency-200ms-10s-4d0f29`)

| | baseline | fault window / fault | recovery | post |
|---|---|---|---|---|
| statements/s (api + opc-agent) | 114.9 | 10.6 (window) | 91.5 | 125.3 |
| mean statement ms | 0.114 | 162.1 (window), 266.7 per delayed statement | 0.057 | 0.108 |
| injected delay per delayed statement | — | 200.15 ms (170 statements) | 0 | 0 |
| DB errors / lock errors | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| API p50 / p95 ms | 8.1 / 14.5 | 10.7 / 1210.1 | 7.2 / 7.4 | 7.5 / 8.3 |
| API success ratio | 1.0 | 1.0 | 1.0 | 1.0 |
| tag ingestion /s, longest stall | 14.7, 0 s | 12.1, 2.06 s | 6.0, 0 s | 14.4, 0 s |

Overall result `recovered`; recovery verified 0.51 s after cleanup; all stages ok. The fault phase has only 6 API
samples over about 12.6 s because each sample's API calls were themselves slowed. That is a downstream effect, and
the sample counts are reported with every phase.

## Limitations

- SQLite in WAL mode with a 15 s busy timeout absorbs a 200 ms delay without errors. Longer delays, or more
  concurrent writers, would be needed to produce `database is locked` failures.
- Only the two database clients are delayed. The floor simulator is affected only through the API.
- This is in-process statement latency, not network latency. A PostgreSQL deployment with a real network hop would
  need a proxy-based fault instead.
