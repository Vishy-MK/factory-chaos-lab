"""Replace the MES's publicly known lab passwords before the plant is reachable from the LAN.

Uses the MES's own `fsmes set-password` in the plant's environment. The simulated floor's accounts (FLOOR-SIM,
FLOOR-SUP, FLOOR-PLAN, FLOOR-CREW) keep their passwords: the upstream floor simulator signs in with fixed
passwords and no override, so changing them would stop the plant's own floor.
"""

from __future__ import annotations

import os
import subprocess

#: MES account -> deployment.env variable holding its new password
ROTATED = {"ADMIN": "FSMES_ADMIN_PASSWORD", "SCOTT": "FSMES_SCOTT_PASSWORD", "AGENT": "FSMES_AGENT_PASSWORD",
           "ANALYST": "FSMES_ANALYST_PASSWORD"}
NOT_ROTATED = ("FLOOR-SIM", "FLOOR-SUP", "FLOOR-PLAN", "FLOOR-CREW")


def rotate(settings) -> dict:
    from chaoslab.adapters.factorysemantics import FactorySemanticsAdapter

    plant = FactorySemanticsAdapter(settings)
    env = plant.plant_environment()
    report = {"rotated": [], "failed": [], "not_rotated": list(NOT_ROTATED)}
    for code, variable in ROTATED.items():
        password = os.environ.get(variable) or settings.deployment_secret(variable)
        if not password or len(password) < 16:
            report["failed"].append({"account": code, "reason": f"{variable} missing or shorter than 16"})
            continue
        out = subprocess.run([str(settings.fsmes_bin), "set-password", code, password], cwd=settings.work_dir,
                             env=env, capture_output=True, text=True, timeout=60, check=False)
        if out.returncode == 0:
            report["rotated"].append(code)
        else:
            report["failed"].append({"account": code, "reason": (out.stderr or out.stdout).strip()[-300:]})
    return report
