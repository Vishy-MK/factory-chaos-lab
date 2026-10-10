# Troubleshooting

Start with `scripts/health-check.sh` (exit 0 only when the API answers, every machine is connected and all four
components run) and `python -m chaoslab check` (prerequisites).

## `scripts/setup.sh`

| Symptom | Cause and fix |
|---|---|
| `python3-venv is missing` | `sudo apt install python3-venv` |
| `Python 3.12 or newer is required` | the upstream package requires 3.12 |
| `upstream is at <sha>, not the tested commit` | the checkout was moved; the lab still works, results record the commit |
| `sim-generate failed` | the upstream tree is incomplete; re-clone it |
| `fsmes plant bottling init exited N` | read `runtime/logs/plant-init.log` |

## Starting the plant

| Symptom | Cause and fix |
|---|---|
| `refusing to start: available memory ... below the safety threshold` | close the browser or lower `resources.min_available_memory_mb` in `config/safety-policy.yaml` knowingly |
| `already has running components` | `scripts/stop.sh`, then `scripts/start.sh` |
| `did not become healthy within 90s` | read `runtime/work/logs/bottling/plant.log` and `api.jsonl` |
| API port in use | another program holds 8010 (the upstream default 8000 is commonly taken); change `api_port` in the copied pack `runtime/fleet/packs/bottling/plant.toml` and the Prometheus config |
| The simulator runs but nothing is booked | a relative `config/*.json` path did not resolve; the lab sets absolute paths via `MES_*_FILE`, so this means the lab was bypassed and `fsmes` was started by hand from another directory |
| A command hangs after the plant started | the plant's children inherited a pipe (`fsmes plant start | tail` never returns); redirect to a file |

## Running experiments

| Result | Meaning |
|---|---|
| `rejected` | policy or schema refused it; `reasons` says which rule |
| `rejected_preconditions` | host memory/CPU gate, plant not healthy, or another experiment is active |
| `injection_failed` | the fault could not be applied; cleanup was still attempted and verified |
| `unsafe_cleanup_failed` | a fault could not be verified as removed. Run `scripts/emergency-stop.sh`, then `scripts/status.sh` |
| `not_recovered` | the fault was removed but the plant did not return to a healthy, ingesting state within the timeout |
| `recovered_with_data_findings` | the plant works again, but a data-consistency check failed (integrity or booking outside the harness's band); this is a finding, not a lab error |
| `error` | the harness or controller failed; `result.json` has `error` |
| `aborted` | emergency stop |

A stale `runtime/state/active.json` with a dead `controller_pid` is taken over automatically. If a lease appears
stuck, `scripts/emergency-stop.sh` releases it after cleaning up.

## Application experiments

- Takes about 2 minutes at the default 20x. They need roughly 600 MB beyond the persistent plant.
- `harness exit -15`: the run was stopped by the emergency stop.
- `no per-look states available`: the harness's watcher was off. The lab adds the `latency` measurement to
  enable it; this appears only if the adapter was bypassed.
- Booking differing by a few hundred units after a disconnect is the upstream-documented under-booking while
  unobserved (decision 0030 in the upstream repository), not a lab fault.

## Monitoring

| Symptom | Fix |
|---|---|
| `only N MB available; not starting monitoring` | free memory or set `MONITORING_MIN_AVAILABLE_MB` |
| Prometheus target `down` for `chaoslab` | the exporter is not running (`runtime/logs/exporter.log`) |
| Prometheus target `down` for `factorysemantics` | the plant is down; the exporter keeps answering |
| Grafana panels empty | Prometheus is not running, or retention (3 days) passed |
| `stop-monitoring.sh` does not stop it | pid files are in `runtime/state/`; remove them after killing by hand |

## Resetting

`scripts/stop.sh`, then remove `runtime/` (this deletes the lab's plant database, replay data and logs only) and run
`scripts/setup.sh` again. `results/` is never touched by setup, start or stop.
