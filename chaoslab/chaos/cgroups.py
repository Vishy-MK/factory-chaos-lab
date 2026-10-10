"""cgroup v2 limits for plant components, through lab-owned user-systemd scopes.

A component the lab wants to limit runs in its own transient scope, `chaoslab-<plant>-<component>.scope`, under the
user's systemd manager (`app.slice`), which delegates that cgroup to the user. Limits are then written straight to
the scope's cgroup files; systemd only creates the scope. Writing the files directly is also how limits are lifted:
`systemctl set-property CPUQuota=` does not reset `cpu.max`.

Every path is derived from a validated unit name under this user's app.slice; nothing here takes a path from a
caller. Standard library only, so the self-limiting reset helper can use it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
UNIT_PATTERN = re.compile(r"^chaoslab-[a-z0-9]+-[a-z0-9-]+\.scope$")
NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
LIMIT_FILES = ("memory.high", "memory.max", "memory.swap.max", "cpu.max")
UNLIMITED = {"memory.high": "max", "memory.max": "max", "memory.swap.max": "max", "cpu.max": "max 100000"}
CPU_PERIOD_US = 100000


class CgroupError(Exception):
    pass


def app_slice() -> Path:
    uid = os.getuid()
    return CGROUP_ROOT / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "app.slice"


def unit_name(plant: str, component: str) -> str:
    if not (NAME_PATTERN.match(plant) and NAME_PATTERN.match(component)):
        raise CgroupError(f"invalid plant or component name: {plant!r}, {component!r}")
    return f"chaoslab-{plant}-{component}.scope"


def scope_dir(unit: str) -> Path:
    if not UNIT_PATTERN.match(unit):
        raise CgroupError(f"not a lab scope: {unit!r}")
    return app_slice() / unit


def process_unit(pid: int) -> str | None:
    """The lab scope a process runs in, or None when it is anywhere else."""
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except (OSError, ValueError):
        return None
    for line in lines:
        if line.startswith("0::"):
            path = CGROUP_ROOT / line[3:].lstrip("/")
            if path.parent == app_slice() and UNIT_PATTERN.match(path.name):
                return path.name
    return None


def available() -> tuple[bool, str]:
    """Whether this host can create lab scopes and write their limits."""
    slice_dir = app_slice()
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        return False, "cgroup v2 is not mounted at /sys/fs/cgroup"
    if not slice_dir.is_dir():
        return False, f"{slice_dir} not found: no user systemd manager"
    # systemd enables a controller for app.slice's children only while some scope asks for it, so check what it
    # can delegate (cgroup.controllers), not what is enabled right now (cgroup.subtree_control).
    controllers = (slice_dir / "cgroup.controllers").read_text().split() \
        if (slice_dir / "cgroup.controllers").is_file() else []
    missing = {"memory", "cpu"} - set(controllers)
    if missing:
        return False, f"controllers {sorted(missing)} are not available to {slice_dir}"
    try:
        out = subprocess.run(["systemd-run", "--user", "--version"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"systemd-run unavailable: {exc}"
    if out.returncode != 0:
        return False, "systemd-run --user is not usable"
    return True, f"user scopes under {slice_dir} with memory and cpu controllers"


def scope_command(unit: str, command: list[str]) -> list[str]:
    scope_dir(unit)
    # CPUWeight=100 is the default weight; asking for it makes systemd enable the cpu controller for the scope,
    # which CPUAccounting=yes no longer does. Neither property limits anything.
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "-p", "CPUWeight=100",
            "-p", "MemoryAccounting=yes", "--", *command]


def read_limits(unit: str) -> dict[str, str]:
    directory = scope_dir(unit)
    limits = {}
    for name in LIMIT_FILES:
        try:
            limits[name] = (directory / name).read_text().strip()
        except OSError:
            limits[name] = None
    return limits


def is_unlimited(limits: dict[str, str | None]) -> bool:
    return all(limits.get(name) is not None and limits[name].split()[0] == "max" for name in LIMIT_FILES)


def write_limits(unit: str, values: dict[str, str]) -> None:
    directory = scope_dir(unit)
    if not directory.is_dir():
        raise CgroupError(f"scope {unit} does not exist")
    missing = [name for name in values if not (directory / name).is_file()]
    if missing:
        raise CgroupError(f"scope {unit} has no {missing}: its controller is not enabled")
    for name in ("memory.max", "memory.high", "memory.swap.max", "cpu.max"):
        if name in values:
            (directory / name).write_text(values[name])


def limit_values(memory_high_mb: int | None, memory_max_mb: int | None, cpu_percent: int | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if memory_high_mb:
        values["memory.high"] = str(int(memory_high_mb) * 1048576)
    if memory_max_mb:
        values["memory.max"] = str(int(memory_max_mb) * 1048576)
    if memory_high_mb or memory_max_mb:
        # Without swap the limit is a RAM limit; otherwise the kernel pages out instead of reclaiming.
        values["memory.swap.max"] = "0"
    if cpu_percent:
        values["cpu.max"] = f"{int(cpu_percent) * CPU_PERIOD_US // 100} {CPU_PERIOD_US}"
    return values


def lift(unit: str, original: dict[str, str | None] | None = None) -> dict[str, str]:
    """Restore the recorded values, or unlimited. A missing scope (process gone) has nothing left to lift."""
    directory = scope_dir(unit)
    if not directory.is_dir():
        return {}
    target = {name: (original or {}).get(name) or UNLIMITED[name] for name in LIMIT_FILES}
    # Loosen memory.max first so memory.high is never left above a tighter hard limit.
    for name in ("memory.max", "memory.high", "memory.swap.max", "cpu.max"):
        if (directory / name).is_file():
            (directory / name).write_text(target[name])
    return target


def lab_units() -> list[str]:
    try:
        return sorted(p.name for p in app_slice().iterdir() if p.is_dir() and UNIT_PATTERN.match(p.name))
    except OSError:
        return []


def _keyed(path: Path) -> dict[str, int]:
    values = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                values[parts[0]] = int(parts[1])
    except OSError:
        pass
    return values


def _pressure(path: Path) -> dict | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    result = {}
    for line in lines:
        kind, *fields = line.split()
        values = dict(field.split("=", 1) for field in fields)
        result[kind] = {"avg10": float(values.get("avg10", 0)), "total_us": int(values.get("total", 0))}
    return result


def stats(unit: str) -> dict | None:
    directory = scope_dir(unit)
    if not directory.is_dir():
        return None

    def number(name):
        try:
            return int((directory / name).read_text().strip())
        except (OSError, ValueError):
            return None

    cpu = _keyed(directory / "cpu.stat")
    events = _keyed(directory / "memory.events")
    return {
        "unit": unit,
        "limits": read_limits(unit),
        "memory_current": number("memory.current"),
        "memory_peak": number("memory.peak"),
        "memory_events": {k: events.get(k, 0) for k in ("high", "max", "oom", "oom_kill")},
        "cpu_usage_us": cpu.get("usage_usec"),
        "cpu_nr_periods": cpu.get("nr_periods"),
        "cpu_nr_throttled": cpu.get("nr_throttled"),
        "cpu_throttled_us": cpu.get("throttled_usec"),
        "memory_pressure": _pressure(directory / "memory.pressure"),
        "cpu_pressure": _pressure(directory / "cpu.pressure"),
    }
