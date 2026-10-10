import json
import os
import signal
import subprocess
import sys
import time

import psutil

from chaoslab.audit import Lease
from chaoslab.chaos import watchdog
from chaoslab.settings import PACKAGE_ROOT


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_watchdog_cleans_up_when_the_controller_is_gone(lab_root, settings):
    victim = subprocess.Popen(["sleep", "60"])
    try:
        os.kill(victim.pid, signal.SIGSTOP)
        time.sleep(0.2)
        lease = Lease(settings.lease_file, settings.stop_flag)
        assert lease.acquire("20261010T000000Z-orphan-aaaaaa", "orphan", time.time() + 600)
        lease.update(controller_pid=_dead_pid(), state="fault_active",
                     records=[{"type": "process_freeze", "component": "api", "pid": victim.pid}])
        code = watchdog.run(lab_root, poll_s=0.1)
        assert code == 0
        assert psutil.Process(victim.pid).status() != psutil.STATUS_STOPPED
        current = json.loads(settings.lease_file.read_text())
        assert current["state"] == "released"
        assert current["cleaned_by"] == "watchdog"
        events = [json.loads(line)["event"] for line in settings.audit_file.read_text().splitlines()]
        assert "watchdog_cleanup_done" in events
    finally:
        victim.kill()
        victim.wait()


def test_watchdog_cleans_up_when_the_deadline_passes(lab_root, settings):
    victim = subprocess.Popen(["sleep", "60"])
    try:
        os.kill(victim.pid, signal.SIGSTOP)
        time.sleep(0.2)
        lease = Lease(settings.lease_file, settings.stop_flag)
        assert lease.acquire("20261010T000000Z-late-bbbbbb", "late", time.time() - 1)
        lease.update(state="fault_active",
                     records=[{"type": "process_freeze", "component": "api", "pid": victim.pid}])
        assert watchdog.run(lab_root, poll_s=0.1) == 0
        assert psutil.Process(victim.pid).status() != psutil.STATUS_STOPPED
    finally:
        victim.kill()
        victim.wait()


def test_watchdog_exits_quietly_when_the_lease_is_released(lab_root, settings):
    lease = Lease(settings.lease_file, settings.stop_flag)
    lease.acquire("20261010T000000Z-fine-cccccc", "fine", time.time() + 600)
    lease.release()
    assert watchdog.run(lab_root, poll_s=0.1) == 0


def test_watchdog_runs_as_an_independent_process(lab_root, settings):
    lease = Lease(settings.lease_file, settings.stop_flag)
    lease.acquire("20261010T000000Z-proc-dddddd", "proc", time.time() + 600)
    pid = watchdog.spawn(lab_root)
    time.sleep(0.5)
    assert psutil.pid_exists(pid)
    assert os.getsid(pid) != os.getsid(0)
    lease.release()
    deadline = time.time() + 10
    while time.time() < deadline and psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie":
        time.sleep(0.2)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == "zombie"


def test_lease_blocks_a_second_holder_but_not_a_dead_one(settings):
    lease = Lease(settings.lease_file, settings.stop_flag)
    assert lease.acquire("20261010T000000Z-one-eeeeee", "one", time.time() + 60)
    assert not lease.acquire("20261010T000000Z-two-ffffff", "two", time.time() + 60)
    lease.update(controller_pid=_dead_pid())
    assert lease.acquire("20261010T000000Z-two-ffffff", "two", time.time() + 60)


def test_watchdog_bound_to_an_experiment_exits_when_the_lease_changes_hands(lab_root, settings):
    lease = Lease(settings.lease_file, settings.stop_flag)
    lease.acquire("20261010T000000Z-new-aaaaaa", "new", time.time() + 600)
    assert watchdog.run(lab_root, poll_s=0.1, experiment_id="20261010T000000Z-old-bbbbbb") == 0
    current = json.loads(settings.lease_file.read_text())
    assert current["experiment_id"] == "20261010T000000Z-new-aaaaaa"
    assert current["state"] != "released"
