"""Tests for reading resolution and the never-fabricate-data rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from therm.api import Alarm, Channel, Device, Snapshot
from therm.config import Config, PanelConfig
from therm.state import build_panel, build_state, convert

NOW = datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc)


def make_config(panels=None, **overrides):
    defaults = dict(
        api_key="twk_test",
        panels=tuple(panels or [PanelConfig(name="Meat", device="RFX MEAT 1", channel="1")]),
        units="F",
        timezone="America/Chicago",
        stale_after_seconds=600,
        poll_seconds=60,
    )
    defaults.update(overrides)
    return Config(**defaults)


def channel(number="1", value=200.0, status="NORMAL", age_seconds=10, **kwargs):
    return Channel(
        id=number,
        number=number,
        value=value,
        units=kwargs.pop("units", "F"),
        status=status,
        last_telemetry_saved=NOW - timedelta(seconds=age_seconds),
        **kwargs,
    )


def device(label="RFX MEAT 1", channels=None, **kwargs):
    return Device(
        id=kwargs.pop("id", "dev-1"),
        label=label,
        channels=tuple(channels if channels is not None else [channel()]),
        **kwargs,
    )


def snapshot(devices):
    return Snapshot(generated_at=NOW, devices=tuple(devices))


# -- the core rule -----------------------------------------------------------

def test_missing_device_reports_missing_and_no_value():
    panel = build_panel(
        PanelConfig(name="Meat", device="NOT PRESENT", channel="1"),
        snapshot([device()]),
        make_config(),
        NOW,
    )
    assert panel.state == "missing"
    assert panel.value is None
    # The message should name what the account actually has, to aid setup.
    assert "RFX MEAT 1" in (panel.detail or "")


def test_no_probe_channel_yields_no_value_even_though_api_sent_one():
    """A channel with no probe still reports a plausible room temperature.
    That number must never reach the display."""
    probe_less = channel(value=71.4, status="NO PROBE")
    panel = build_panel(
        PanelConfig(name="Ambient", device="RFX GATEWAY", channel="1"),
        snapshot([device(label="RFX GATEWAY", channels=[probe_less])]),
        make_config(),
        NOW,
    )
    assert panel.state == "no_probe"
    assert panel.value is None


def test_probe_failed_flag_is_respected():
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([device(channels=[channel(value=180.0, probe_failed=True)])]),
        make_config(),
        NOW,
    )
    assert panel.state == "no_probe"
    assert panel.value is None


def test_no_snapshot_yields_unknown_not_a_guess():
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        None,
        make_config(),
        NOW,
    )
    assert panel.state == "unknown"
    assert panel.value is None


# -- staleness ---------------------------------------------------------------

def test_fresh_reading_is_ok():
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([device(channels=[channel(value=203.0, age_seconds=30)])]),
        make_config(),
        NOW,
    )
    assert panel.state == "ok"
    assert panel.value == pytest.approx(203.0)


def test_old_reading_is_stale_but_keeps_its_value():
    """Stale data is still shown -- flagged, with its age -- because the last
    known temperature is useful. It must not be silently presented as current."""
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([device(channels=[channel(value=203.0, age_seconds=1200)])]),
        make_config(),
        NOW,
    )
    assert panel.state == "stale"
    assert panel.value == pytest.approx(203.0)
    assert panel.age_seconds == pytest.approx(1200)
    assert "20m" in (panel.detail or "")


def test_staleness_threshold_is_applied_uniformly_across_panels():
    """The previous implementation used 300s for two probes and 60s for the
    third, so one panel flapped offline on its own."""
    panels = [
        PanelConfig(name="A", device="D1", channel="1"),
        PanelConfig(name="B", device="D2", channel="1"),
        PanelConfig(name="C", device="D3", channel="1"),
    ]
    devices = [
        device(label=f"D{i}", id=f"d{i}", channels=[channel(value=100.0, age_seconds=400)])
        for i in (1, 2, 3)
    ]
    state = build_state(
        make_config(panels=panels, stale_after_seconds=600),
        snapshot(devices),
        connection="ok",
        last_success=NOW,
        now=NOW,
    )
    assert [p["state"] for p in state["panels"]] == ["ok", "ok", "ok"]


# -- aggregates --------------------------------------------------------------

def test_min_picks_coolest_healthy_channel():
    """On an RFX Meat probe the deepest sensor reads coolest, and that is the
    number that decides doneness."""
    channels = [
        channel("1", 150.0),
        channel("2", 161.0),
        channel("3", 172.0),
        channel("4", 185.0),
    ]
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="min"),
        snapshot([device(channels=channels)]),
        make_config(),
        NOW,
    )
    assert panel.state == "ok"
    assert panel.value == pytest.approx(150.0)
    assert panel.channel_count == 4


def test_aggregate_excludes_unhealthy_channels():
    """A disconnected sensor reporting room temperature must not drag the
    minimum down to 70 while the meat is at 150."""
    channels = [
        channel("1", 150.0),
        channel("2", 70.0, status="NO PROBE"),
        channel("3", 172.0),
    ]
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="min"),
        snapshot([device(channels=channels)]),
        make_config(),
        NOW,
    )
    assert panel.value == pytest.approx(150.0)
    assert panel.channel_count == 2


def test_avg_is_mean_of_healthy_channels():
    channels = [channel("1", 100.0), channel("2", 200.0), channel("3", 60.0, status="OPEN")]
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="avg"),
        snapshot([device(channels=channels)]),
        make_config(),
        NOW,
    )
    assert panel.value == pytest.approx(150.0)


def test_aggregate_with_no_healthy_channels_is_no_probe():
    channels = [channel("1", 70.0, status="NO PROBE"), channel("2", 71.0, status="NO PROBE")]
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="min"),
        snapshot([device(channels=channels)]),
        make_config(),
        NOW,
    )
    assert panel.state == "no_probe"
    assert panel.value is None


# -- units and alarms --------------------------------------------------------

def test_convert_fahrenheit_to_celsius():
    assert convert(212.0, "F", "C") == pytest.approx(100.0)
    assert convert(32.0, "F", "C") == pytest.approx(0.0)
    assert convert(100.0, "C", "F") == pytest.approx(212.0)


def test_convert_passes_through_matching_units():
    assert convert(203.0, "F", "F") == pytest.approx(203.0)


def test_unknown_source_units_are_not_mangled():
    assert convert(203.0, None, "F") == pytest.approx(203.0)
    assert convert(203.0, "", "C") == pytest.approx(203.0)


def test_channel_value_is_converted_to_display_units():
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([device(channels=[channel(value=212.0, units="F")])]),
        make_config(units="C"),
        NOW,
    )
    assert panel.value == pytest.approx(100.0)


def test_alarm_threshold_uses_device_display_units():
    """The API reports alarm thresholds in the device's displayUnits, which can
    differ from the channel's reading units."""
    ch = channel(value=100.0, units="C", alarm_high=Alarm(enabled=True, value=212.0))
    dev = device(channels=[ch], display_units="F")
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([dev]),
        make_config(units="C"),
        NOW,
    )
    assert panel.value == pytest.approx(100.0)
    assert panel.alarm_high == pytest.approx(100.0)


