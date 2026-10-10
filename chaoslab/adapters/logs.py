from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from chaoslab.settings import Settings

REDACTIONS = [
    re.compile(r'("?(?:password|passwd|secret|token|api[_-]?key|authorization)"?\s*[:=]\s*)("[^"]*"|\S+)',
               re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
]
IMPORTANT_LEVELS = {"warning", "error", "critical"}


def redact(text: str) -> str:
    for pattern in REDACTIONS:
        text = pattern.sub(lambda m: f"{m.group(1)}[redacted]", text)
    return text


def parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


class LogCollector:
    def __init__(self, settings: Settings):
        self.settings = settings

    def plant_log_dir(self) -> Path:
        return self.settings.work_dir / "logs" / self.settings.plant_name

    def collect(self, since: datetime, until: datetime, destination: Path, max_info_lines: int = 150,
                max_important_lines: int = 400) -> dict:
        destination.mkdir(parents=True, exist_ok=True)
        summary: dict[str, dict] = {}
        directory = self.plant_log_dir()
        if not directory.is_dir():
            return {"available": False, "reason": f"{directory} does not exist"}
        for path in sorted(directory.glob("*.jsonl")):
            important: list[str] = []
            info: list[str] = []
            total = 0
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = parse_timestamp(str(entry.get("timestamp", "")))
                if stamp is None or stamp < since or stamp > until:
                    continue
                total += 1
                bucket = important if str(entry.get("level", "")).lower() in IMPORTANT_LEVELS else info
                bucket.append(redact(json.dumps(entry, separators=(",", ":"))))
            kept = important[:max_important_lines] + info[-max_info_lines:]
            if kept:
                (destination / path.name).write_text("\n".join(kept) + "\n", encoding="utf-8")
            summary[path.name] = {
                "lines_in_window": total,
                "important_in_window": len(important),
                "kept": len(kept),
            }
        restarted = self.settings.log_dir
        for path in sorted(restarted.glob("component-*.log")):
            if path.stat().st_mtime < since.timestamp():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
            if lines:
                (destination / path.name).write_text(redact("\n".join(lines)) + "\n", encoding="utf-8")
                summary[path.name] = {"lines_in_window": None, "kept": len(lines), "note": "console tail"}
        return {"available": True, "window": [since.isoformat(), until.isoformat()], "files": summary,
                "directory": str(destination)}

    def tail(self, component_file: str, lines: int = 50) -> list[str]:
        path = self.plant_log_dir() / component_file
        if not path.is_file():
            return []
        return [redact(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]]
