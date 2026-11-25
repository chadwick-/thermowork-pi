"""Configuration loading and validation.

Config lives in a TOML file (see config.example.toml). The API key may also be
supplied via the THERM_API_KEY environment variable, which takes precedence, so
that the key never has to be written to disk if you would rather not.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The API caches responses for 15s and devices report about once a minute, so
# polling faster than this buys nothing and only burns rate limit.
MIN_POLL_SECONDS = 30
DEFAULT_POLL_SECONDS = 60

# RFX Meat probes transmit on temperature change rather than on a fixed
# interval; gaps of six minutes have been observed on idle probes. A threshold
# that is too tight makes an idle probe flap between online and offline.
DEFAULT_STALE_AFTER_SECONDS = 600

CHANNEL_AGGREGATES = ("min", "max", "avg")


class ConfigError(Exception):
    """Raised when the config file is missing, malformed or incomplete."""


@dataclass(frozen=True)
class PanelConfig:
    """One temperature panel on the display."""

    name: str
    device: str
    channel: str = "1"

    @property
    def is_aggregate(self) -> bool:
        return self.channel in CHANNEL_AGGREGATES


@dataclass(frozen=True)
class Config:
    api_key: str
    panels: tuple[PanelConfig, ...]
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "https://api.cloud.thermoworks.com"
    poll_seconds: int = DEFAULT_POLL_SECONDS
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    units: str = "F"
    timezone: str = "America/Chicago"
    title: str = "Therm"
    source_path: Path | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


def _require_table(raw: Any, key: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table, got {type(value).__name__}")
    return value


def _positive_int(value: Any, key: str, default: int, minimum: int = 1) -> tuple[int, str | None]:
    """Coerce to an int at or above `minimum`. Returns (value, warning)."""
    if value is None:
        return default, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    number = int(value)
    if number < minimum:
        return minimum, (
            f"{key} was {number}, which is below the minimum of {minimum}; using {minimum}."
        )
    return number, None


def _parse_channel(value: Any, panel_name: str) -> str:
    if value is None:
        return "1"
    if isinstance(value, bool):
        raise ConfigError(f"panel {panel_name!r}: channel must be a number or one of {CHANNEL_AGGREGATES}")
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str):
        text = value.strip().lower()
        if text in CHANNEL_AGGREGATES or text.isdigit():
            return text
    raise ConfigError(
        f"panel {panel_name!r}: channel must be a channel number or one of "
        f"{', '.join(CHANNEL_AGGREGATES)}; got {value!r}"
    )


def _parse_panels(raw: Any) -> tuple[PanelConfig, ...]:
    entries = raw.get("panel")
    if not entries:
        raise ConfigError(
            "No panels configured. Add at least one [[panel]] section "
            "naming a device and channel to display."
        )
    if not isinstance(entries, list):
        raise ConfigError("[[panel]] sections must form an array of tables")

    panels: list[PanelConfig] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"panel #{index} is not a table")
        device = entry.get("device")
        if not isinstance(device, str) or not device.strip():
            raise ConfigError(f"panel #{index} is missing a non-empty 'device'")
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"panel #{index}: 'name' must be a string")
        label = (name or device).strip()
        panels.append(
            PanelConfig(
                name=label,
                device=device.strip(),
                channel=_parse_channel(entry.get("channel"), label),
            )
        )
    return tuple(panels)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration.

    Search order for the config file: the `path` argument, then $THERM_CONFIG,
    then ./config.toml, then /etc/therm/config.toml.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        env_path = os.environ.get("THERM_CONFIG")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path("config.toml"))
        candidates.append(Path("/etc/therm/config.toml"))

    config_path: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            config_path = candidate
            break

    if config_path is None:
        looked = ", ".join(str(c) for c in candidates)
        raise ConfigError(
            f"No config file found (looked in: {looked}). "
            "Copy config.example.toml to config.toml and add your API key."
        )

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    warnings: list[str] = []

    # The environment wins, so a key need never be written to disk.
    api_key = (os.environ.get("THERM_API_KEY") or raw.get("api_key") or "").strip()
    if not api_key:
        raise ConfigError(
            f"No API key. Set 'api_key' in {config_path} or the THERM_API_KEY "
            "environment variable. Create a key in the ThermoWorks Cloud web "
            "app under Account Settings -> Integrations."
        )
    if api_key.startswith("twk_") is False:
        warnings.append(
            "The API key does not start with 'twk_', which is the documented "
            "prefix. Check you pasted the whole key."
        )

    server = _require_table(raw, "server")
    polling = _require_table(raw, "polling")
    display = _require_table(raw, "display")

    port, warning = _positive_int(server.get("port"), "server.port", 8000, minimum=1)
    if warning:
        warnings.append(warning)

    poll_seconds, warning = _positive_int(
        polling.get("interval_seconds"),
        "polling.interval_seconds",
        DEFAULT_POLL_SECONDS,
        minimum=MIN_POLL_SECONDS,
    )
    if warning:
        warnings.append(warning)

    stale_after, warning = _positive_int(
        polling.get("stale_after_seconds"),
        "polling.stale_after_seconds",
        DEFAULT_STALE_AFTER_SECONDS,
        minimum=60,
    )
    if warning:
        warnings.append(warning)

    units = str(display.get("units") or "F").strip().upper()
    if units not in ("F", "C"):
        raise ConfigError(f"display.units must be 'F' or 'C', got {units!r}")

    timezone_name = str(display.get("timezone") or "America/Chicago").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"display.timezone {timezone_name!r} is not a known timezone: {exc}") from exc

    host = str(server.get("host") or "127.0.0.1").strip()
    base_url = str(raw.get("base_url") or "https://api.cloud.thermoworks.com").strip()

    return Config(
        api_key=api_key,
        panels=_parse_panels(raw),
        host=host,
        port=port,
        base_url=base_url,
        poll_seconds=poll_seconds,
        stale_after_seconds=stale_after,
        units=units,
        timezone=timezone_name,
        title=str(display.get("title") or "Therm").strip(),
        source_path=config_path,
        warnings=tuple(warnings),
    )
