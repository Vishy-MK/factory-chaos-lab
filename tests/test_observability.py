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
    assert len(files) == 3
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
