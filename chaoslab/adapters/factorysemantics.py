from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from chaoslab.settings import ConfigError, Settings


@dataclass
class HttpResult:
    ok: bool
    status: int | None
    latency_s: float
    body: Any
    error: str | None = None


class PlantError(Exception):
    pass


class FactorySemanticsAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.name = settings.plant_name
        self._token: str | None = None
        self._components = settings.allowlist["plants"][self.name]["components"]

    def _request(self, path: str, method: str = "GET", payload: dict | None = None,
                 auth: bool = False, timeout: float | None = None, raw: bool = False) -> HttpResult:
        timeout = timeout or float(self.settings.lab["sampling"]["request_timeout_s"])
        url = f"{self.settings.api_url}{path}"
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            token = self._token or self._login(timeout)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                latency = time.monotonic() - started
                if raw:
                    return HttpResult(True, response.status, latency, content.decode("utf-8", "replace"))
                return HttpResult(True, response.status, latency, json.loads(content) if content else None)
        except urllib.error.HTTPError as exc:
            latency = time.monotonic() - started
            if exc.code == 401 and auth and self._token:
                self._token = None
            return HttpResult(False, exc.code, latency, None, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            return HttpResult(False, None, time.monotonic() - started, None, type(exc).__name__)

    def _login(self, timeout: float) -> str | None:
        code, password = self.settings.reader_credentials
        url = f"{self.settings.api_url}/auth/login"
        body = json.dumps({"code": code, "password": password}).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                self._token = json.loads(response.read())["token"]
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError):
            self._token = None
        return self._token

    def health(self, timeout: float | None = None) -> HttpResult:
        return self._request("/health", timeout=timeout)

    def metrics_text(self) -> HttpResult:
        return self._request("/metrics", raw=True)

    def authenticated_probe(self) -> HttpResult:
        return self._request("/workorders/summary", auth=True)

    def equipment_states(self) -> HttpResult:
        return self._request("/equipment/states", auth=True)

    def equipment_connections(self) -> HttpResult:
        return self._request("/equipment/connections", auth=True)

    def order_summary(self) -> HttpResult:
        return self._request("/workorders/summary", auth=True)

    def orders(self) -> HttpResult:
        return self._request("/kpis/orders", auth=True)

    def services(self) -> HttpResult:
        return self._request("/ops/services", auth=True)

    def oee(self, equipment: str, hours: float = 0.25) -> HttpResult:
        return self._request(f"/kpis/oee/{equipment}?hours={hours}", auth=True, timeout=8.0)

    def downtime(self) -> HttpResult:
        return self._request("/analysis/downtime", auth=True, timeout=8.0)

    def component_command(self, component: str) -> list[str]:
        spec = self._components[component]
        return [str(self.settings.fsmes_bin), spec["command"], *spec.get("arguments", [])]

    def find_components(self) -> dict[str, psutil.Process]:
        found: dict[str, psutil.Process] = {}
        commands = {spec["command"]: name for name, spec in self._components.items()}
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if len(cmdline) < 3 or Path(cmdline[1]).name != "fsmes":
                    continue
                name = commands.get(cmdline[2])
                if not name:
                    continue
                if not self._belongs_to_plant(proc):
                    continue
                found[name] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return found

    def _belongs_to_plant(self, proc: psutil.Process) -> bool:
        try:
            environ = proc.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return False
        return environ.get("MES_PLANT_NAME") == self.name

    def log_sink_pids(self) -> list[int]:
        pids = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if len(cmdline) >= 3 and Path(cmdline[1]).name == "fsmes" and cmdline[2] == "run-log-sink" \
                        and self._belongs_to_plant(proc):
                    pids.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return pids

    def process_table(self) -> dict[str, dict]:
        table: dict[str, dict] = {}
        for name in self._components:
            table[name] = {"pid": None, "alive": False, "status": "missing", "cpu_percent": None, "rss_mb": None}
        for name, proc in self.find_components().items():
            try:
                with proc.oneshot():
                    table[name] = {
                        "pid": proc.pid,
                        "alive": proc.status() != psutil.STATUS_ZOMBIE,
                        "status": proc.status(),
                        "cpu_percent": proc.cpu_percent(interval=None),
                        "rss_mb": round(proc.memory_info().rss / 1048576, 1),
                    }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return table

    def plant_environment(self) -> dict[str, str]:
        os.environ["FSMES_PLANT_REGISTRY"] = str(self.settings.fleet_file)
        try:
            from fsmes import plant as plants_mod
            from fsmes.pack import fleet
        except ImportError as exc:
            raise ConfigError(f"the fsmes package is not importable ({exc}); run scripts/setup.sh") from exc
        work = self.settings.work_dir
        registry = fleet.load(work)
        if self.name not in registry:
            raise ConfigError(f"plant {self.name!r} is not in {self.settings.fleet_file}")
        speed = float(self.settings.lab["plant"]["sim_speed"])
        env = plants_mod.plant_env(self.name, registry[self.name], work, speed)
        env.update(self.upstream_config_overrides())
        return env

    def upstream_config_overrides(self) -> dict[str, str]:
        config_dir = self.settings.upstream_dir / "config"
        return {
            "MES_LINE_LAYOUT_FILE": str(config_dir / "line_layout.json"),
            "MES_INBOUND_MAPPING_FILE": str(config_dir / "inbound_mapping.json"),
            "MES_INBOUND_SQL_FILE": str(config_dir / "inbound_sql.json"),
        }

    def _fsmes_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["FSMES_PLANT_REGISTRY"] = str(self.settings.fleet_file)
        env.update(self.upstream_config_overrides())
        return env

    def _run_fsmes(self, arguments: list[str], log_name: str, timeout: float) -> int:
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.settings.log_dir / log_name
        with log_path.open("ab") as log:
            result = subprocess.run(
                [str(self.settings.fsmes_bin), *arguments], cwd=self.settings.work_dir, env=self._fsmes_env(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False,
            )
        return result.returncode

    def initialise(self) -> int:
        return self._run_fsmes(["plant", self.name, "init"], "plant-init.log", 180)

    def start(self) -> None:
        if self.find_components():
            raise PlantError(f"plant {self.name} already has running components; stop it first")
        speed = str(self.settings.lab["plant"]["sim_speed"])
        code = self._run_fsmes(["plant", self.name, "start", "--speed", speed], "plant-start.log", 60)
        if code != 0:
            raise PlantError(f"`fsmes plant {self.name} start` exited {code}; see {self.settings.log_dir}/plant-start.log")

    def wait_healthy(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self.health(timeout=2.0)
            if result.ok and isinstance(result.body, dict) and result.body.get("status") == "ok":
                watching = result.body.get("watching") or {}
                if watching.get("machines") and watching.get("connected") == watching.get("machines"):
                    return True
            time.sleep(1.0)
        return False

    def stop(self) -> int:
        code = self._run_fsmes(["plant", self.name, "stop"], "plant-stop.log", 60)
        deadline = time.monotonic() + float(self.settings.lab["plant"]["stop_timeout_s"])
        while time.monotonic() < deadline and (self.find_components() or self.log_sink_pids()):
            time.sleep(0.5)
        for proc in list(self.find_components().values()):
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        return code

    def pid_file(self) -> Path:
        return self.settings.data_dir / f"{self.name}.pids"

    def spawn_component(self, component: str) -> int:
        if component not in self._components:
            raise PlantError(f"component {component!r} is not in the allowlist")
        if component in self.find_components():
            return self.find_components()[component].pid
        env = self.plant_environment()
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.settings.log_dir / f"component-{component}.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                self.component_command(component), cwd=self.settings.work_dir, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        self._register_pid(process.pid, replacing=component)
        return process.pid

    def _register_pid(self, pid: int, replacing: str) -> None:
        path = self.pid_file()
        if not path.is_file():
            return
        existing = [int(x) for x in path.read_text(encoding="utf-8").split() if x.strip().isdigit()]
        alive = [p for p in existing if psutil.pid_exists(p)]
        alive.append(pid)
        path.write_text("\n".join(str(p) for p in alive), encoding="utf-8")

    def signal_component(self, component: str, sig: int) -> int | None:
        proc = self.find_components().get(component)
        if proc is None:
            return None
        os.kill(proc.pid, sig)
        return proc.pid

    def kill_component(self, component: str, sig: int = signal.SIGKILL, wait_s: float = 10.0) -> int | None:
        proc = self.find_components().get(component)
        if proc is None:
            return None
        pid = proc.pid
        os.kill(pid, sig)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return pid
            time.sleep(0.1)
        raise PlantError(f"component {component} (pid {pid}) did not exit within {wait_s}s")

    def database_integrity(self) -> tuple[bool | None, str]:
        import sqlite3

        path = self.settings.database_file
        if not path.is_file():
            return None, f"database file {path} not found"
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return None, f"could not check: {exc}"
        verdict = row[0] if row else "no result"
        return verdict == "ok", str(verdict)


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            head, value = line.rsplit(" ", 1)
            number = float(value)
        except ValueError:
            continue
        labels: dict[str, str] = {}
        if "{" in head:
            name, rest = head.split("{", 1)
            body = rest.rsplit("}", 1)[0]
            for part in body.split('",'):
                if "=" not in part:
                    continue
                key, val = part.split("=", 1)
                labels[key.strip()] = val.strip().strip('"')
        else:
            name = head
        samples.append((name, labels, number))
    return samples
