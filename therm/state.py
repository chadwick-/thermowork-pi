"""Turn an API snapshot into the state the display renders.

Design rule, learned the hard way from the previous version of this project:
this module never invents a temperature. If a reading is unavailable, stale or
coming from a channel with no probe attached, that fact is reported as such.
A cooking thermometer that shows a plausible but fabricated number is worse
than one that shows nothing, so every value carries its own provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .api import Channel, Device, Snapshot
from .config import Config, PanelConfig

ReadingState = Literal["ok", "stale", "no_probe", "missing", "unknown"]
ConnectionState = Literal["ok", "stale", "error", "auth_error", "starting"]


def convert(value: float | None, from_units: str | None, to_units: str) -> float | None:
    """Convert between Fahrenheit and Celsius. Unknown source units are
    assumed to already be in the target scale rather than silently mangled."""
    if value is None:
        return None
    source = (from_units or to_units or "F").strip().upper()[:1]
    target = to_units.strip().upper()[:1]
    if source == target or source not in ("F", "C"):
        return value
    if source == "F" and target == "C":
        return (value - 32.0) * 5.0 / 9.0
    if source == "C" and target == "F":
        return value * 9.0 / 5.0 + 32.0
    return value


def _pick_channel(
    device: Device, panel: PanelConfig
) -> tuple[Channel | None, list[Channel]]:
    """Resolve a panel's channel spec to a channel and its contributing set.

    For an aggregate ("min"/"max"/"avg") only healthy channels contribute, so a
    disconnected sensor on a multi-sensor probe cannot drag the figure around.
    """
    if not panel.is_aggregate:
        channel = device.channel(panel.channel)
        return channel, [channel] if channel else []

    healthy = [ch for ch in device.channels if ch.healthy and ch.value is not None]
    if not healthy:
        return None, []

    if panel.channel == "min":
        return min(healthy, key=lambda c: c.value or 0.0), healthy
    if panel.channel == "max":
        return max(healthy, key=lambda c: c.value or 0.0), healthy
    return healthy[0], healthy  # "avg": representative channel for metadata


def _age_seconds(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


@dataclass
class PanelReading:
    """One panel's resolved value, with everything the UI needs to be honest
    about how much to trust it."""

    name: str
    device: str
    state: ReadingState
    value: float | None = None
    units: str = "F"
    age_seconds: float | None = None
    measured_at: datetime | None = None
    status: str | None = None
    detail: str | None = None
    battery: float | None = None
    battery_state: str | None = None
    signal: float | None = None
    alarm_high: float | None = None
    alarm_low: float | None = None
    alarming: bool = False
    channel_count: int = 0

    def to_json(self, tzinfo: Any) -> dict[str, Any]:
        local_time = (
            self.measured_at.astimezone(tzinfo).strftime("%-I:%M%p").lower()
            if self.measured_at
            else None
        )
        return {
            "name": self.name,
            "device": self.device,
            "state": self.state,
            # Rounded for display only; the UI must not do arithmetic on this.
            "value": None if self.value is None else round(self.value, 1),
            "units": self.units,
            "ageSeconds": None if self.age_seconds is None else int(self.age_seconds),
            "updatedAt": local_time,
            "status": self.status,
            "detail": self.detail,
            "battery": None if self.battery is None else int(self.battery),
            "batteryState": self.battery_state,
            "signal": None if self.signal is None else int(self.signal),
            "alarmHigh": self.alarm_high,
            "alarmLow": self.alarm_low,
            "alarming": self.alarming,
            "channelCount": self.channel_count,
        }


def build_panel(
    panel: PanelConfig,
    snapshot: Snapshot | None,
    config: Config,
    now: datetime | None = None,
) -> PanelReading:
    """Resolve a single configured panel against a snapshot."""
    now = now or datetime.now(timezone.utc)

    if snapshot is None:
        return PanelReading(
            name=panel.name,
            device=panel.device,
            state="unknown",
            units=config.units,
            detail="Waiting for first reading",
        )

    device = snapshot.by_id_or_label(panel.device)
    if device is None:
        available = ", ".join(sorted(d.display_name for d in snapshot.devices)) or "none"
        return PanelReading(
            name=panel.name,
            device=panel.device,
            state="missing",
            units=config.units,
            detail=f"No device named {panel.device!r} on this account. Found: {available}",
        )

    channel, contributing = _pick_channel(device, panel)

    base = PanelReading(
        name=panel.name,
        device=device.display_name,
        state="unknown",
        units=config.units,
        battery=device.battery,
        battery_state=device.battery_state,
        signal=device.wifi_signal if device.wifi_signal is not None else device.gateway_signal,
        channel_count=len(contributing),
    )

    if channel is None:
        base.state = "no_probe"
        base.detail = (
            "No healthy channel reporting"
            if panel.is_aggregate
            else f"Device has no channel {panel.channel}"
        )
        return base

    base.status = channel.status
    base.alarming = channel.alarming

    if not channel.healthy:
        base.state = "no_probe"
        base.detail = (channel.status or "Probe not reporting").title()
        return base

    # Average is computed across the healthy set rather than taken from one
    # channel, so it reflects the whole probe.
    if panel.channel == "avg" and contributing:
        values = [c.value for c in contributing if c.value is not None]
        raw_value = sum(values) / len(values) if values else None
        source_units = contributing[0].units
    else:
        raw_value = channel.value
        source_units = channel.units

    base.value = convert(raw_value, source_units, config.units)

    # Alarm thresholds are expressed in the device's display units, which can
    # differ from the channel's reading units.
    alarm_units = device.display_units or source_units
    if channel.alarm_high and channel.alarm_high.enabled:
        base.alarm_high = convert(channel.alarm_high.value, alarm_units, config.units)
    if channel.alarm_low and channel.alarm_low.enabled:
        base.alarm_low = convert(channel.alarm_low.value, alarm_units, config.units)

    measured_at = channel.last_telemetry_saved or channel.last_seen
    base.measured_at = measured_at
    base.age_seconds = _age_seconds(measured_at, now)

    if base.age_seconds is None:
        base.state = "ok"
        base.detail = "No timestamp reported"
    elif base.age_seconds > config.stale_after_seconds:
        base.state = "stale"
        base.detail = f"Last reading {_humanize(base.age_seconds)} ago"
        # An alarm flag attached to a stale reading describes the past, not
        # the present: we do not know whether the probe is in alarm now. A red
        # ALARM on a ten-day-old number reads as "act now", so staleness wins.
        base.alarming = False
    else:
        base.state = "ok"

    return base


def _humanize(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    return f"{hours // 24}d {hours % 24}h"


def build_state(
    config: Config,
    snapshot: Snapshot | None,
    connection: ConnectionState,
    error: str | None = None,
    last_success: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the complete JSON payload the display polls for."""
    now = now or datetime.now(timezone.utc)
    panels = [build_panel(panel, snapshot, config, now) for panel in config.panels]

    data_age = _age_seconds(last_success, now)
    return {
        "title": config.title,
        "connection": connection,
        "error": error,
        "units": config.units,
        "serverTime": now.astimezone(config.tzinfo).strftime("%-I:%M%p").lower(),
        "lastSuccessAt": (
            last_success.astimezone(config.tzinfo).strftime("%-I:%M:%S%p").lower()
            if last_success
            else None
        ),
        "dataAgeSeconds": None if data_age is None else int(data_age),
        "dataAgeText": _humanize(data_age) if data_age is not None else None,
        "pollSeconds": config.poll_seconds,
        "staleAfterSeconds": config.stale_after_seconds,
        "panels": [panel.to_json(config.tzinfo) for panel in panels],
    }
