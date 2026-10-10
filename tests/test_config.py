import shutil
from pathlib import Path

import pytest
import yaml

from chaoslab import settings as settings_module
from chaoslab.settings import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_configuration_loads(settings):
    assert settings.limits["max_fault_duration_s"] == 60
    assert settings.limits["max_simultaneous_faults"] == 1
    assert settings.policy["memory_pressure"]["enabled"] is False
    assert settings.policy["compound"]["enabled"] is False
    assert settings.allowlist["hosts"] == ["127.0.0.1"]


def test_missing_key_is_reported(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    path = tmp_path / "config" / "safety-policy.yaml"
    data = yaml.safe_load(path.read_text())
    del data["limits"]["recovery_timeout_s"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="recovery_timeout_s"):
        settings_module.load(tmp_path)


def test_missing_file_is_reported(tmp_path):
    (tmp_path / "config").mkdir()
    with pytest.raises(ConfigError, match="missing configuration file"):
        settings_module.load(tmp_path)


def test_invalid_yaml_is_reported(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "lab.yaml").write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        settings_module.load(tmp_path)


def test_allowlist_contains_only_loopback_hosts(settings):
    assert all(host in ("127.0.0.1", "localhost") for host in settings.allowlist["hosts"])


def test_exporter_binds_to_loopback(settings):
    assert settings.lab["exporter"]["host"] in ("127.0.0.1", "localhost", "::1")
