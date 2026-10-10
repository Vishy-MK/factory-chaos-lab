import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
OUT = Path(__file__).resolve().parent / "dashboards"


def target(expr, legend="", ref="A"):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}


class Board:
    def __init__(self, uid, title, tags):
        self.uid, self.title, self.tags = uid, title, tags
        self.panels = []
        self.next_id = 1
        self.x = 0
        self.y = 0
        self.row_height = 0

    def _place(self, width, height):
        if self.x + width > 24:
            self.x = 0
            self.y += self.row_height
            self.row_height = 0
        position = {"x": self.x, "y": self.y, "w": width, "h": height}
        self.x += width
        self.row_height = max(self.row_height, height)
        return position

    def _add(self, panel, width, height):
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = self._place(width, height)
        panel["datasource"] = DS
        self.panels.append(panel)

    def stat(self, title, targets, width=4, mappings=None, unit="none", description="", decimals=None):
        defaults = {"unit": unit, "mappings": mappings or []}
        if decimals is not None:
            defaults["decimals"] = decimals
        self._add({
            "type": "stat", "title": title, "description": description, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        }, width, 4)

    def timeseries(self, title, targets, width=12, unit="none", description="", minimum=None):
        defaults = {"unit": unit, "custom": {"lineWidth": 1, "fillOpacity": 8, "spanNulls": False}}
        if minimum is not None:
            defaults["min"] = minimum
        self._add({
            "type": "timeseries", "title": title, "description": description, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }, width, 8)

    def state_timeline(self, title, targets, mappings, width=24, description=""):
        self._add({
            "type": "state-timeline", "title": title, "description": description, "targets": targets,
            "fieldConfig": {"defaults": {"mappings": mappings, "custom": {"fillOpacity": 70}}, "overrides": []},
            "options": {"showValue": "auto", "mergeValues": True, "legend": {"displayMode": "list"}},
        }, width, 7)

    def table(self, title, targets, width=12, description=""):
        self._add({
            "type": "table", "title": title, "description": description, "targets": [
                {**t, "instant": True, "format": "table"} for t in targets],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showHeader": True},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, "lab": True}}}],
        }, width, 7)

    def text(self, title, content, width=12):
        self._add({"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}, width, 6)

    def build(self, annotate_experiments=True):
        annotations = [{
            "name": "Experiment active", "datasource": DS, "enable": annotate_experiments,
            "expr": "chaoslab_experiment_active == 1", "iconColor": "orange", "step": "5s",
            "titleFormat": "experiment", "useValueForTime": False,
        }]
        return {
            "uid": self.uid, "title": self.title, "tags": self.tags, "schemaVersion": 39, "version": 1,
            "editable": False, "refresh": "10s", "time": {"from": "now-30m", "to": "now"},
            "timezone": "browser", "annotations": {"list": annotations}, "panels": self.panels,
            "templating": {"list": []},
        }

    def write(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(json.dumps(self.build(), indent=2) + "\n", encoding="utf-8")


UP_MAP = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}]
YESNO = [{"type": "value", "options": {
    "0": {"text": "no", "color": "red"}, "1": {"text": "yes", "color": "green"}}}]
ACTIVE = [{"type": "value", "options": {
    "0": {"text": "idle", "color": "green"}, "1": {"text": "ACTIVE", "color": "orange"}}}]
STATE = [{"type": "value", "options": {
    "0": {"text": "stopped", "color": "dark-red"}, "1": {"text": "running", "color": "green"},
    "2": {"text": "starved", "color": "yellow"}, "3": {"text": "blocked", "color": "orange"},
    "4": {"text": "down", "color": "red"}, "5": {"text": "changeover", "color": "blue"}}}]

overview = Board("chaoslab-overview", "Lab overview", ["chaoslab"])
overview.stat("Plant API", [target("chaoslab_plant_up", "{{plant}}")], mappings=UP_MAP)
overview.stat("Machines connected", [target("chaoslab_machines_connected", "connected")], decimals=0)
overview.stat("Experiment", [target("chaoslab_experiment_active", "state")], mappings=ACTIVE)
overview.stat("Last experiment recovered", [target("chaoslab_last_experiment_recovered", "{{name}}")],
              mappings=YESNO, width=6)
overview.stat("Last recovery duration", [target("chaoslab_last_experiment_recovery_seconds", "{{name}}")],
              unit="s", width=6, description="Unavailable when the last experiment defines no wall-clock recovery.")
overview.timeseries("API /health latency", [target("chaoslab_api_health_latency_seconds", "{{plant}}")], unit="s",
                    description="Absent while the API does not answer; absence is not zero.")
overview.timeseries("Plant components up", [target("chaoslab_component_up", "{{component}}")], minimum=0)
overview.timeseries("Tag ingestion rate", [target("rate(mes_tag_values_max_id[1m])", "tag values / s")],
                    description="Derived from the application's own mes_tag_values_max_id series.")
overview.timeseries("Floor: seconds since last check",
                    [target("chaoslab_floor_seconds_since_check", "{{plant}}")], unit="s")
overview.timeseries("Component memory (RSS)", [target("chaoslab_component_rss_bytes", "{{component}}")],
                    unit="bytes")
overview.timeseries("Component CPU", [target("chaoslab_component_cpu_percent", "{{component}}")],
                    unit="percent")
overview.timeseries("Host memory available", [target("chaoslab_host_memory_available_bytes", "available")],
                    unit="bytes")
overview.timeseries("Host CPU", [target("chaoslab_host_cpu_percent", "cpu")], unit="percent")
overview.write("lab-overview.json")

manufacturing = Board("chaoslab-manufacturing", "Manufacturing resilience", ["chaoslab", "manufacturing"])
manufacturing.state_timeline("Machine state", [target("chaoslab_equipment_state_code", "{{equipment}}")], STATE)
manufacturing.timeseries("Good units booked", [target("chaoslab_good_quantity", "good")],
                         description="Sum across work orders, from /workorders/summary.")
manufacturing.timeseries("Scrap units booked", [target("chaoslab_scrap_quantity", "scrap")])
manufacturing.timeseries("Work orders by status", [target("mes_work_orders", "{{status}}")],
                         description="Application series mes_work_orders.")
manufacturing.timeseries("Machines connected", [target("chaoslab_machines_connected", "connected"),
                                                 target("chaoslab_machines_total", "total", "B")])
manufacturing.timeseries("Availability (when stated)", [target("mes_equipment_availability", "{{equipment}}")],
                         minimum=0, description="Absent when the MES withholds it; it is never plotted as zero.")
manufacturing.timeseries("Observation coverage", [target("mes_equipment_coverage", "{{equipment}}")], minimum=0)
manufacturing.timeseries("Not observed seconds by cause",
                         [target("sum by (cause) (mes_equipment_not_observed_seconds)", "{{cause}}")], unit="s")
manufacturing.text("Scoring results",
                   "The upstream scoring harness scores its own ephemeral replay and is not exported as a "
                   "time series. Use `chaoslab show <experiment>` or `chaoslab compare` for booking, downtime "
                   "and OEE verdicts. OEE performance is deliberately not exported by the application.")
manufacturing.write("manufacturing-resilience.json")

comparison = Board("chaoslab-comparison", "Experiment comparison", ["chaoslab", "experiments"])
comparison.state_timeline("Experiment windows", [target("chaoslab_experiment_active", "active")], ACTIVE)
comparison.timeseries("API /health latency", [target("chaoslab_api_health_latency_seconds", "{{plant}}")], unit="s")
comparison.timeseries("Tag ingestion rate", [target("rate(mes_tag_values_max_id[30s])", "tag values / s")])
comparison.timeseries("Machines connected", [target("chaoslab_machines_connected", "connected")], minimum=0)
comparison.timeseries("Components up", [target("chaoslab_component_up", "{{component}}")], minimum=0)
comparison.table("Most recent experiment", [
    target("chaoslab_last_experiment_info", "info", "A"),
    target("chaoslab_last_experiment_recovered", "recovered", "B"),
    target("chaoslab_last_experiment_cleanup_ok", "cleanup ok", "C"),
])
comparison.table("Experiments recorded", [target("chaoslab_experiments_recorded", "recorded")], width=6)
comparison.text("Baseline vs fault vs recovery",
                "Phase-level numbers (baseline, fault, recovery, post) and recovery stages are written to "
                "`results/<id>/result.json`. Compare two runs with `scripts/run-experiment.sh` output or "
                "`python -m chaoslab compare <a> <b>`. Orange bands above are experiment windows.", width=6)
comparison.write("experiment-comparison.json")
