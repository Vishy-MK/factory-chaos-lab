import json
import re
from pathlib import Path

from chaoslab.adapters.factorysemantics import parse_prometheus

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "observability" / "grafana" / "dashboards"
UPSTREAM_METRICS_SOURCE = ROOT / "factorysemantics-mes" / "src" / "fsmes" / "api" / "routers" / "system.py"
EXPORTER_SOURCE = ROOT / "chaoslab" / "observability" / "exporter.py"
METRIC = re.compile(r"\b((?:chaoslab|mes)_[a-z0-9_]+)\b")


def referenced_metrics() -> set[str]:
    names: set[str] = set()
    for path in DASHBOARDS.glob("*.json"):
        data = json.loads(path.read_text())
        for panel in data["panels"]:
            for target in panel.get("targets", []):
                names.update(METRIC.findall(target.get("expr", "")))
        for annotation in data.get("annotations", {}).get("list", []):
            names.update(METRIC.findall(annotation.get("expr", "")))
    return names


def test_dashboards_exist_and_parse():
    files = sorted(DASHBOARDS.glob("*.json"))
    assert len(files) == 6
    for path in files:
        data = json.loads(path.read_text())
        assert data["uid"] and data["title"] and data["panels"]


def test_dashboards_only_use_metrics_that_exist():
    exporter = EXPORTER_SOURCE.read_text()
    upstream = UPSTREAM_METRICS_SOURCE.read_text() if UPSTREAM_METRICS_SOURCE.is_file() else ""
    unknown = []
    for name in referenced_metrics():
        if name.startswith("chaoslab_") and f'"{name}"' in exporter:
            continue
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        if name.startswith("chaoslab_") and base != name and f'"{base}"' in exporter and "histogram" in exporter:
            continue
        if name.startswith("mes_") and name in upstream:
            continue
        unknown.append(name)
    assert not unknown, f"dashboards reference metrics nothing exports: {unknown}"


def test_prometheus_config_scrapes_only_loopback():
    text = (ROOT / "observability" / "prometheus" / "prometheus.yml").read_text()
    targets = re.findall(r'"([^"]+:\d+)"', text)
    assert targets
    assert all(t.startswith("127.0.0.1:") for t in targets)


def test_exporter_output_is_valid_exposition_text(settings, stub_plant):
    from chaoslab.observability.exporter import Collector

    collector = Collector(settings)
    collector.plant = stub_plant
    text = collector.render()
    samples = parse_prometheus(text)
    names = {n for n, _, _ in samples}
    assert "chaoslab_plant_up" in names
    assert "chaoslab_component_up" in names
    assert "chaoslab_experiment_active" in names
    assert all(isinstance(v, float) for _, _, v in samples)
    up = [v for n, _, v in samples if n == "chaoslab_plant_up"]
    assert up == [1.0]


def test_exporter_reports_plant_down_without_inventing_values(settings, stub_plant):
    from chaoslab.observability.exporter import Collector

    stub_plant.state.api_up = False
    collector = Collector(settings)
    collector.plant = stub_plant
    names = {n for n, _, _ in parse_prometheus(collector.render())}
    assert [v for n, _, v in parse_prometheus(collector.render()) if n == "chaoslab_plant_up"] == [0.0]
    assert "chaoslab_machines_connected" not in names
    assert "chaoslab_good_quantity" not in names
    assert "chaoslab_api_health_latency_seconds" not in names


def test_exporter_refuses_non_loopback_bind(lab_root):
    import yaml

    from chaoslab.observability import exporter
    from chaoslab.settings import ConfigError

    path = lab_root / "config" / "lab.yaml"
    data = yaml.safe_load(path.read_text())
    data["exporter"]["host"] = "0.0.0.0"
    path.write_text(yaml.safe_dump(data))
    try:
        exporter.serve(lab_root)
    except ConfigError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("exporter bound to a non-loopback address")


