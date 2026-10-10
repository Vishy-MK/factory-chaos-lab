"""Name each MES component of a lab plant in htop and ps, without changing the MES.

The lab puts this directory on PYTHONPATH when it starts a plant, and Python imports `sitecustomize` at startup.
For an `fsmes run-*` component it sets /proc/<pid>/comm to "<plant>/<component>", nothing else: the lab finds
components by their command line and environment, and both are left exactly as they were. Every other process is
left alone. Then it hands over to the next sitecustomize on the path (Ubuntu ships one for apport).

Standard library only, and it must never raise: it runs inside the application under test.
"""

import ctypes
import os
import sys

COMPONENTS = {"run-api": "api", "run-opc-sim": "opcsim", "run-opc-agent": "opcagt",
              "run-operations": "floor", "run-log-sink": "logs"}


def _name_component():
    with open("/proc/self/cmdline", "rb") as handle:
        argv = handle.read().split(b"\0")
    plant = os.environ.get("MES_PLANT_NAME")
    if len(argv) < 3 or os.path.basename(argv[1]) != b"fsmes" or not plant:
        return
    component = COMPONENTS.get(argv[2].decode(errors="replace"))
    if component:
        ctypes.CDLL(None).prctl(15, f"{plant}/{component}".encode()[:15], 0, 0, 0)


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
    _name_component()
except Exception:
    pass
try:
    _chain()
except Exception:
    pass
