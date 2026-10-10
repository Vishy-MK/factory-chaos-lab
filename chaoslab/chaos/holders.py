from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time

HARD_LIMIT_S = 120


def _install_term_handler() -> None:
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def cpu_burn(seconds: int, percent: int, cpu: int) -> int:
    seconds = max(1, min(int(seconds), HARD_LIMIT_S))
    percent = max(1, min(int(percent), 90))
    os.sched_setaffinity(0, {cpu})
    os.nice(5)
    _install_term_handler()
    period = 0.1
    busy = period * percent / 100.0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        cycle_start = time.monotonic()
        while time.monotonic() - cycle_start < busy:
            pass
        remaining = period - (time.monotonic() - cycle_start)
        if remaining > 0:
            time.sleep(remaining)
    return 0


def sqlite_lock(db: str, seconds: int) -> int:
    seconds = max(1, min(int(seconds), HARD_LIMIT_S))
    _install_term_handler()
    connection = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        sys.stdout.write(f"FAILED {exc}\n")
        sys.stdout.flush()
        connection.close()
        return 3
    sys.stdout.write("LOCKED\n")
    sys.stdout.flush()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        connection.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chaoslab.chaos.holders")
    sub = parser.add_subparsers(dest="kind", required=True)
    burn = sub.add_parser("cpu_burn")
    burn.add_argument("--seconds", type=int, required=True)
    burn.add_argument("--percent", type=int, required=True)
    burn.add_argument("--cpu", type=int, required=True)
    lock = sub.add_parser("sqlite_lock")
    lock.add_argument("--db", required=True)
    lock.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.kind == "cpu_burn":
            return cpu_burn(args.seconds, args.percent, args.cpu)
        return sqlite_lock(args.db, args.seconds)
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
