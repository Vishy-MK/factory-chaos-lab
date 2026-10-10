from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chaoslab import settings as settings_module  # noqa: E402
from chaoslab.adapters.factorysemantics import HttpResult  # noqa: E402


@pytest.fixture
def lab_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "results").mkdir()
    return tmp_path


@pytest.fixture
def settings(lab_root: Path):
    return settings_module.load(lab_root)


@dataclass
class StubPlantState:
    api_up: bool = True
    tag_id: int = 1000
    connected: int = 6


class StubPlant:
    def __init__(self, settings):
        self.settings = settings
        self.state = StubPlantState()
        self.calls: list[str] = []

    def health(self, timeout=None):
        if not self.state.api_up:
            return HttpResult(False, None, 0.01, None, "URLError")
        return HttpResult(True, 200, 0.005, {
            "status": "ok",
            "watching": {"machines": 6, "connected": self.state.connected, "disconnected": 0, "unknown": 0},
            "floor": {"last_check_recorded": "now", "seconds_since": 1.0},
        })

    def order_summary(self):
        return HttpResult(True, 200, 0.004, {"by_status": {"running": 1}, "good_qty": 10.0, "scrap_qty": 1.0})

    def authenticated_probe(self):
        return self.order_summary()

    def equipment_states(self):
        return HttpResult(True, 200, 0.004, [{"equipment": "FILL01", "state": "running"}])

    def equipment_connections(self):
        return HttpResult(True, 200, 0.004, {"machines": []})

    def orders(self):
        return HttpResult(True, 200, 0.004, [{"order": "WO-1", "good": 1, "scrap": 0}])

    def oee(self, equipment, hours=0.25):
        return HttpResult(True, 200, 0.004, {"availability": 1.0, "performance": None, "quality": 1.0,
                                             "oee": None, "coverage": 1.0})

    def downtime(self):
        return HttpResult(True, 200, 0.004, {})

    def metrics_text(self):
        self.state.tag_id += 5
        return HttpResult(True, 200, 0.004, f'mes_tag_values_max_id{{plant="bottling"}} {self.state.tag_id}\n'
                                            f'mes_audit_entries_max_id{{plant="bottling"}} 5\n')

    def process_table(self):
        return {name: {"pid": 100 + i, "alive": True, "status": "sleeping", "cpu_percent": 0.1, "rss_mb": 50.0}
                for i, name in enumerate(("api", "opc-sim", "opc-agent", "floor"))}

    def find_components(self):
        class P:
            pid = 100

        return {name: P() for name in ("api", "opc-sim", "opc-agent", "floor")}

    def database_integrity(self):
        return True, "ok"


@pytest.fixture
def stub_plant(settings):
    return StubPlant(settings)
