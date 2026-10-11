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

DELAY = [{"type": "value", "options": {
    "0": {"text": "off", "color": "green"}, "1": {"text": "DELAYING", "color": "orange"}}}]
LIMITED = [{"type": "value", "options": {
    "0": {"text": "unlimited", "color": "green"}, "1": {"text": "LIMITED", "color": "orange"}}}]

resources_board = Board("chaoslab-db-resources", "Database and resources", ["chaoslab", "database", "cgroups"])
resources_board.stat("Database delay", [target("chaoslab_db_delay_active", "{{component}}")], mappings=DELAY, width=6,
                     description="From the lab's database hook in the plant's database clients.")
resources_board.stat("Configured delay", [target("chaoslab_db_delay_configured_seconds", "delay")], unit="s", width=6,
                     description="Absent when no delay is active.")
resources_board.stat("cgroup limits", [target("chaoslab_cgroup_limited", "{{component}}")], mappings=LIMITED,
                     width=6, description="Only components running in their lab scope (resource_limit) appear.")
resources_board.stat("OOM kills (window)", [target("sum(increase(chaoslab_cgroup_memory_events_total"
                                                   "{event=\"oom_kill\"}[$__range]))", "oom kills")],
                     width=6, decimals=0)
resources_board.timeseries("Database statements / s", [
    target("sum by (component) (rate(chaoslab_db_statements_total[30s]))", "{{component}}")], minimum=0)
resources_board.timeseries("Mean statement time", [
    target("sum by (component) (rate(chaoslab_db_statement_seconds_total[30s])) / "
           "sum by (component) (rate(chaoslab_db_statements_total[30s]))", "{{component}} total", "A"),
    target("sum by (component) (rate(chaoslab_db_driver_seconds_total[30s])) / "
           "sum by (component) (rate(chaoslab_db_statements_total[30s]))", "{{component}} driver", "B")],
    unit="s", description="Total includes the injected delay; driver excludes it. The gap is the injected delay.")
resources_board.timeseries("Statement time p95 (histogram)", [
    target("histogram_quantile(0.95, sum by (le, component) "
           "(rate(chaoslab_db_statement_duration_seconds_bucket[30s])))", "{{component}} p95")], unit="s",
    description="Bucket-based estimate from the hook's histogram.")
resources_board.timeseries("Delayed statements / s and delay added", [
    target("sum by (component) (rate(chaoslab_db_injected_statements_total[30s]))", "{{component}} delayed /s", "A"),
    target("sum by (component) (rate(chaoslab_db_injected_delay_seconds_total[30s]))", "{{component}} delay s/s", "B")],
    minimum=0)
resources_board.timeseries("Database errors / s", [
    target("sum by (component) (rate(chaoslab_db_errors_total[30s]))", "{{component}} errors", "A"),
    target("sum by (component) (rate(chaoslab_db_lock_errors_total[30s]))", "{{component}} locked", "B")], minimum=0)
resources_board.timeseries("API /health latency", [target("chaoslab_api_health_latency_seconds", "{{plant}}")],
                           unit="s", description="Downstream effect of database delay or CPU limits.")
resources_board.timeseries("CPU used vs quota (CPUs)", [
    target("rate(chaoslab_cgroup_cpu_usage_seconds_total[30s])", "{{component}} used", "A"),
    target("chaoslab_cgroup_cpu_quota_cpus", "{{component}} quota", "B")], minimum=0)
resources_board.timeseries("CPU throttled ratio", [
    target("rate(chaoslab_cgroup_cpu_throttled_periods_total[30s]) / rate(chaoslab_cgroup_cpu_periods_total[30s])",
           "{{component}}")], unit="percentunit", minimum=0,
    description="Share of CFS periods in which the component was throttled.")
resources_board.timeseries("Scope memory vs limits", [
    target("chaoslab_cgroup_memory_current_bytes", "{{component}} current", "A"),
    target("chaoslab_cgroup_memory_high_bytes", "{{component}} memory.high", "B"),
    target("chaoslab_cgroup_memory_max_bytes", "{{component}} memory.max", "C")], unit="bytes", minimum=0)
resources_board.timeseries("Memory events / s", [
    target("rate(chaoslab_cgroup_memory_events_total[30s])", "{{component}} {{event}}")], minimum=0)
resources_board.timeseries("Pressure stall (share of time)", [
    target("rate(chaoslab_cgroup_pressure_stall_seconds_total[30s])", "{{component}} {{resource}}")],
    unit="percentunit", minimum=0, description="PSI 'some': time at least one task in the scope was stalled.")
resources_board.write("database-and-resources.json")

IMPACT = [{"type": "value", "options": {
    "-1": {"text": "unknown", "color": "text"}, "0": {"text": "normal", "color": "green"},
    "1": {"text": "DEGRADED", "color": "orange"}, "2": {"text": "DOWN", "color": "red"}}}]

