import os
import subprocess
import sys
import threading
import time

import psutil
import pytest

from chaoslab import proctitle
from chaoslab.adapters.factorysemantics import PLANT_NAMES_DIR, with_plant_names

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process names are a Linux feature")

CHILD = """
import sys, threading, time
from chaoslab import proctitle
{body}
print("ready", flush=True)
time.sleep(30)
"""


def spawn(body: str, **kwargs) -> subprocess.Popen:
    process = subprocess.Popen([sys.executable, "-c", CHILD.format(body=body)], stdout=subprocess.PIPE, text=True,
                               **kwargs)
    assert process.stdout.readline().strip() == "ready"
    return process


def comm(pid: int) -> str:
    with open(f"/proc/{pid}/comm") as handle:
        return handle.read().strip()


def test_a_name_set_from_a_worker_thread_names_the_process():
    process = spawn("t = threading.Thread(target=proctitle.set_name, args=('cl:from-thread',)); t.start(); t.join()")
    try:
        assert comm(process.pid) == "cl:from-thread"
    finally:
        process.kill()


def test_experiment_titles_only_in_the_runner():
    process = spawn("proctitle.enable(); proctitle.experiment('run-1', 'baseline')")
    try:
        assert "cl:experiment" not in " ".join(psutil.Process(process.pid).cmdline())
    finally:
        process.kill()
    process = spawn("proctitle.enable(experiments=True); proctitle.experiment('run-1', 'fault_active', 1)")
    try:
        assert psutil.Process(process.pid).cmdline()[0] == "cl:experiment run-1 [fault_active step 1]"
        assert comm(process.pid) == "cl:experiment"
    finally:
        process.kill()


def test_titles_stay_off_until_enabled(monkeypatch):
    monkeypatch.setattr(proctitle, "_enabled", False)
    before = psutil.Process().cmdline()
    proctitle.agent("injector", "round 1")
    assert psutil.Process().cmdline() == before


def test_a_plant_component_is_named_and_keeps_its_command_line_and_environment(tmp_path):
    fake = tmp_path / "fsmes"
    fake.write_text("import time\ntime.sleep(30)\n")
    env = with_plant_names({"PATH": os.environ["PATH"], "MES_PLANT_NAME": "bottling", "PYTHONPATH": "/elsewhere"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(PLANT_NAMES_DIR), "/elsewhere"]
    assert with_plant_names(dict(env))["PYTHONPATH"] == env["PYTHONPATH"]
    process = subprocess.Popen([sys.executable, str(fake), "run-api", "--flag"], env=env)
    try:
        deadline = time.monotonic() + 10
        while comm(process.pid) != "bottling/api" and time.monotonic() < deadline:
            time.sleep(0.05)
        proc = psutil.Process(process.pid)
        assert comm(process.pid) == "bottling/api"
        assert proc.cmdline() == [sys.executable, str(fake), "run-api", "--flag"]
        assert proc.environ()["MES_PLANT_NAME"] == "bottling"
    finally:
        process.kill()


def test_other_processes_on_the_plant_path_are_left_alone(tmp_path):
    env = with_plant_names({"PATH": os.environ["PATH"], "MES_PLANT_NAME": "bottling"})
    out = subprocess.run([sys.executable, "-c", "print(open('/proc/self/comm').read().strip())"], env=env,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip().startswith("python")
    assert out.stderr == ""
