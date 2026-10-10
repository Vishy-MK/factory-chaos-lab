# Safety

The lab is an isolated test environment on one laptop. Everything below is enforced in code and covered by tests
unless it is listed under "Not enforced".

## What can be touched

An experiment may only act on resources named in `config/service-allowlist.yaml`:

- hosts: `127.0.0.1` only. A target may not carry `host`, `address`, `ip`, `url` or `port`; such keys are rejected.
- plants: `bottling` only (the lab's own plant).
- components of that plant: `api`, `opc-sim`, `opc-agent`, `floor`. They are found by command line and by the
  `MES_PLANT_NAME` in their environment, so a same-named process belonging to something else is not matched.
- resource: the plant's SQLite database file, addressed as `database`, never by path.
- per fault: which of those components the fault may target (`fault_targets`).
- cgroup scopes: only `chaoslab-<plant>-<component>.scope` under this user's systemd `app.slice`, derived from the
  allowlisted component name. No cgroup path is ever taken from a definition or a record.

Anything else is rejected before anything runs. There is no way to name an arbitrary process, address, container or
file.

## Limits (`config/safety-policy.yaml`)

| Limit | Value | Enforced where |
|---|---|---|
| Maximum fault duration | 60 s | policy validator, and again at run time (`min(hold, max)`) |
| Simultaneous faults | 1 | one lease; steps run sequentially |
| Compound experiments | disabled | policy; enabling them still caps steps at 3 |
| Baseline | 5 to 60 s | policy |
| Recovery timeout | 90 s | policy, per experiment may only be lower |
| Whole experiment wall time | 600 s | policy (estimate) and harness timeout 420 s |
| CPU pressure | at most 80 % duty, 30 s | policy and the helper process clamps again |
| Memory pressure | disabled | `memory_pressure` is listed as unsupported |
| Resource limit | memory at least 64 MiB, CPU at least 5 % of one CPU, 30 s, at least one limit; can be switched off | policy (`resource_limit`); the reset helper clamps its own deadline to 120 s |
| Database latency | at most 1000 ms per statement, 30 s; can be switched off | policy (`database_latency`); the hook clamps again to 2000 ms and a 120 s window |
| Workload | at most 5 requests/s; high-volume profiles refused | policy and generator |
| Available memory | refuse when below 1500 MB | resource gate before every experiment and before start |
| Host CPU | refuse above 85 % | resource gate |
| Scripted simulator fault | at most 60 s of wall time at the chosen speed; speed 1 to 30 | policy |

## Lifecycle guarantees

1. **Validate**: schema, then policy. Rejections are persisted as results with reasons.
2. **Preflight**: host resources, plant health (API up, every machine connected, every component running), fault
   preconditions (component present, database lock free; for database latency: hook loaded in every database
   client and no activation file present).
3. **Lease**: one active experiment at a time (`runtime/state/active.json`, atomic writes, stale leases from dead
   controllers are taken over).
4. **Intent record before injection**: the lease lists what is about to be done before it is done, so cleanup is
   possible even if the controller dies between the two.
5. **Independent watchdog**: a separate process (own session) started per experiment. It cleans up the lease's
   records if the controller process disappears or the deadline passes, then exits. It is bound to the experiment id
   so it cannot act on a later experiment.
6. **Self-limiting helpers**: the CPU burner and the SQLite lock holder end themselves at their deadline and clamp
   to 120 s regardless of what they are told. The database latency hook ignores its activation once the
   CLOCK_MONOTONIC deadline written into it has passed, if it is from another boot, or if its window exceeds 120 s.
   A resource limit is only written after its reset helper is running; the helper lifts the limits at the deadline
   or when it is told to stop.
   If the controller and watchdog both died, all of them still stop.
7. **Cleanup is verified**, not assumed: every fault has a separate `removed` check (process running, process not
   stopped, lock acquirable, affinity restored, burner gone, latency activation absent and no further delayed
   statement). A failed cleanup yields `unsafe_cleanup_failed` and no
   recovery claim.
8. **Recovery is application evidence**: API up, an authenticated operation succeeds, every machine connected,
   the floor recording checks, tag ingestion advancing, database integrity check ok.
9. **Audit**: `runtime/state/audit.jsonl` records submission, state changes, injection, cleanup, finish and
   emergency stops with timestamps and the experiment id.

## Emergency stop

`scripts/emergency-stop.sh` (or `python -m chaoslab emergency-stop`):

1. writes the stop flag; the running controller checks it every 250 ms in every wait and aborts, cleans up and
   records `aborted`;
2. waits up to 20 s for the lease to be released; if it is not, it runs the lease's cleanup records itself;
3. sweeps for stopped plant components (SIGCONT) and lab helper processes (SIGTERM), and removes any database
   latency activation file, recorded in a lease or not, and lifts any limits left on a lab cgroup scope.

It does not restart components that are not part of an active lease, does not touch Docker, and does not touch
unrelated services.

## What the lab never does

- No host firewall, route or traffic-shaping changes.
- No Docker control and no Docker socket access (Docker is not installed).
- No deleting or moving database files; the write-lock fault only holds a lock and rolls back, and the latency
   fault only adds sleeps before statements. Neither changes data.
- No restart of anything outside the four plant components.
- No exposure to the LAN: the plant API binds `127.0.0.1`, the exporter refuses a non-loopback bind, and the lab has
  no network-facing control API.
- No credentials in logs: log excerpts pass through a redaction filter; the environment is never dumped.

## Privileges

Everything runs as the unprivileged desktop user. `cpu_pressure` changes CPU affinity of the user's own process and
restores it. `resource_limit` writes cgroup files of scopes the user's own systemd manager delegates to the user. Signals are sent only to that user's processes. No `sudo`.

## Not enforced / limits of the guarantees

- A watchdog is a process on the same machine. A power loss, kernel panic or `kill -9` of the whole session cannot be
  recovered by it. After a reboot nothing is left running; the plant must be started again.
- `process_outage` with `KILL` can interrupt an in-flight database transaction in the killed component; SQLite
  rolls it back, but a transaction that was in flight is lost. The integrity check verifies the file, not that no
  business operation was dropped.
- The simulator experiments run in the upstream harness's own ephemeral plant on fixed ports (API 8100, OPC 4900).
  The lab's teardown check looks for leftover `fsmes` processes that are not the persistent plant's; it does not
  prove those ports are free if an unrelated program took them.
- The resource gate samples memory when an experiment starts. It does not stop an experiment if memory later falls.
- The CPU pressure experiment shares the pinned CPU with whatever else the desktop is running on it.
- `resource_limit` restarts its target component once, before the baseline, to move it into its scope. A hard memory
  limit below the working set ends in a kernel OOM kill of that component; cleanup restarts it. The OOM kill can
  interrupt an in-flight transaction like `process_outage` with `KILL`.
- `--dry-run` checks the definition, the policy and the resource gate; fault-specific preconditions (hook loaded,
  cgroup scopes available) are checked only by a real run, before anything is injected.
- The database latency hook runs inside the plant's api and opc-agent. It catches its own errors, but it is lab
  code in the application's process; it is only loaded when the lab starts the plant with `CHAOSLAB_DB_HOOK_*` set.
