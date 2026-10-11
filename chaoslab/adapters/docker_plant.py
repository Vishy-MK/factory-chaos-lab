"""The FactorySemantics MES as the lab's compose stack: same HTTP surface, containers instead of processes."""

from __future__ import annotations

import subprocess
import time

from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter, PlantError
from chaoslab.chaos import containers


class ContainerHandle:
    """What callers of find_components() use: a pid (the container's main process on the host) and an id."""

    def __init__(self, info: dict):
        self.info = info
        self.pid = info.get("pid") or 0
        self.component = info["component"]

    def status(self) -> str:
        return "stopped" if self.info["paused"] else ("running" if self.info["running"] else "exited")


class DockerPlantAdapter(FactorySemanticsAdapter):
    deployment = "docker"

    def find_components(self) -> dict[str, ContainerHandle]:
        found = {}
        for name in self._components:
            try:
                info = containers.inspect(name)
            except containers.ContainerError:
                continue
            if info["running"]:
                found[name] = ContainerHandle(info)
        return found

    def process_table(self) -> dict[str, dict]:
        table = {}
        for name in self._components:
            row = {"pid": None, "alive": False, "status": "missing", "cpu_percent": None, "rss_mb": None}
            try:
                info = containers.inspect(name)
            except containers.ContainerError:
                table[name] = row
                continue
            row.update(pid=info["pid"], alive=info["running"] and not info["paused"],
                       status="stopped" if info["paused"] else (info["status"] or "unknown"),
                       container=info["name"], health=info["health"], restarts=info["restart_count"])
            if info["running"]:
                try:
                    usage = containers.stats(name) or {}
                except containers.ContainerError:
                    usage = {}
                row["cpu_percent"] = usage.get("cpu_percent")
                row["rss_mb"] = round(usage["memory_bytes"] / 1048576, 1) if usage.get("memory_bytes") else None
            table[name] = row
        return table

    def database_integrity(self) -> tuple[bool | None, str]:
        return containers.postgres_ready()

    def kill_component(self, component: str, sig=None, wait_s: float = 30.0) -> int | None:
        info = containers.inspect(component)
        if not info["running"]:
            return None
        containers.stop(component)
        return info["pid"]

    def spawn_component(self, component: str, scope_unit: str | None = None) -> int:
        if component not in self._components:
            raise PlantError(f"component {component!r} is not in the allowlist")
        containers.start(component)
        info = containers.wait(component, containers.healthy, timeout_s=90)
        return info.get("pid") or 0

    def signal_component(self, component: str, sig: int) -> int | None:
        raise PlantError("signals are not used in the Docker deployment; use container_pause")

    # -- stack lifecycle ----------------------------------------------------------------------------------------

    def _compose(self, *args: str, timeout: float = 600) -> int:
        compose = self.settings.root / self.settings.lab["docker"]["compose_file"]
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.settings.log_dir / "docker-compose.log").open("ab") as log:
            env_file = self.settings.state_dir / "docker.env"
            return subprocess.run(["docker", "compose", "--env-file", str(env_file), "-f", str(compose), *args],
                                  cwd=self.settings.root,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False).returncode

    def start(self) -> None:
        if self._compose("up", "-d", "--wait") != 0:
            raise PlantError("docker compose up failed; see runtime/logs/docker-compose.log")

    def stop(self) -> int:
        return self._compose("stop")

    def wait_healthy(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self.health(timeout=2.0)
            if result.ok and isinstance(result.body, dict) and result.body.get("status") == "ok":
                watching = result.body.get("watching") or {}
                if watching.get("machines") and watching.get("connected") == watching.get("machines"):
                    return True
            time.sleep(2.0)
        return False


def make_adapter(settings) -> FactorySemanticsAdapter:
    """The plant adapter of the active deployment; the lightweight one unless CHAOSLAB_DEPLOYMENT=docker."""
    if settings.deployment == "docker":
        return DockerPlantAdapter(settings)
    return FactorySemanticsAdapter(settings)
