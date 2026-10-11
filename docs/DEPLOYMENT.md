# LAN deployment

`scripts/deploy-lan.sh` serves the whole lab to the PCs on this machine's local network: Grafana, the MES web app
and API, the TeamSynapse RAG backend and the lab's control API. Every service keeps listening on `127.0.0.1`. One
gateway listens on the LAN address and forwards only clients from the LAN subnet, and every service has its own
login.

```bash
scripts/deploy-lan.sh up        # plant, monitoring + Grafana, RAG backend, control API, gateway
scripts/deploy-lan.sh status    # what runs, what listens on the LAN address, the URLs
scripts/deploy-lan.sh down      # stop the LAN exposure (gateway, control API, RAG backend)
scripts/deploy-lan.sh down --all  # also stop monitoring and the plant
```

## What other PCs open

| Service | URL | Login | Behind it |
|---|---|---|---|
| Grafana | `http://<lan-ip>:3000` | `viewer` (read only) or `admin` | the four lab dashboards; Prometheus stays internal |
| MES web app and API | `http://<lan-ip>:8010` | MES accounts (`ADMIN`, `SCOTT` rotated; see below) | the bottling plant |
| RAG backend | `http://<lan-ip>:8200/docs` | `X-API-Key: <BACKEND_API_KEY>` (all routes but `/health`) | TeamSynapse FastAPI, in a 1 GB / 2 CPU cgroup scope |
| Lab control API | `http://<lan-ip>:8090/api/help` | `Authorization: Bearer <CHAOSLAB_CONTROL_TOKEN>` | `LabApi` + `Lifecycle`: status, runs, analysis, run, emergency stop, plant start/stop |

Credentials are generated once into `runtime/state/deployment.env` (mode 600) and reused on every `up`. Share
them privately. Delete the file and run `up` again to rotate everything.

Example from another PC:

```bash
curl -H "Authorization: Bearer $TOKEN" http://<lan-ip>:8090/api/status
curl -H "Authorization: Bearer $TOKEN" -d '{"name": "api-process-outage-10s"}' http://<lan-ip>:8090/api/run
curl -H "Authorization: Bearer $TOKEN" -X POST http://<lan-ip>:8090/api/emergency-stop
curl -H "X-API-Key: $KEY" -F file=@manual.txt http://<lan-ip>:8200/api/rag/documents/upload
```

## How it is protected

- **Network scope**: the gateway binds only to the private address of the default-route interface and refuses
  connections from outside that subnet (plus loopback). A non-private address is refused outright. Every LAN
  connection is logged with the client address in `runtime/logs/gateway.log`.
- **Never exposed**: Prometheus (9090), the exporter (9108) and the OPC UA server (4841).
- **Grafana**: the documented lab admin password is replaced on the first `up`; a separate `viewer` account has the
  Viewer role; sign-up and anonymous access are off.
- **MES**: `deploy-secure-plant` replaces the publicly known lab passwords of `ADMIN`, `SCOTT`, `AGENT` and `ANALYST`
  through the MES's own `fsmes set-password`. If that fails, `up` stops before anything is exposed. The lab's reader
  (`ANALYST`) reads its new password from `deployment.env`, so plain `chaoslab` commands keep working.
- **RAG backend**: an API key on every route except `/health` (`ApiKeyMiddleware`, only active when
  `BACKEND_API_KEY` is set).
- **Control API**: a bearer token of at least 24 characters, checked in constant time; loopback bind enforced in
  code. A remote request goes through the same safety policy, lease, watchdog and audit as a local one. Every request
  and every refusal is in `runtime/state/audit.jsonl`.
- **Firewall (optional, needs sudo)**: `up` prints `ufw` rules limiting the four ports to the LAN subnet.

## Known limitations

- **Simulated floor accounts keep their lab passwords.** `FLOOR-SIM`, `FLOOR-SUP`, `FLOOR-PLAN` and `FLOOR-CREW` sign
  in with passwords fixed in the upstream floor simulator, which has no override, so changing them would stop the
  floor. Anyone on the LAN who knows those defaults can sign in to the MES as an operator, supervisor or planner
  (not as admin).
- **No TLS.** Traffic, including passwords and tokens, is plain HTTP on the LAN. Use it on a network you trust.
- **The lab controls can inject faults.** Anyone holding the control token can run allowlisted experiments against
  the plant. The policy still bounds what runs; the token is the approval.
- **The services see the gateway, not the client.** Their own logs show `127.0.0.1`; the client address is in the
  gateway log.
- **Not persistent across reboots or logout.** Run `scripts/deploy-lan.sh up` again; the address is re-detected
  (DHCP can change it).
- **A separate older `bosch-backend` system service** (from `~/bosch-backend`) listens on `0.0.0.0:8000` without
  authentication. It is not part of this deployment; stop it with `sudo systemctl stop bosch-backend` if it should
  not be reachable.
