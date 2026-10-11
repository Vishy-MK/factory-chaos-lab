# Docker deployment (opt-in)

The lightweight plant (fsmes on SQLite) stays the default. The lab can also drive a lighter FactorySemantics MES
stack in containers, selected with `CHAOSLAB_DEPLOYMENT=docker`; unset, nothing changes.

## Setup

Your user needs Docker access. With the snap package:

```bash
sudo addgroup --system docker && sudo adduser $USER docker
sudo snap disable docker && sudo snap enable docker
newgrp docker            # or log out and in
docker ps
```

Membership in the `docker` group is equivalent to root on this machine. The lab keeps Docker actions in one module
(`chaoslab/chaos/containers.py`) and never gives agents or MCP tools the Docker socket.

## The stack

`docker/compose.lab.yml` (project `chaoslab`, network `chaoslab_lab`) builds the upstream Dockerfile unchanged and
runs PostgreSQL 16, a one-shot `migrate` (schema, demo plant, the lab's read-only `ANALYST` account), the API, the
OPC UA simulator (1x) and the OPC agent. Only the API is published, on `127.0.0.1:8020`. Each service has memory
and CPU limits, health checks and a `chaoslab.target=<component>` label. Secrets are generated once into
`runtime/state/docker.env` (mode 600); the `ANALYST` password is shared with the lightweight deployment.

```bash
scripts/docker-up.sh         # build, start, wait until healthy, show status
scripts/docker-status.sh
scripts/docker-down.sh       # stop (keeps the database); --remove removes containers; --volumes also the data
CHAOSLAB_DEPLOYMENT=docker python -m chaoslab status
```

About 330 MB in total when idle.

## What the lab does with it

`CHAOSLAB_DEPLOYMENT=docker` selects the `bottling-docker` plant (`config/service-allowlist.yaml`) and the Docker
plant adapter (`chaoslab/adapters/docker_plant.py`): the same HTTP probes against port 8020, component state, CPU
and memory from the containers, and a PostgreSQL readiness check as the integrity check. Recovery does not
require floor progress there, because the stack has no floor simulator.

## External container faults: observe and analyse

The lab does not inject container faults itself. Another tool does (plain `docker` commands, Pumba, Chaos
Toolkit's Docker driver...), and the lab observes and analyses the result, read-only.

```bash
# 1. record the stack while the fault happens (10..600 s; detects the fault window itself)
CHAOSLAB_DEPLOYMENT=docker python -m chaoslab analysis record --seconds 120 --label api-stop
#    meanwhile, in another terminal, the external fault, for example:
docker stop chaoslab-api-1; sleep 10; docker start chaoslab-api-1
docker pause chaoslab-opc-sim-1; sleep 10; docker unpause chaoslab-opc-sim-1
docker network disconnect chaoslab_lab chaoslab-opc-agent-1; sleep 10
docker network connect --alias opc-agent chaoslab_lab chaoslab-opc-agent-1

# 2. analyse the recording: impact, ranked weaknesses, Recommender fixes, resilience score
python -m chaoslab analysis analyze results/analysis/recordings/<recording>.json
```

The recorder samples once a second (slower while the API is down) with the lab's own probes plus each container's
state, network attachment, restart count and CPU/memory limits. It detects the fault window from what it observed
(stopped, paused, off the lab network, restarted, limits changed), so attribution is
`confirmed_by_measurement`. When it sees no change, attribution stays `uncertain`; supply the timing with
`analysis analyze --fault-meta` if another system recorded it. External faults have no lab cleanup or removal
record, so the cleanup dimension of the score is unmeasured and the overall score is `insufficient_evidence` unless
that evidence exists.

## Observation in Grafana

When the stack is running, the exporter adds `chaoslab_container_*` (state, health, lab network attachment,
restarts, CPU, memory) and per-service impact for the `bottling-docker` plant. The **Injection impact** dashboard
shows both plants (`plant / service`) and a Docker stack section with container state, network attachment,
restarts, CPU and memory. The exporter collects the Docker view on its own thread, so a slow Docker call never
delays a scrape. The exporter must run with Docker access: after the group change, log in again or start
monitoring from a shell that has it (`newgrp docker`).

## Status

Implemented: the stack, the deployment switch, the adapter, container observation in the exporter and Grafana,
read-only recording of external container faults, and their analysis. The lab does not inject container faults
itself.