def test_exporter_publishes_database_hook_and_cgroup_metrics(settings, stub_plant, monkeypatch):
    import os
    import subprocess
    import time
    import uuid

    import pytest

    from chaoslab.chaos import cgroups
    from chaoslab.observability.exporter import Collector
    from chaoslab.plant_names import chaoslab_dbhook as hook

    directory = settings.db_hook_dir
    directory.mkdir(parents=True, exist_ok=True)
    histogram = [0] * (len(hook.BUCKETS_MS) + 1)
    histogram[0], histogram[hook.bucket_index(200.5)] = 90, 10
    (directory / f"stats-{os.getpid()}.json").write_text(json.dumps({
        "version": hook.VERSION, "pid": os.getpid(), "plant": "bottling", "component": "run-api",
        "statements": {"read": 80, "write": 20}, "statements_total": 100, "errors": 1, "lock_errors": 1,
        "observed_ms_total": 2010.0, "db_ms_total": 10.0, "observed_histogram": histogram,
        "db_histogram": [100] + [0] * len(hook.BUCKETS_MS), "buckets_ms": list(hook.BUCKETS_MS),
        "injected": {"statements": 10, "ms_total": 2000.0}, "control": {"active": True}}))
    hook.write_control(str(directory), "bottling", str(settings.database_file), 200, 5)
    collector = Collector(settings)
    collector.plant = stub_plant
    text = collector.render()
    samples = parse_prometheus(text)
    value = {(n, tuple(sorted(l.items()))): v for n, l, v in samples}
    api = (("component", "api"), ("plant", "bottling"))
    assert value[("chaoslab_db_delay_active", api)] == 1.0
    assert value[("chaoslab_db_delay_configured_seconds", (("plant", "bottling"),))] == 0.2
    assert value[("chaoslab_db_statements_total", (("component", "api"), ("kind", "write"), ("plant", "bottling")))] == 20
    assert value[("chaoslab_db_injected_delay_seconds_total", api)] == 2.0
    assert value[("chaoslab_db_statement_duration_seconds_count", api)] == 100
    assert value[("chaoslab_db_statement_duration_seconds_bucket", (("component", "api"), ("le", "+Inf"),
                                                                    ("plant", "bottling")))] == 100
    assert "# TYPE chaoslab_db_statement_duration_seconds histogram" in text
    assert "# TYPE chaoslab_db_statements_total counter" in text
    hook.remove_control(str(directory))

    ok, detail = cgroups.available()
    if not ok:
        pytest.skip(detail)
    unit = f"chaoslab-pytest-{uuid.uuid4().hex[:8]}.scope"
    proc = subprocess.Popen(cgroups.scope_command(unit, ["sleep", "30"]))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and cgroups.process_unit(proc.pid) != unit:
            time.sleep(0.05)
        cgroups.write_limits(unit, cgroups.limit_values(64, None, 20))
        stub_plant.process_table = lambda: {"api": {"pid": proc.pid, "alive": True, "status": "sleeping",
                                                    "cpu_percent": 0.0, "rss_mb": 1.0}}
        collector._cache = None
        values = {(n, tuple(sorted(l.items()))): v for n, l, v in parse_prometheus(collector.render())}
        assert values[("chaoslab_cgroup_limited", api)] == 1.0
        assert values[("chaoslab_cgroup_cpu_quota_cpus", api)] == 0.2
        assert values[("chaoslab_cgroup_memory_high_bytes", api)] == 64 * 1048576
        assert ("chaoslab_cgroup_memory_max_bytes", api) not in values, "unlimited is absent, never a number"
        assert ("chaoslab_cgroup_memory_events_total", (("component", "api"), ("event", "oom_kill"),
                                                        ("plant", "bottling"))) in values
    finally:
        proc.kill()
        proc.wait()


def test_exporter_omits_database_and_cgroup_metrics_without_evidence(settings, stub_plant):
    from chaoslab.observability.exporter import Collector

    collector = Collector(settings)
    collector.plant = stub_plant
    names = {n for n, _, _ in parse_prometheus(collector.render())}
    assert not {n for n in names if n.startswith(("chaoslab_db_", "chaoslab_cgroup_"))}


def test_a_slow_api_does_not_slow_the_scrape_and_reads_down_when_stale(settings, stub_plant):
    import time

    from chaoslab.observability.exporter import Collector

    collector = Collector(settings)
    collector.plant = stub_plant
    collector.render()                     # first scrape probes synchronously and starts the probe thread
    real_health = stub_plant.health
    stub_plant.health = lambda timeout=None: (time.sleep(3), real_health(timeout))[1]
    try:
        collector._cache = None
        started = time.monotonic()
        text = collector.render()
        assert time.monotonic() - started < 1.0, "the scrape waited for the API"
        assert "chaoslab_component_up" in text and "chaoslab_api_probe_age_seconds" in text
        collector.api_stale_s = 0.0
        collector._cache = None
        values = {n: v for n, _, v in parse_prometheus(collector.render())}
        assert values["chaoslab_plant_up"] == 0.0
        assert "chaoslab_machines_connected" not in values
    finally:
        collector.stop()
