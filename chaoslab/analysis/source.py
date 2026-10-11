"""Bounded, read-only access to the upstream MES source, so recommendations cite only code that exists."""

from __future__ import annotations

import re
from pathlib import Path

MAX_LINES = 200
MAX_RESULTS = 50


def root(settings) -> Path:
    return (Path(settings.upstream_dir) / "src" / "fsmes").resolve()


def resolve(settings, path: str) -> Path:
    """A path inside the upstream source, given as fsmes-relative or with the factorysemantics-mes/src/fsmes/ prefix."""
    base = root(settings)
    text = str(path).strip()
    for prefix in ("factorysemantics-mes/src/fsmes/", "src/fsmes/", "fsmes/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    candidate = (base / text).resolve()
    if base not in candidate.parents or not candidate.is_file() or candidate.suffix not in (".py", ".toml", ".json",
                                                                                              ".yaml", ".yml", ".md"):
        raise ValueError(f"{path!r} is not a readable file in the upstream MES source")
    return candidate


def shown(settings, path: Path) -> str:
    return "factorysemantics-mes/src/fsmes/" + str(path.relative_to(root(settings)))


def read(settings, path: str, start_line: int = 1, end_line: int = 80) -> dict:
    file = resolve(settings, path)
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), int(end_line), start + MAX_LINES - 1)
    return {"path": shown(settings, file), "start_line": start, "end_line": end, "total_lines": len(lines),
            "lines": [{"line": n, "text": lines[n - 1]} for n in range(start, end + 1)]}


def search(settings, pattern: str, path_glob: str = "**/*.py", max_results: int = 20) -> dict:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    if ".." in path_glob:
        raise ValueError("path_glob may not leave the upstream source")
    limit = max(1, min(int(max_results), MAX_RESULTS))
    matches = []
    for file in sorted(root(settings).glob(path_glob)):
        if not file.is_file() or "vendor" in file.parts:
            continue
        for number, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append({"path": shown(settings, file), "line": number, "text": line.strip()[:200]})
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


def verify_location(settings, path: str, line: int, excerpt: str) -> tuple[bool, str]:
    """True when `excerpt` appears on `line` (or within two lines of it) of the upstream file."""
    try:
        file = resolve(settings, path)
    except ValueError as exc:
        return False, str(exc)
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    if not isinstance(line, int) or not 1 <= line <= len(lines):
        return False, f"line {line} is outside {shown(settings, file)} (1..{len(lines)})"
    needle = " ".join(str(excerpt).split())
    if len(needle) < 6:
        return False, "excerpt too short to verify"
    window = range(max(1, line - 2), min(len(lines), line + 2) + 1)
    if any(needle in " ".join(lines[n - 1].split()) for n in window):
        return True, f"{shown(settings, file)}:{line}"
    return False, f"excerpt not found at {shown(settings, file)}:{line}"