def test_disabled_alarm_is_not_shown():
    ch = channel(alarm_high=Alarm(enabled=False, value=203.0))
    panel = build_panel(
        PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),
        snapshot([device(channels=[ch])]),
        make_config(),
        NOW,
    )
    assert panel.alarm_high is None


# -- payload shape -----------------------------------------------------------

def test_build_state_payload_has_what_the_ui_needs():
    state = build_state(
        make_config(),
        snapshot([device(channels=[channel(value=203.4, age_seconds=0)])]),
        connection="ok",
        last_success=NOW,
        now=NOW,
    )
    assert state["connection"] == "ok"
    assert state["units"] == "F"
    assert len(state["panels"]) == 1
    panel = state["panels"][0]
    assert panel["value"] == pytest.approx(203.4)
    # Central time in November is UTC-6, so 18:00:00Z is 12:00pm.
    assert panel["updatedAt"] == "12:00pm"


def test_error_is_surfaced_without_destroying_last_known_readings():
    state = build_state(
        make_config(),
        snapshot([device(channels=[channel(value=203.0, age_seconds=30)])]),
        connection="error",
        error="Could not reach api.cloud.thermoworks.com",
        last_success=NOW - timedelta(seconds=90),
        now=NOW,
    )
    assert state["connection"] == "error"
    assert "thermoworks" in state["error"].lower()
    # The reading itself is still there to show.
    assert state["panels"][0]["value"] == pytest.approx(203.0)
    assert state["dataAgeSeconds"] == 90