impact = Board("chaoslab-impact", "Injection impact", ["chaoslab", "impact"])
impact.stat("Injection target", [target("chaoslab_injection_target", "{{fault}} -> {{service}}")], width=8,
            mappings=[{"type": "value", "options": {"1": {"text": "ACTIVE", "color": "orange"}}}],
            description="Absent while no experiment holds the lease.")
impact.stat("Services degraded", [target("count(chaoslab_service_impact == 1) or vector(0)", "degraded")], width=4,
            decimals=0)
impact.stat("Services down", [target("count(chaoslab_service_impact == 2) or vector(0)", "down")], width=4,
            decimals=0)
impact.stat("Experiment", [target("chaoslab_experiment_active", "state")], mappings=ACTIVE, width=8)
impact.state_timeline("Service status (each against its own baseline)",
                      [target("chaoslab_service_impact", "{{plant}} / {{service}}")], IMPACT,
                      description="normal / DEGRADED / DOWN per service, judged by the exporter from its own probes "
                                  "against each service's rolling baseline; unknown when not measured.")
impact.table("Now: status and measured reason", [target("chaoslab_service_impact_info", "info")], width=24,
             description="The reason is what was measured, e.g. '/health 1210 ms vs limit 120 ms'.")
impact.timeseries("API /health latency", [target("chaoslab_api_health_latency_seconds", "{{plant}}")], unit="s",
                  width=8)
impact.timeseries("Tag ingestion", [target("rate(chaoslab_plant_tag_values_max_id[30s])", "tags / s")], width=8,
                  minimum=0)
impact.timeseries("Database statement time", [
    target("sum(rate(chaoslab_db_statement_seconds_total[30s])) / sum(rate(chaoslab_db_statements_total[30s]))",
           "mean")], unit="s", width=8)
CONTAINER = [{"type": "value", "options": {
    "0": {"text": "STOPPED", "color": "red"}, "1": {"text": "running", "color": "green"},
    "2": {"text": "PAUSED", "color": "orange"}, "3": {"text": "restarting", "color": "yellow"}}}]
NETWORK = [{"type": "value", "options": {"0": {"text": "OFF NETWORK", "color": "red"},
                                          "1": {"text": "attached", "color": "green"}}}]
impact.state_timeline("Docker stack: container state", [target("chaoslab_container_state_code", "{{component}}")],
                      CONTAINER, description="The lab's compose stack (scripts/docker-up.sh); absent when it is down.")
impact.state_timeline("Docker stack: lab network attachment",
                      [target("chaoslab_container_on_lab_network", "{{component}}")], NETWORK)
impact.timeseries("Docker stack: container restarts", [target("chaoslab_container_restarts", "{{component}}")],
                  width=8, minimum=0)
impact.timeseries("Docker stack: container CPU", [target("chaoslab_container_cpu_percent", "{{component}}")],
                  unit="percent", width=8, minimum=0)
impact.timeseries("Docker stack: container memory", [target("chaoslab_container_memory_bytes", "{{component}}")],
                  unit="bytes", width=8, minimum=0)
impact.write("injection-impact.json")

agents = Board("chaoslab-agents", "Agents", ["chaoslab", "agents"])
agents.stat("Agents running", [target("sum(chaoslab_agent_active) or vector(0)", "running")], width=6, decimals=0)
agents.stat("Tool calls (all time)", [target("sum(chaoslab_agent_tool_calls_total) or vector(0)", "calls")], width=6,
            decimals=0)
agents.stat("Refused tool calls", [target("sum(chaoslab_agent_tool_calls_total{outcome=\"refused\"}) or vector(0)",
                                          "refused")], width=6, decimals=0,
            description="Calls outside an agent's permitted tools, or real faults a person denied.")
agents.stat("Proposals waiting for approval", [target("chaoslab_plan_proposals{status=\"proposed\"}", "proposed")],
            width=6, decimals=0)
agents.state_timeline("Which agent is running", [target("chaoslab_agent_active", "{{role}}")],
                      [{"type": "value", "options": {"0": {"text": "idle", "color": "transparent"},
                                                      "1": {"text": "RUNNING", "color": "blue"}}}])
agents.table("What each agent is doing now", [target("chaoslab_agent_activity_info", "activity")], width=24,
             description="Live from the agents' process titles: round, tool and subject.")
agents.table("Tool calls by agent and tool", [target("sum by (role, tool, outcome) (chaoslab_agent_tool_calls_total)",
                                                     "calls")], width=12)
agents.table("Agent runs by outcome", [target("chaoslab_agent_runs_total", "runs")], width=12)
agents.timeseries("Tool calls added", [target("sum by (role) (increase(chaoslab_agent_tool_calls_total[5m]))",
                                              "{{role}}")], minimum=0,
                  description="Counted when an agent saves its transcript at the end of a run.")
agents.timeseries("Proposals by status", [target("chaoslab_plan_proposals", "{{status}}")], minimum=0)
agents.write("agents.json")
