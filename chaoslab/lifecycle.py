from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from chaoslab import resources
from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter, PlantError
from chaoslab.adapters.docker_plant import make_adapter
from chaoslab.settings import PACKAGE_ROOT, ConfigError, Settings


class Lifecycle:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.plant = make_adapter(settings)

    def prerequisites(self) -> list[dict]:
        checks = []

        def add(name: str, ok: bool, detail: str, required: bool = True):
            checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

        add("python>=3.12", sys.version_info >= (3, 12), sys.version.split()[0])
        add("git", shutil.which("git") is not None, shutil.which("git") or "not found", required=False)
        upstream = self.settings.upstream_dir
        add("upstream checkout", (upstream / "pyproject.toml").is_file(), str(upstream))
        add("upstream LICENSE preserved", (upstream / "LICENSE").is_file() and (upstream / "NOTICE").is_file(),
            "LICENSE and NOTICE present" if (upstream / "LICENSE").is_file() else "missing")
        try:
            binary = self.settings.fsmes_bin
            add("fsmes executable", True, str(binary))
        except ConfigError as exc:
            add("fsmes executable", False, str(exc))
        pack_ok = (self.settings.pack_dir / "plant.toml").is_file()
        add("lab pack prepared", pack_ok, str(self.settings.pack_dir) if pack_ok else "run scripts/setup.sh")
        add("lab replay data generated", (self.settings.pack_dir / "out" / "tags.json").is_file(),
            str(self.settings.pack_dir / "out"))
        snapshot = resources.host_snapshot()
        floor = float(self.settings.policy["resources"]["min_available_memory_mb"])
        add("available memory", snapshot.available_mb >= floor,
            f"{snapshot.available_mb:.0f} MB available, threshold {floor:.0f} MB")
        add("docker", shutil.which("docker") is not None,
            shutil.which("docker") or "not installed (not required for lightweight mode)", required=False)
        for port, label in ((self.settings.api_port if pack_ok else 8010, "plant API port"),):
            busy = self._port_in_use(port)
            plant_here = bool(self.plant.find_components())
            add(label, (not busy) or plant_here, f"{port} {'in use' if busy else 'free'}"
                f"{' by the lab plant' if busy and plant_here else ''}")
        return checks

    @staticmethod
    def _port_in_use(port: int) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def prepare(self, force: bool = False) -> list[str]:
        settings = self.settings
        settings.ensure_dirs()
        actions: list[str] = []
        source = settings.pack_source
        if not (source / "plant.toml").is_file():
            raise ConfigError(f"upstream pack not found at {source}")
        pack = settings.pack_dir
        if force and pack.exists():
            shutil.rmtree(pack)
        if not pack.exists():
            pack.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, pack, ignore=shutil.ignore_patterns("out", "__pycache__"))
            actions.append(f"copied pack {source} -> {pack}")
        out = pack / "out"
        if force or not (out / "tags.json").is_file():
            code = subprocess.run(
                [str(settings.fsmes_bin), "sim-generate", str(pack / "line.json"), "--out", str(out)],
                cwd=settings.work_dir, capture_output=True, text=True, timeout=300, check=False)
            if code.returncode != 0:
                raise ConfigError(f"sim-generate failed: {code.stderr.strip() or code.stdout.strip()}")
            actions.append(f"generated replay data in {out}")
        fleet_file = settings.fleet_file
        content = f'packs = ["packs/{settings.plant_name}"]\n\n[environment]\ndata_dir = "{settings.data_dir}"\n'
        if not fleet_file.is_file() or fleet_file.read_text(encoding="utf-8") != content:
            fleet_file.write_text(content, encoding="utf-8")
            actions.append(f"wrote fleet file {fleet_file}")
        if not settings.database_file.is_file():
            code = self.plant.initialise()
            if code != 0:
                raise ConfigError(f"`fsmes plant {settings.plant_name} init` exited {code}; see "
                                  f"{settings.log_dir}/plant-init.log")
            actions.append(f"initialised plant database {settings.database_file}")
        return actions

    def start(self, with_exporter: bool = False) -> dict:
        gate = resources.ResourceGate(self.settings.policy["resources"])
        allowed, reasons, snapshot = gate.check()
        if not allowed:
            raise PlantError("refusing to start: " + "; ".join(reasons))
        if not self.settings.database_file.is_file():
            raise PlantError("the plant is not initialised; run scripts/setup.sh first")
        if self.plant.find_components():
            raise PlantError("the lab plant already has running components; use scripts/status.sh")
        started = time.monotonic()
        self.plant.start()
        healthy = self.plant.wait_healthy(float(self.settings.lab["plant"]["start_timeout_s"]))
        report = {"healthy": healthy, "seconds": round(time.monotonic() - started, 1),
                  "host": snapshot.as_dict()}
        if not healthy:
            raise PlantError(f"the plant did not become healthy within "
                             f"{self.settings.lab['plant']['start_timeout_s']}s; see "
                             f"{self.settings.work_dir}/logs/{self.settings.plant_name}/plant.log")
        if with_exporter:
            report["exporter_pid"] = self.start_exporter()
        return report

    def exporter_pid_file(self) -> Path:
        return self.settings.state_dir / "exporter.pid"

    def exporter_pid(self) -> int | None:
        path = self.exporter_pid_file()
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return pid if psutil.pid_exists(pid) else None

    def start_exporter(self) -> int:
        existing = self.exporter_pid()
        if existing:
            return existing
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.settings.log_dir / "exporter.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "chaoslab", "--root", str(self.settings.root), "exporter"],
                cwd=str(PACKAGE_ROOT),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.exporter_pid_file().write_text(str(process.pid), encoding="utf-8")
        return process.pid

    def stop_exporter(self) -> bool:
        pid = self.exporter_pid()
        if pid is None:
            self.exporter_pid_file().unlink(missing_ok=True)
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.exporter_pid_file().unlink(missing_ok=True)
        return True

    def stop(self) -> dict:
        exporter = self.stop_exporter()
        code = self.plant.stop()
        remaining = self.plant.find_components()
        return {"exporter_stopped": exporter, "exit_code": code, "remaining_components": sorted(remaining)}

    def status(self) -> dict:
        health = self.plant.health(timeout=2.0)
        table = self.plant.process_table()
        host = resources.host_snapshot()
        body = health.body if health.ok and isinstance(health.body, dict) else {}
        return {
            "plant": self.settings.plant_name,
            "api_url": self.settings.api_url,
            "api_up": bool(health.ok and body.get("status") == "ok"),
            "api_latency_ms": round(health.latency_s * 1000, 1) if health.ok else None,
            "machines": body.get("watching"),
            "floor": body.get("floor"),
            "components": table,
            "plant_rss_mb_total": round(sum(c["rss_mb"] or 0 for c in table.values()), 1),
            "host": host.as_dict(),
            "exporter_pid": self.exporter_pid(),
        }


def is_healthy(status: dict) -> bool:
    machines = status["machines"] or {}
    return bool(status["api_up"] and machines.get("machines")
                and machines.get("connected") == machines.get("machines")
                and all(c["alive"] for c in status["components"].values()))
