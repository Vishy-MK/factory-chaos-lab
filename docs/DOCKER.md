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

## Status

Implemented: the stack, the deployment switch, the adapter and the allowlisted container module. Not implemented
yet: container fault types, the emergency-stop sweep for containers, container metrics in the exporter, and agent
runs against the stack.
