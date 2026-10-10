# Licences

Four groups are kept distinct. None of this is legal advice; it records what the installed metadata and the
repositories state.

## 1. Upstream FactorySemantics MES

- Apache License 2.0. `factorysemantics-mes/LICENSE` and `factorysemantics-mes/NOTICE` are present and unmodified
  (`scripts/setup.sh` refuses to continue if either is missing).
- `NOTICE`: "Copyright 2026 Scott Kalwei". It states provenance from the author's earlier projects (MES-TWIN,
  LineSim), public standards and public documentation, and says the software *depends on, but does not include*
  FactorySemantics (MIT).
- The upstream tree is not modified. The lab keeps a **copy** of the bottling pack in `runtime/fleet/packs/bottling`
  (Apache-2.0, derived from `labs/multiplant/bottling`) and regenerates its replay data there. The copy carries the
  pack's own files unchanged apart from the generated `out/` directory. `runtime/` is not part of the lab's source.
- The repository's `.gitignore` excludes `factorysemantics-mes/` so the upstream checkout is not re-committed inside
  the lab repository; it is cloned by `scripts/setup.sh` at the commit in `UPSTREAM_COMMIT`.

## 2. This laboratory's original code

`chaoslab/`, `scripts/`, `config/`, `experiments/`, `observability/` (excluding downloaded binaries), `tests/`,
`docs/`. **No licence has been chosen.** Without one, the code is "all rights reserved" by default. Pick one before
publishing. Apache-2.0 would be consistent with the upstream project but that is the owner's decision.

No upstream source code was copied into the lab. The lab calls upstream through its command line, its HTTP API,
process signals and (in one place, `FactorySemanticsAdapter.plant_environment`) two upstream functions
(`fsmes.plant.plant_env`, `fsmes.pack.fleet.load`) imported at run time to reproduce the plant's own environment
when restarting one component.

## 3. Python dependencies

Read from installed package metadata in `.venv` (51 distributions at the time of writing).

| Licence | Packages |
|---|---|
| MIT / MIT-0 | aiosqlite, alembic, annotated-doc, annotated-types, anyio, cffi, fastapi, h11, httptools, iniconfig, mako, markdown-it-py, mdurl, pip, pluggy, pydantic, pydantic-settings, pydantic_core, pytest, pytz, pyyaml, rich, six, sqlalchemy, typer, typing-inspection, uvloop, watchfiles |
| BSD-2 / BSD-3 | click, httpcore, httpx, idna, markupsafe, psutil, pycparser, pygments, python-dotenv, starlette, uvicorn, websockets |
| Apache-2.0 | opentelemetry-api, sortedcontainers |
| Dual (Apache-2.0 or BSD) | cryptography, packaging, python-dateutil, structlog (MIT or Apache-2.0) |
| ISC | shellingham |
| PSF-2.0 | typing_extensions |
| MPL-2.0 | certifi |
| **LGPL-3.0-or-later** | **asyncua** |
| Apache-2.0 (upstream) | factorysemantics-mes |

**Finding: `asyncua`** (the OPC UA library the upstream MES depends on) is LGPL-3.0-or-later, not Apache-2.0. The
lab installs it unmodified from PyPI as a separate library and links to it only through upstream. Using and
dynamically importing an LGPL library is permitted; modifying it, vendoring it, or distributing a bundle that
includes it would bring LGPL obligations (provide its source and allow replacement). The lab does neither. Anyone
who redistributes a container image or a frozen bundle of this lab must account for it.

`certifi` is MPL-2.0 (file-level copyleft); unmodified use has no obligations beyond keeping its notice.

`psycopg` (LGPL-3.0) is an *optional* upstream extra for PostgreSQL and is **not installed**.

## 4. Optional downloaded tools and container images

| Item | Licence | Notes |
|---|---|---|
| Prometheus 3.15.0 | Apache-2.0 | downloaded by `scripts/install-monitoring.sh` with SHA-256 verification; not redistributed |
| Grafana OSS 13.2.3 | AGPL-3.0 | optional, downloaded the same way; used unmodified as a local tool. AGPL network-use obligations apply to anyone who modifies Grafana and offers it to others; this lab does not |
| `postgres:16-alpine`, `nginx:alpine` | PostgreSQL licence / BSD-2 | referenced only by upstream's `docker-compose.yml`; **not pulled or run** (Docker is not installed) |
| `ghcr.io/factorysemantics/fsmes` | Apache-2.0 per upstream | not pulled |

## 5. Data

Everything the lab plant shows is simulated. The pack data (`ACME Beverages`) is upstream's fictional data. No
production data, employer data or credentials are used. The accounts used are upstream's documented lab-only
accounts; their well-known passwords are for this loopback-only lab and must not be reused.
