from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CHAOSLAB_ROOT") or PACKAGE_ROOT)


class ConfigError(Exception):
    pass


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _require(table: dict, dotted: str, path: Path):
    node = table
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"{path}: missing key {dotted}")
        node = node[part]
    return node


@dataclass(frozen=True)
class Settings:
    root: Path
    lab: dict
    policy: dict
    allowlist: dict

    def path(self, relative: str) -> Path:
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.root / candidate

    @cached_property
    def upstream_dir(self) -> Path:
        return self.path(self.lab["upstream"]["path"])

    @cached_property
    def pack_source(self) -> Path:
        return self.upstream_dir / self.lab["upstream"]["pack_source"]

    @cached_property
    def runtime_dir(self) -> Path:
        return self.path(self.lab["runtime"]["dir"])

    @cached_property
    def fleet_dir(self) -> Path:
        return self.path(self.lab["runtime"]["fleet_dir"])

    @cached_property
    def fleet_file(self) -> Path:
        return self.fleet_dir / "fleet.toml"

    @cached_property
    def data_dir(self) -> Path:
        return self.path(self.lab["runtime"]["data_dir"])

    @cached_property
    def work_dir(self) -> Path:
        return self.path(self.lab["runtime"]["work_dir"])

    @cached_property
    def state_dir(self) -> Path:
        return self.path(self.lab["runtime"]["state_dir"])

    @cached_property
    def log_dir(self) -> Path:
        return self.path(self.lab["runtime"]["log_dir"])

    @cached_property
    def results_dir(self) -> Path:
        return self.path(self.lab["results"]["dir"])

    @cached_property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @cached_property
    def lease_file(self) -> Path:
        return self.state_dir / "active.json"

    @cached_property
    def db_hook_dir(self) -> Path:
        """Activation and per-process counters of the database latency hook (chaoslab_dbhook)."""
        return self.state_dir / "db-latency"

    @cached_property
    def stop_flag(self) -> Path:
        return self.state_dir / "emergency-stop"

    @cached_property
    def deployment(self) -> str:
        """`lightweight` (default: fsmes plant on SQLite) or `docker` (the lab compose stack), from
        CHAOSLAB_DEPLOYMENT. Everything else follows from it; unset, nothing changes."""
        value = (os.environ.get("CHAOSLAB_DEPLOYMENT") or "lightweight").strip().lower()
        if value not in ("lightweight", "docker"):
            raise ConfigError(f"CHAOSLAB_DEPLOYMENT must be lightweight or docker, not {value!r}")
        if value == "docker" and "docker" not in self.lab:
            raise ConfigError("config/lab.yaml has no docker section")
        return value

    @cached_property
    def plant_name(self) -> str:
        if self.deployment == "docker":
            return self.lab["docker"]["plant"]
        return self.lab["plant"]["name"]

    @cached_property
    def pack_dir(self) -> Path:
        return self.fleet_dir / "packs" / self.plant_name

    @cached_property
    def pack_settings(self) -> dict:
        plant_toml = self.pack_dir / "plant.toml"
        if not plant_toml.is_file():
            raise ConfigError(f"the lab pack is not prepared yet: {plant_toml} is missing (run scripts/setup.sh)")
        with plant_toml.open("rb") as handle:
            return tomllib.load(handle)

    @cached_property
    def api_host(self) -> str:
        if self.deployment == "docker":
            return "127.0.0.1"
        return self.pack_settings.get("serve", {}).get("api_host", "127.0.0.1")

    @cached_property
    def api_port(self) -> int:
        if self.deployment == "docker":
            return int(self.lab["docker"]["api_port"])
        return int(self.pack_settings["serve"]["api_port"])

    @cached_property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"

    @cached_property
    def opc_endpoint(self) -> str:
        return str(self.pack_settings["serve"]["opc_endpoint"])

    @cached_property
    def database_file(self) -> Path:
        return self.data_dir / f"{self.plant_name}.db"

    @cached_property
    def fsmes_bin(self) -> Path:
        candidate = Path(os.environ.get("CHAOSLAB_VENV") or self.root / ".venv") / "bin" / "fsmes"
        if candidate.exists():
            return candidate
        import shutil

        found = shutil.which("fsmes")
        if not found:
            raise ConfigError("the fsmes executable was not found; run scripts/setup.sh first")
        return Path(found)

    @cached_property
    def deployment_env_file(self) -> Path:
        """Generated secrets of a LAN deployment (scripts/deploy-lan.sh), readable by this user only."""
        return self.state_dir / "deployment.env"

    def deployment_secret(self, name: str) -> str | None:
        try:
            for line in self.deployment_env_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip("'\"")
        except OSError:
            return None
        return None

    @cached_property
    def reader_credentials(self) -> tuple[str, str]:
        reader = self.lab["reader"]
        # A deployment rotates the reader's password; the lab follows it even when its env was not sourced.
        password = os.environ.get(reader["password_env"]) or self.deployment_secret(reader["password_env"]) \
            or reader["password_default"]
        return reader["code"], password

    @cached_property
    def limits(self) -> dict:
        return self.policy["limits"]

    def ensure_dirs(self) -> None:
        for directory in (self.runtime_dir, self.fleet_dir, self.data_dir, self.work_dir, self.state_dir,
                          self.log_dir, self.results_dir):
            directory.mkdir(parents=True, exist_ok=True)


def load(root: Path | None = None) -> Settings:
    base = Path(root) if root else ROOT
    config_dir = base / "config"
    lab = _read_yaml(config_dir / "lab.yaml")
    policy = _read_yaml(config_dir / "safety-policy.yaml")
    allowlist = _read_yaml(config_dir / "service-allowlist.yaml")
    for dotted in ("upstream.path", "upstream.pack_source", "runtime.dir", "plant.name", "results.dir"):
        _require(lab, dotted, config_dir / "lab.yaml")
    for dotted in ("limits.max_fault_duration_s", "limits.max_simultaneous_faults", "limits.recovery_timeout_s",
                   "resources.min_available_memory_mb"):
        _require(policy, dotted, config_dir / "safety-policy.yaml")
    for dotted in ("hosts", "plants", "fault_targets"):
        _require(allowlist, dotted, config_dir / "service-allowlist.yaml")
    return Settings(root=base, lab=lab, policy=policy, allowlist=allowlist)
