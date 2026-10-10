# Resource limit fault (`resource_limit`)

Caps one plant component's RAM and/or CPU through cgroup v2 for a bounded window, then lifts the caps and verifies
recovery. This is the lab version of the OS-level limits used for the TeamSynapse backend
(`backend/scripts/run_constrained.sh`): the same kernel mechanism, applied to the MES under test with the lab's
safety lifecycle and agent tools.

```bash
scripts/start.sh
scripts/inject-resource-limit.sh api --cpu 20                       # 20 % of one CPU for 15 s
scripts/inject-resource-limit.sh opc-agent --cpu 10 --memory-high 80 --duration 10 --baseline 15
scripts/inject-resource-limit.sh api --memory-max 96                # hard limit; OOM kill if the working set exceeds it
scripts/inject-resource-limit.sh api --cpu 20 --agent               # Injector agent runs it, Recovery Analyst judges
scripts/inject-resource-limit.sh last
python -m chaoslab run experiments/definitions/api-cpu-limit-20pct-15s.yaml
```

## Parameters

| Parameter | Kernel file | Effect | Bounds (fault / policy) |
|---|---|---|---|
| `cpu_percent` | `cpu.max` (`<pct×1000> 100000`) | CPU quota; 100 = one full CPU; throttling above it | 5..400 / at least 5 |
| `memory_high_mb` | `memory.high` | reclaim and throttle above it; no kill | 32..4096 / at least 64 |
| `memory_max_mb` | `memory.max` | hard limit; the kernel OOM-kills the component above it | 32..4096 / at least 64 |
| `duration_s` | | how long the limits stay | 1..60 / at most 30 |

At least one limit is required, and `memory_high_mb` must not exceed `memory_max_mb`. With any memory limit,
`memory.swap.max` is set to 0, so the limit applies to RAM rather than pushing pages to swap. Targets are the
allowlisted components `api`, `opc-sim`, `opc-agent` and `floor`. `resource_limit.enabled: false` in
`config/safety-policy.yaml` turns the fault off.

## Mechanism

- Each limited component runs in its own transient user-systemd scope, `chaoslab-bottling-<component>.scope`,
  under `user@<uid>.service/app.slice`, which systemd delegates to the user. The scope is created with
  `CPUWeight=100` (the default weight). On systemd 255 that is what enables the cpu controller for it;
  `CPUAccounting=yes` alone does not.
- **Preparation, before the baseline:** if the component is not in its scope yet, the lab restarts it there once,
  with the plant's environment and the database latency hook, and waits until the plant is healthy again. Later runs
  find it already in the scope and skip the restart. The restart is recorded under `preparation` in the result and is
  not part of the fault window.
- **Injection:** first starts the self-limiting reset helper (`holders resource_reset`), then writes the limits
  directly to the scope's cgroup files. `systemctl set-property CPUQuota=` does not reset `cpu.max`, so the lab writes
  the files itself.
- **Cleanup:** stops the helper (which lifts the limits as it exits) and restores the recorded original values
  (unlimited). If the kernel OOM-killed the component, cleanup restarts it in its scope. Cleanup is idempotent and
  works from an intent-only record, from the watchdog, and from emergency stop. Emergency stop lifts limits on every
  lab scope, whether or not a lease records them.
- **Removal check:** limits back to the originals, the helper gone, the component running.
- **Recovery:** the standard stable-sample checks, plus `resource_limits_lifted`: after recovery the scope reports
  unlimited limits, zero throttled CPU periods and zero `memory.high` events. If it fails, the result is
  `not_recovered`.

## Evidence in `result.json`

Each sample carries the scope's counters (`memory.current`, `memory.peak`, `memory.events`, `cpu.stat`, and PSI
`memory.pressure` / `cpu.pressure`). `observed_metrics.<phase>.cgroups` and `resource_limit.cgroup` summarise them
per phase and for the exact `limit_window`: CPU used, throttled periods and ratio, throttled time, CPU and memory
stall time, memory max/mean, `memory.high`/`memory.max` events, and OOM kills. `resource_limit.downstream` holds the
API, ingestion and machine-connection effects. Counters of a scope that was recreated inside a phase (after an OOM
restart) are counted from zero. Unavailable values are `null`.

## Live results on this host (2026-10-10)

| Run | Limit | Inside the window | Downstream | Result |
|---|---|---|---|---|
| `20261010T171434Z-api-cpu-limit-20pct-15s-0eef45` | api `cpu.max` 20 % | CPU 18 % (baseline 8 %), 93 % of periods throttled, 6.8 s CPU stall | API p50 9 → 130 ms, p95 16 → 467 ms; success 100 %; ingestion 14.8 → 13.8 /s | recovered in 0.94 s |
| `20261010T171550Z-api-memory-high-64mb-15s-e0035e` | api `memory.high` 64 MiB, no swap | scope reclaimed 106 → 60 MB at once; 0 `memory.high` events, 0.2 ms memory stall | API p50 7.9 → 11.9 ms | recovered in 0.88 s |
| `20261010T171944Z-opc-agent-limit-cpu10-high80-10s-916683` | opc-agent 10 % CPU, `memory.high` 80 MiB | 42 % of periods throttled; the agent needs about 4 % CPU | no ingestion loss, 6/6 machines connected | recovered in 0.84 s |

The 64 MiB `memory.high` run shows that about 45 MB of the API scope's 106 MB was reclaimable page cache. Its
anonymous memory fits under the 64 MiB policy minimum, so a `memory_max_mb` within policy does not OOM-kill the API
on this plant. The OOM-and-restart path is covered by tests, not by a live run.

## Limitations

- The limits act on the component's own scope only. Host memory is not consumed (`memory_pressure` stays disabled).
- The first run against a component restarts it once before the baseline.
- `cpu_throttled_ms` is the kernel's `throttled_usec` and sums over the scope's CPUs, so it can exceed wall time.
- PSI stall times are `some` totals: time in which at least one task in the scope was stalled.
