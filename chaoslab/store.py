from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9._-]+-[0-9a-f]{6}$")

CSV_COLUMNS = [
    "experiment_id", "name", "category", "fault_type", "target", "overall_result", "fault_injected",
    "cleanup_ok", "recovered", "recovery_duration_s", "started_at", "baseline_api_success_ratio",
    "fault_api_success_ratio", "post_api_success_ratio", "baseline_api_p95_ms", "fault_api_p95_ms",
    "post_api_p95_ms", "baseline_tag_ingest_per_s", "fault_tag_ingest_per_s", "post_tag_ingest_per_s",
    "min_machines_connected_in_fault", "booking_stations_in_band", "breakdowns_detected", "application_version",
]


def new_run_id(name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{name}-{secrets.token_hex(3)}"


class ResultStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def directory(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.match(run_id):
            raise ValueError(f"invalid experiment id: {run_id!r}")
        return self.root / run_id

    def create(self, run_id: str) -> Path:
        directory = self.directory(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def write_result(self, run_id: str, result: dict) -> Path:
        directory = self.directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "result.json"
        temporary = directory / "result.json.tmp"
        temporary.write_text(json.dumps(result, indent=2, default=str, sort_keys=False), encoding="utf-8")
        temporary.replace(path)
        return path

    def read_result(self, run_id: str) -> dict:
        path = self.directory(run_id) / "result.json"
        if not path.is_file():
            raise FileNotFoundError(f"no result for {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            (p.name for p in self.root.iterdir() if p.is_dir() and RUN_ID_PATTERN.match(p.name)
             and (p / "result.json").is_file()),
            reverse=True,
        )

    def latest(self) -> str | None:
        runs = self.list_runs()
        return runs[0] if runs else None

    def resolve(self, ref: str) -> str:
        if ref in ("latest", "last"):
            latest = self.latest()
            if latest is None:
                raise FileNotFoundError("no experiment results yet")
            return latest
        runs = self.list_runs()
        if ref in runs:
            return ref
        matches = [r for r in runs if ref in r]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"no experiment matches {ref!r}")
        raise FileNotFoundError(f"{ref!r} is ambiguous: {matches[:5]}")

    def to_csv_rows(self) -> list[dict]:
        rows = []
        for run_id in reversed(self.list_runs()):
            rows.append(flatten(self.read_result(run_id)))
        return rows

    def export_csv(self, destination: Path) -> int:
        rows = self.to_csv_rows()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("unavailable" if row.get(k) is None else row.get(k)) for k in CSV_COLUMNS})
        return len(rows)

    def csv_text(self) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in self.to_csv_rows():
            writer.writerow({k: ("unavailable" if row.get(k) is None else row.get(k)) for k in CSV_COLUMNS})
        return buffer.getvalue()


def _phase(result: dict, phase: str) -> dict:
    return (result.get("observed_metrics") or {}).get(phase) or {}


def flatten(result: dict) -> dict:
    definition = result.get("definition") or {}
    steps = definition.get("steps") or [{}]
    first = steps[0] if steps else {}
    fault = (first.get("fault") or {}).get("type")
    target = first.get("target") or {}
    baseline, during, post = _phase(result, "baseline"), _phase(result, "fault"), _phase(result, "post")
    recovery = result.get("recovery") or {}
    cleanup = result.get("cleanup") or {}
    scoring = result.get("scoring") or {}
    booking = scoring.get("booking") or {}
    downtime = scoring.get("downtime") or {}

    def p95(phase: dict):
        return (phase.get("api_latency_ms") or {}).get("p95")

    return {
        "experiment_id": result.get("experiment_id"),
        "name": definition.get("name"),
        "category": definition.get("category"),
        "fault_type": fault,
        "target": ",".join(f"{k}={v}" for k, v in target.items()),
        "overall_result": result.get("overall_result"),
        "fault_injected": (result.get("fault_injection") or {}).get("injected"),
        "cleanup_ok": cleanup.get("ok"),
        "recovered": recovery.get("recovered"),
        "recovery_duration_s": recovery.get("duration_s"),
        "started_at": result.get("started_at"),
        "baseline_api_success_ratio": baseline.get("api_success_ratio"),
        "fault_api_success_ratio": during.get("api_success_ratio"),
        "post_api_success_ratio": post.get("api_success_ratio"),
        "baseline_api_p95_ms": p95(baseline),
        "fault_api_p95_ms": p95(during),
        "post_api_p95_ms": p95(post),
        "baseline_tag_ingest_per_s": baseline.get("tag_ingest_per_second"),
        "fault_tag_ingest_per_s": during.get("tag_ingest_per_second"),
        "post_tag_ingest_per_s": post.get("tag_ingest_per_second"),
        "min_machines_connected_in_fault": during.get("machines_connected_min"),
        "booking_stations_in_band": booking.get("stations_in_band"),
        "breakdowns_detected": downtime.get("breakdowns_detected"),
        "application_version": (result.get("application") or {}).get("version"),
    }


def compare(first: dict, second: dict) -> dict:
    a, b = flatten(first), flatten(second)
    numeric = [k for k in CSV_COLUMNS if isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float))
               and not isinstance(a.get(k), bool)]
    deltas = {k: round(b[k] - a[k], 4) for k in numeric}
    rows = []
    for key in CSV_COLUMNS:
        rows.append({"metric": key, "first": a.get(key), "second": b.get(key), "delta": deltas.get(key)})
    return {
        "first": a.get("experiment_id"),
        "second": b.get("experiment_id"),
        "rows": rows,
        "note": "delta is second minus first; null means unavailable in at least one run",
    }
