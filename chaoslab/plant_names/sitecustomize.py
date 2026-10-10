"""Name each MES component of a lab plant in htop and ps, without changing the MES.

The lab puts this directory on PYTHONPATH when it starts a plant, and Python imports `sitecustomize` at startup.
For an `fsmes run-*` component it sets /proc/<pid>/comm to "<plant>/<component>", nothing else: the lab finds
components by their command line and environment, and both are left exactly as they were. Every other process is
left alone. Then it hands over to the next sitecustomize on the path (Ubuntu ships one for apport).

When the lab also sets CHAOSLAB_DB_HOOK_DIR, it installs the dormant database latency hook in the components listed
in CHAOSLAB_DB_HOOK_COMPONENTS; see chaoslab_dbhook.py. That hook delays nothing until the lab activates it.

Standard library only (the database hook adds SQLAlchemy, which the MES loads anyway), and it must never raise: it runs inside the application under test.
"""

import ctypes
import os
import sys

COMPONENTS = {"run-api": "api", "run-opc-sim": "opcsim", "run-opc-agent": "opcagt",
              "run-operations": "floor", "run-log-sink": "logs"}


def _fsmes_command():
    with open("/proc/self/cmdline", "rb") as handle:
        argv = handle.read().split(b"\0")
    if len(argv) < 3 or os.path.basename(argv[1]) != b"fsmes" or not os.environ.get("MES_PLANT_NAME"):
        return None
    return argv[2].decode(errors="replace")


def _name_component(command):
    component = COMPONENTS.get(command)
    if component:
        ctypes.CDLL(None).prctl(15, f"{os.environ['MES_PLANT_NAME']}/{component}".encode()[:15], 0, 0, 0)


def _database_hook(command):
    if os.environ.get("CHAOSLAB_DB_HOOK_DIR"):
        import chaoslab_dbhook

        chaoslab_dbhook.install_from_environment(command)


def _chain():
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        if os.path.abspath(entry or ".") == here:
            continue
        candidate = os.path.join(entry or ".", "sitecustomize.py")
        if os.path.isfile(candidate):
            import importlib.util

            spec = importlib.util.spec_from_file_location("_chained_sitecustomize", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return


try:
    _command = _fsmes_command()
except Exception:
    _command = None
if _command:
    try:
        _name_component(_command)
    except Exception:
        pass
    try:
        _database_hook(_command)
    except Exception:
        pass
try:
    _chain()
except Exception:
    pass
