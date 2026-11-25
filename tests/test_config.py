"""Tests for config parsing and validation."""

from __future__ import annotations

import pytest

from therm.config import (
    DEFAULT_POLL_SECONDS,
    MIN_POLL_SECONDS,
    ConfigError,
    load,
)

MINIMAL = """
api_key = "twk_abc"
[[panel]]
name = "Meat"
device = "RFX MEAT 1"
channel = 1
"""


def write(tmp_path, text, name="config.toml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_minimal_config_loads_with_sensible_defaults(tmp_path):
    config = load(write(tmp_path, MINIMAL))
    assert config.api_key == "twk_abc"
    assert config.host == "127.0.0.1"           # not exposed to the network
    assert config.port == 8000
    assert config.poll_seconds == DEFAULT_POLL_SECONDS
    assert config.units == "F"
    assert len(config.panels) == 1
    assert config.panels[0].channel == "1"


def test_env_var_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("THERM_API_KEY", "twk_from_env")
    config = load(write(tmp_path, MINIMAL))
    assert config.api_key == "twk_from_env"


def test_missing_file_names_where_it_looked(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path / "nope.toml")
    assert "nope.toml" in str(excinfo.value)


def test_missing_api_key_is_an_actionable_error(tmp_path, monkeypatch):
    monkeypatch.delenv("THERM_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, '[[panel]]\ndevice = "X"\n'))
    assert "Integrations" in str(excinfo.value)


def test_no_panels_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, 'api_key = "twk_x"\n'))
    assert "panel" in str(excinfo.value).lower()


def test_panel_without_device_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load(write(tmp_path, 'api_key = "twk_x"\n[[panel]]\nname = "No device"\n'))


def test_invalid_toml_is_reported_clearly(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, "api_key = unquoted value\n"))
    assert "valid TOML" in str(excinfo.value)


def test_poll_interval_is_floored_not_silently_accepted(tmp_path):
    """Polling every 5s would hit the free tier's 4/min limit instantly."""
    config = load(write(tmp_path, MINIMAL + "\n[polling]\ninterval_seconds = 5\n"))
    assert config.poll_seconds == MIN_POLL_SECONDS
    assert any("below the minimum" in w for w in config.warnings)


def test_aggregate_channel_specs_are_accepted(tmp_path):
    text = 'api_key = "twk_x"\n[[panel]]\ndevice = "D"\nchannel = "min"\n'
    config = load(write(tmp_path, text))
    assert config.panels[0].channel == "min"
    assert config.panels[0].is_aggregate


def test_bad_channel_spec_is_rejected(tmp_path):
    text = 'api_key = "twk_x"\n[[panel]]\ndevice = "D"\nchannel = "middle"\n'
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, text))
    assert "channel" in str(excinfo.value)


def test_bad_units_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load(write(tmp_path, MINIMAL + '\n[display]\nunits = "kelvin"\n'))


def test_bad_timezone_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load(write(tmp_path, MINIMAL + '\n[display]\ntimezone = "Mars/Olympus"\n'))
    assert "timezone" in str(excinfo.value).lower()


def test_key_without_expected_prefix_warns_but_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("THERM_API_KEY", raising=False)
    config = load(write(tmp_path, MINIMAL.replace("twk_abc", "abc123")))
    assert config.api_key == "abc123"
    assert any("twk_" in w for w in config.warnings)


def test_panel_name_defaults_to_the_device_label(tmp_path):
    config = load(write(tmp_path, 'api_key = "twk_x"\n[[panel]]\ndevice = "RFX GATEWAY"\n'))
    assert config.panels[0].name == "RFX GATEWAY"
