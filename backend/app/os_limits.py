"""OS-level resource injection for low-memory machines.

apply_os_limits() must run before numpy, torch or faiss are imported: BLAS and
OpenMP read their thread counts once at import. Hard memory caps belong to the
cgroup (systemd MemoryMax / scripts/run_constrained.sh); this module reads that
cgroup so load shedding sees the same limit the kernel enforces.
"""
import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

THREAD_VARIABLES = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "RAYON_NUM_THREADS")

class OSLimitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    os_threads: int | None = Field(None, ge=1, le=256)
    os_cpu_affinity: str | None = Field(None, pattern=r"^\s*\d+(-\d+)?(\s*,\s*\d+(-\d+)?)*\s*$")
    os_nice: int = Field(0, ge=0, le=19)
    os_min_available_mb: int = Field(0, ge=0)
    # Estimated peak cost of loading the embedding model (torch + weights); 0 disables the check.
    os_model_load_mb: int = Field(0, ge=0)

def parse_cpu_list(value: str) -> set[int]:
    cpus = set()
    for part in value.split(","):
        start, _, end = part.strip().partition("-")
        cpus.update(range(int(start), int(end or start) + 1))
    return cpus

def apply_os_limits(settings: OSLimitSettings | None = None) -> dict:
    settings = settings or OSLimitSettings()
    applied = {}
    if settings.os_threads:
        for name in THREAD_VARIABLES:
            os.environ[name] = str(settings.os_threads)
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        applied["threads"] = settings.os_threads
    if settings.os_cpu_affinity and hasattr(os, "sched_setaffinity"):
        cpus = parse_cpu_list(settings.os_cpu_affinity) & os.sched_getaffinity(0)
        if not cpus:
            raise ValueError("OS_CPU_AFFINITY selects no CPUs available to this process")
        os.sched_setaffinity(0, cpus)
        applied["cpu_affinity"] = sorted(cpus)
    if settings.os_nice:
        # nice() is relative; only raise the niceness up to the configured value.
        current = os.nice(0)
        if settings.os_nice > current:
            os.nice(settings.os_nice - current)
        applied["nice"] = os.nice(0)
    applied["min_available_mb"] = settings.os_min_available_mb
    return applied

def _cgroup_dir() -> Path | None:
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    except OSError:
        pass
    return None

def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return None if value == "max" else int(value)

def memory_status() -> dict:
    import psutil
    mib = 2**20
    status = {"process_rss_mb": round(psutil.Process().memory_info().rss / mib, 1), "system_available_mb": round(psutil.virtual_memory().available / mib, 1), "cgroup_limit_mb": None, "cgroup_working_set_mb": None, "cgroup_peak_mb": None, "cgroup_oom_kills": None}
    available = status["system_available_mb"]
    folder = _cgroup_dir()
    if folder:
        # The kernel caps at the tightest limit on the path, so check ancestors too.
        limits = [limit for parent in (folder, *folder.parents) if parent.is_relative_to("/sys/fs/cgroup") and (limit := _read_int(parent / "memory.max")) is not None]
        current = _read_int(folder / "memory.current")
        if current is not None:
            try:
                stats = dict(line.split() for line in (folder / "memory.stat").read_text().splitlines())
                # Inactive page cache is reclaimable; exclude it like the kubelet working set.
                current -= int(stats.get("inactive_file", 0))
            except (OSError, ValueError):
                pass
            status["cgroup_working_set_mb"] = round(current / mib, 1)
        if limits:
            status["cgroup_limit_mb"] = round(min(limits) / mib, 1)
            if current is not None:
                available = min(available, round((min(limits) - current) / mib, 1))
        if (peak := _read_int(folder / "memory.peak")) is not None:
            status["cgroup_peak_mb"] = round(peak / mib, 1)
        try:
            events = dict(line.split() for line in (folder / "memory.events").read_text().splitlines())
            status["cgroup_oom_kills"] = int(events.get("oom_kill", 0))
        except (OSError, ValueError):
            pass
    status["available_mb"] = available
    return status

def ensure_memory_for_model_load() -> None:
    """Refuse a model load the cgroup cannot hold, instead of being OOM-killed mid-load."""
    required = OSLimitSettings().os_model_load_mb
    if not required:
        return
    available = memory_status()["available_mb"]
    if available < required:
        from app.rag.errors import RAGError
        raise RAGError(f"Not enough memory to load the embedding model ({available:.0f} MB available, {required} MB needed); raise the memory limit or lower OS_MODEL_LOAD_MB", 503)

def trim_memory() -> None:
    """Return freed heap pages to the OS; glibc keeps them after large ingestions."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
