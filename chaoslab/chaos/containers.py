"""The only code in the lab that talks to Docker, and only to the lab's own compose project.

Callers name a component from the allowlist (`api`, `opc-sim`, `opc-agent`, `postgres`), never a container, image,
command or Docker argument. A component resolves to exactly one container carrying both
`com.docker.compose.project=chaoslab` and `chaoslab.target=<component>`. The operations are a closed list: inspect,
stats, stop, start, pause, unpause, update CPU/memory, and disconnect/connect the one lab network. Nothing here
removes a container or a volume, runs an image, or executes a command inside a container, except the fixed
PostgreSQL readiness check.

Agents never reach this module directly: it is used by registered faults, their cleanup, the watchdog and the
emergency stop.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

PROJECT = "chaoslab"
NETWORK = "chaoslab_lab"
COMPONENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
TIMEOUT_S = 60


class ContainerError(Exception):
    pass


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


_runner: Runner = _default_runner


def set_runner(runner: Runner | None) -> None:
    """Tests replace the Docker CLI with a fake."""
    global _runner
    _runner = runner or _default_runner


def _docker(args: list[str], timeout: float = TIMEOUT_S) -> str:
    try:
        out = _runner(args, timeout)
    except FileNotFoundError as exc:
        raise ContainerError("the docker CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContainerError(f"docker {args[0]} timed out") from exc
    if out.returncode != 0:
        message = (out.stderr or out.stdout or "").strip().splitlines()
        raise ContainerError(message[-1] if message else f"docker {args[0]} failed")
    return out.stdout


def available() -> tuple[bool, str]:
    try:
        _docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
    except ContainerError as exc:
        hint = " (is your user in the docker group? see docs/DOCKER.md)" if "permission" in str(exc).lower() else ""
        return False, f"Docker is not usable: {exc}{hint}"
    return True, "docker daemon reachable"


def container_id(component: str) -> str:
    if not COMPONENT_PATTERN.match(component or ""):
        raise ContainerError(f"invalid component name {component!r}")
    out = _docker(["ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}",
                   "--filter", f"label=chaoslab.target={component}", "--format", "{{.ID}}"])
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if len(ids) != 1:
        raise ContainerError(f"expected one lab container for {component!r}, found {len(ids)} "
                             "(start the stack with scripts/docker-up.sh)")
    return ids[0]


def inspect(component: str) -> dict:
    data = json.loads(_docker(["inspect", container_id(component)]))[0]
    labels = data.get("Config", {}).get("Labels") or {}
    if labels.get("com.docker.compose.project") != PROJECT or labels.get("chaoslab.target") != component:
        raise ContainerError(f"container for {component!r} is not a lab container")
    state = data.get("State") or {}
    host = data.get("HostConfig") or {}
    networks = (data.get("NetworkSettings") or {}).get("Networks") or {}
    lab = networks.get(NETWORK)
    return {
        "component": component, "id": data["Id"], "name": data.get("Name", "").lstrip("/"),
        "status": state.get("Status"), "running": bool(state.get("Running")), "paused": bool(state.get("Paused")),
        "pid": state.get("Pid") or None, "health": (state.get("Health") or {}).get("Status"),
        "restart_count": data.get("RestartCount", 0), "started_at": state.get("StartedAt"),
        "nano_cpus": host.get("NanoCpus") or 0, "memory": host.get("Memory") or 0,
        "memory_swap": host.get("MemorySwap") or 0,
        "on_lab_network": lab is not None,
        "aliases": [a for a in (lab or {}).get("Aliases") or [] if a != data["Id"][:12]],
        "default_cpus": labels.get("chaoslab.cpus"), "default_memory_mb": labels.get("chaoslab.memory_mb"),
    }


def lab_components() -> list[str]:
    out = _docker(["ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                   "--format", '{{.Label "chaoslab.target"}}'])
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def stats(component: str) -> dict | None:
    """CPU percent and memory of a running container; None when it is not running."""
    info = inspect(component)
    if not info["running"]:
        return None
    out = _docker(["stats", "--no-stream", "--format", "{{json .}}", info["id"]], timeout=20)
    try:
        row = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None

    def bytes_of(text: str) -> float | None:
        match = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip())
        if not match:
            return None
        scale = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "KB": 1e3, "MB": 1e6, "GB": 1e9}
        return float(match.group(1)) * scale.get(match.group(2), 1)

    used, _, limit = (row.get("MemUsage") or "").partition("/")
    return {"cpu_percent": float(str(row.get("CPUPerc", "0")).rstrip("%") or 0), "memory_bytes": bytes_of(used),
            "memory_limit_bytes": bytes_of(limit)}


def stop(component: str, grace_s: int = 10) -> None:
    _docker(["stop", "-t", str(max(0, min(int(grace_s), 30))), container_id(component)])


def start(component: str) -> None:
    _docker(["start", container_id(component)])


def pause(component: str) -> None:
    _docker(["pause", container_id(component)])


def unpause(component: str) -> None:
    _docker(["unpause", container_id(component)])


def update_limits(component: str, nano_cpus: int | None = None, memory: int | None = None,
                  memory_swap: int | None = None) -> None:
    args = ["update"]
    if nano_cpus is not None:
        args += ["--cpus", f"{nano_cpus / 1e9:.3f}" if nano_cpus else "0"]
    if memory is not None:
        args += ["--memory", str(int(memory)) if memory else "0"]
        args += ["--memory-swap", str(int(memory_swap)) if memory_swap else ("-1" if not memory else str(int(memory)))]
    _docker(args + [container_id(component)])


def disconnect(component: str) -> None:
    _docker(["network", "disconnect", NETWORK, container_id(component)])


def connect(component: str, aliases: list[str]) -> None:
    args = ["network", "connect"]
    for alias in aliases:
        if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$", alias):
            args += ["--alias", alias]
    _docker(args + [NETWORK, container_id(component)])


def postgres_ready() -> tuple[bool | None, str]:
    """The fixed readiness check of the lab PostgreSQL: pg_isready, then a trivial query."""
    try:
        info = inspect("postgres")
    except ContainerError as exc:
        return None, str(exc)
    if not info["running"] or info["paused"]:
        return False, f"postgres container {info['status']}{' (paused)' if info['paused'] else ''}"
    try:
        _docker(["exec", info["id"], "pg_isready", "-U", "mes", "-d", "mes_twin"], timeout=15)
        out = _docker(["exec", info["id"], "psql", "-U", "mes", "-d", "mes_twin", "-tAc", "SELECT 1"], timeout=15)
    except ContainerError as exc:
        return False, f"postgres not ready: {exc}"
    return out.strip() == "1", "pg_isready ok; SELECT 1 returned " + out.strip()


def wait(component: str, condition: Callable[[dict], bool], timeout_s: float = 60, poll_s: float = 1.0) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    info = inspect(component)
    while time.monotonic() < deadline:
        info = inspect(component)
        if condition(info):
            return info
        time.sleep(poll_s)
    return info


def healthy(info: dict) -> bool:
    return info["running"] and not info["paused"] and info["health"] in (None, "healthy")
