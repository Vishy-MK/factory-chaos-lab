from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from chaoslab.settings import Settings


class ScoringError(Exception):
    pass


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise ScoringError(f"cannot encode {type(value).__name__} in a plan")


class ScoringAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_plan(self, directory: Path, name: str, description: str, duration_s: int, speed: float, seed: int,
                   measure: list[str], events: list[dict]) -> Path:
        plant = self.settings.plant_name
        pack = self.settings.pack_source
        lines = [
            f"name = {_toml_value(name)}",
            f"note = {_toml_value(description)}",
            f"packs = [{_toml_value(str(pack))}]",
            f"duration = {int(duration_s)}",
            f"seed = {int(seed)}",
            f"speed = {_toml_value(float(speed))}",
            f"measure = {_toml_value(measure)}",
            "",
            f"[scenario.{plant}]",
            "events = [",
        ]
        for event in events:
            lines.append(f"  {_toml_value(event)},")
        lines.append("]")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "plan.toml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        config_dir = self.settings.upstream_dir / "config"
        env.pop("FSMES_PLANT_REGISTRY", None)
        env["MES_LINE_LAYOUT_FILE"] = str(config_dir / "line_layout.json")
        env["MES_INBOUND_MAPPING_FILE"] = str(config_dir / "inbound_mapping.json")
        env["MES_INBOUND_SQL_FILE"] = str(config_dir / "inbound_sql.json")
        return env

    def run(self, plan: Path, results_root: Path, timeout_s: float, log_path: Path,
            stop_requested=lambda: False) -> tuple[int | None, Path | None, str]:
        results_root.mkdir(parents=True, exist_ok=True)
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [str(self.settings.fsmes_bin), "lab", "run", str(plan), "--results", str(results_root)],
                cwd=self.settings.work_dir, env=self._environment(), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            deadline = time.monotonic() + timeout_s
            reason = "completed"
            while process.poll() is None:
                if time.monotonic() > deadline:
                    reason = "timeout"
                    break
                if stop_requested():
                    reason = "stopped"
                    break
                time.sleep(0.5)
            if process.poll() is None:
                self._terminate(process)
        directories = sorted(p for p in results_root.iterdir() if p.is_dir())
        run_dir = directories[-1] if directories else None
        return process.returncode, run_dir, reason

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(0.2)

    def leftover_processes(self) -> list[int]:
        found = []
        commands = {"run-api", "run-opc-sim", "run-opc-agent", "run-operations", "run-log-sink"}
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if len(cmdline) < 3 or Path(cmdline[1]).name != "fsmes" or cmdline[2] not in commands:
                    continue
                database = proc.environ().get("MES_DATABASE_URL", "")
                if str(self.settings.data_dir) in database:
                    continue
                found.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return found

    def load_scores(self, run_dir: Path) -> dict | None:
        path = run_dir / "scores.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_watched(self, run_dir: Path, plant: str) -> dict | None:
        path = run_dir / "watched" / f"{plant}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def derive(self, scores: dict, watched: dict | None, events: list[dict], duration_s: int) -> dict:
        plant = self.settings.plant_name
        entry = next((p for p in scores.get("plants", []) if p.get("plant") == plant), None)
        if entry is None:
            return {"available": False, "reason": f"plant {plant} missing from scores.json"}
        measurements = entry.get("measurements") or {}
        out: dict[str, Any] = {
            "available": True,
            "product_version": scores.get("product_version"),
            "seed": entry.get("seed"),
            "speed": entry.get("speed"),
            "duration_line_seconds": entry.get("duration_line_seconds"),
            "wall_seconds_played": entry.get("wall_seconds_played"),
            "verdict_withheld": entry.get("verdict_withheld"),
            "pipeline": _pick(entry.get("pipeline") or {}, (
                "agent_max_lag_s", "agent_max_backlog", "agent_busy_share", "agent_readings",
                "replay_max_behind_s")),
            "measurements_taken": sorted(measurements),
        }

        booking = measurements.get("booking")
        if booking:
            stations = booking.get("stations") or []
            in_band = 0
            judged = 0
            rows = []
            for row in stations:
                expected = row.get("expected_range")
                mes_good = row.get("mes_good")
                inside = None
                if expected and mes_good is not None:
                    inside = expected[0] <= mes_good <= expected[1]
                    judged += 1
                    in_band += 1 if inside else 0
                rows.append({"station": row.get("station"), "truth_good": row.get("truth_good"),
                             "mes_good": mes_good, "difference": row.get("difference"), "inside_band": inside,
                             "verdict": row.get("verdict")})
            out["booking"] = {
                "unknown_because": booking.get("unknown_because"),
                "stations_total": booking.get("stations_total"),
                "stations_answered": booking.get("stations_answered"),
                "stations_judged": judged,
                "stations_in_band": in_band if judged else None,
                "line": booking.get("line"),
                "stations": rows,
            }

        downtime = measurements.get("downtime")
        if downtime:
            breakdowns = downtime.get("breakdowns") or {}
            planned = downtime.get("planned_stops") or {}
            out["downtime"] = {
                "unknown_because": downtime.get("unknown_because"),
                "breakdowns_scripted": breakdowns.get("scripted"),
                "breakdowns_scored": breakdowns.get("scored"),
                "breakdowns_detected": _detected(breakdowns),
                "recall": breakdowns.get("recall"),
                "events": [_pick(e, ("equipment", "window_line_s", "detected", "lag_line_seconds", "lag_says",
                                     "unknown_because")) for e in breakdowns.get("events") or []],
                "planned_stops_scripted": planned.get("scripted"),
                "planned_stops_misclassified_as_downtime": planned.get("misclassified_as_downtime"),
                "total_down": downtime.get("total_down"),
            }

        oee = measurements.get("oee")
        if oee:
            rows = []
            for row in oee.get("stations") or []:
                rows.append({
                    "station": row.get("station"),
                    "truth_availability": (row.get("truth") or {}).get("availability"),
                    "mes_availability": (row.get("mes") or {}).get("availability"),
                    "truth_performance": (row.get("truth") or {}).get("performance"),
                    "mes_performance_line_clock": (row.get("mes") or {}).get("performance_line_clock"),
                    "difference": row.get("difference"),
                })
            out["oee"] = {
                "unknown_because": oee.get("unknown_because"),
                "stations_total": oee.get("stations_total"),
                "stations_answered": oee.get("stations_answered"),
                "window": oee.get("window"),
                "line_oee_reported": oee.get("line_oee_reported"),
                "stations": rows,
            }

        connection = measurements.get("connection")
        if connection:
            out["connection"] = {
                "scripted_outages": connection.get("scripted_outages"),
                "outages": [_pick(o, ("window_sim_s", "resolvable_at_this_speed", "looks_inside", "machines_seen",
                                      "machines_read_disconnected", "machines_still_claiming_a_state", "verdict"))
                            for o in connection.get("outages") or []],
                "after_the_run": connection.get("after_the_run"),
            }

        out["state_timeline"] = self.state_timeline(watched, events, duration_s)
        return out

    @staticmethod
    def state_timeline(watched: dict | None, events: list[dict], duration_s: int) -> dict:
        if not watched or not watched.get("looks"):
            return {"available": False, "reason": "no per-look station states were recorded"}
        looks = watched["looks"]
        equipment = sorted({e for look in looks for e in (look.get("states") or {})})
        summary: dict[str, Any] = {"available": True, "looks": len(looks), "equipment": equipment, "windows": []}
        last = looks[-1].get("states") or {}
        summary["final_states"] = last
        for event in events:
            start = event.get("start", event.get("at"))
            end = event.get("end", event.get("at"))
            if start is None:
                summary["windows"].append({"event": event.get("type"), "scripted": event, "note": "no fixed window"})
                continue
            inside = [look for look in looks if start <= look.get("line_second", -1) <= end]
            after = [look for look in looks if look.get("line_second", -1) > end]
            counts: dict[str, int] = {}
            for look in inside:
                for equipment_code, state in (look.get("states") or {}).items():
                    counts[state] = counts.get(state, 0) + 1
            final_after = after[-1].get("states") if after else None
            summary["windows"].append({
                "event": event.get("type"),
                "station": event.get("station"),
                "window_line_seconds": [start, end],
                "looks_inside_window": len(inside),
                "state_counts_inside_window": counts,
                "looks_after_window": len(after),
                "states_at_last_look_after_window": final_after,
            })
        return summary


def _pick(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: source.get(key) for key in keys if key in source}


def _detected(breakdowns: dict) -> int | None:
    events = breakdowns.get("events")
    if events is None or breakdowns.get("scored") in (None, 0):
        return None
    return sum(1 for e in events if e.get("detected"))
