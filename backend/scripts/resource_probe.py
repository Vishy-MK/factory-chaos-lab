"""Inject OS resource limits and measure how the backend behaves under each.

    venv/bin/python scripts/resource_probe.py
    venv/bin/python scripts/resource_probe.py --profile 600M:100%:1 --profile 400M:50%:1

A profile is MEMORY_MAX:CPU_QUOTA:OS_THREADS. Each runs a fresh backend under
scripts/run_constrained.sh with its own temporary RAG data directory, ingests a
generated document with the real embedding model, then times retrievals.
"""
import argparse
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
import httpx

BACKEND = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES = ["3G:400%:4", "1G:100%:1", "600M:100%:1", "400M:100%:1"]

def make_document(paragraphs: int) -> bytes:
    topics = ["pump", "motor", "valve", "sensor", "bearing", "filter"]
    lines = [f"Section {i}: the {topics[i % len(topics)]} must be inspected every {i % 7 + 1} weeks and logged with torque, temperature and vibration readings." for i in range(paragraphs)]
    return "\n\n".join(lines).encode()

def wait_ready(client, process, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            if client.get("/health").status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(0.3)
    return False

def memory(client):
    try:
        return client.get("/health").json()["memory"]
    except (httpx.TransportError, ValueError, KeyError):
        return {}

def run_profile(profile, port, queries, paragraphs):
    memory_max, cpu_quota, threads = profile.split(":")
    result = dict(profile=profile, started=False, upload="-", ingest_s=None, retrieve_ok=0, shed=0, failed=0, p50_ms=None, p95_ms=None, idle_mb=None, peak_mb=None, oom_kills=None)
    with tempfile.TemporaryDirectory(prefix="probe_") as data_dir:
        environment = dict(os.environ, MEMORY_MAX=memory_max, CPU_QUOTA=cpu_quota, OS_THREADS=threads, PORT=str(port), RAG_DATA_DIR=data_dir, PYTHONUNBUFFERED="1")
        process = subprocess.Popen(["bash", str(BACKEND / "scripts" / "run_constrained.sh")], cwd=BACKEND, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180) as client:
                if not wait_ready(client, process, 60):
                    return result
                result["started"] = True
                result["idle_mb"] = memory(client).get("cgroup_working_set_mb")
                started = time.perf_counter()
                try:
                    upload = client.post("/api/rag/documents/upload", files={"file": ("probe.txt", make_document(paragraphs))})
                    result["upload"] = upload.status_code
                except httpx.TransportError:
                    result["upload"] = "died"
                result["ingest_s"] = round(time.perf_counter() - started, 2)
                latencies = []
                for i in range(queries):
                    started = time.perf_counter()
                    try:
                        response = client.post("/api/rag/retrieve", json={"query": f"how often is the {['pump', 'motor', 'valve'][i % 3]} inspected", "top_k": 5})
                    except httpx.TransportError:
                        result["failed"] += 1
                        time.sleep(1)
                        continue
                    if response.status_code == 200:
                        result["retrieve_ok"] += 1
                        latencies.append((time.perf_counter() - started) * 1000)
                    elif response.status_code == 503:
                        result["shed"] += 1
                    else:
                        result["failed"] += 1
                if latencies:
                    latencies.sort()
                    result["p50_ms"] = round(latencies[len(latencies) // 2], 1)
                    result["p95_ms"] = round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1)
                final = memory(client)
                result["peak_mb"], result["oom_kills"] = final.get("cgroup_peak_mb"), final.get("cgroup_oom_kills")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", action="append", help="MEMORY_MAX:CPU_QUOTA:OS_THREADS, repeatable")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--paragraphs", type=int, default=400)
    args = parser.parse_args()
    columns = ["profile", "started", "upload", "ingest_s", "retrieve_ok", "shed", "failed", "p50_ms", "p95_ms", "idle_mb", "peak_mb", "oom_kills"]
    rows = []
    for profile in args.profile or DEFAULT_PROFILES:
        print(f"running {profile} ...", flush=True)
        rows.append(run_profile(profile, args.port, args.queries, args.paragraphs))
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for row in rows:
        print("  ".join(str(row[c]).ljust(widths[c]) for c in columns))

if __name__ == "__main__":
    main()
