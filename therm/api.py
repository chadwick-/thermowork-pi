"""Client for the official ThermoWorks Cloud Integrations API.

This replaces the old Firebase email/password flow, which ThermoWorks disabled
in November 2026 by enforcing Firebase App Check. Password login now fails with
401 "Firebase App Check token is invalid" and cannot be made to work from an
unofficial client -- App Check exists precisely to prevent that.

The supported API is read-only and authenticated with an API key created in the
ThermoWorks Cloud web app under Account Settings -> Integrations.

Spec: https://cloud.thermoworks.com/api/docs/openapi.yaml
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

_LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.cloud.thermoworks.com"
USER_AGENT = "therm/1.0 (+https://github.com/)"


class ThermoworksError(Exception):
    """Base class for all API failures."""


class AuthError(ThermoworksError):
    """The API key was rejected: missing, malformed, revoked or expired."""


class RateLimitError(ThermoworksError):
    """Rate limit exceeded (HTTP 429)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransportError(ThermoworksError):
    """The request never produced a usable HTTP response (DNS, TCP, TLS, timeout)."""


def _parse_ts(value: Any) -> datetime | None:
    """Parse an API timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _LOG.debug("unparseable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_float(value: Any) -> float | None:
    """Coerce an API numeric field to float. The API has been seen to send
    numbers as strings, so accept both and reject anything else."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Alarm:
    enabled: bool = False
    value: float | None = None
    alarming: bool = False

    @classmethod
    def from_json(cls, raw: Any) -> "Alarm | None":
        if not isinstance(raw, dict):
            return None
        return cls(
            enabled=bool(raw.get("enabled")),
            value=_as_float(raw.get("value")),
            alarming=bool(raw.get("alarming")),
        )


@dataclass(frozen=True)
class Channel:
    id: str
    number: str | None = None
    label: str | None = None
    type: str | None = None
    enabled: bool = True
    value: float | None = None
    units: str | None = None
    status: str | None = None
    probe_failed: bool = False
    alarming: bool = False
    alarm_high: Alarm | None = None
    alarm_low: Alarm | None = None
    last_telemetry_saved: datetime | None = None
    last_seen: datetime | None = None

    @property
    def healthy(self) -> bool:
        """True when this channel is reporting a usable reading.

        A channel with no probe attached still reports a plausible room
        temperature, so status must be checked, not just the value.
        """
        if not self.enabled or self.probe_failed or self.value is None:
            return False
        status = (self.status or "").strip().upper()
        return status not in {"NO PROBE", "OPEN", "SHORT", "ERROR", "OFFLINE"}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Channel":
        number = raw.get("number")
        return cls(
            id=str(raw.get("id") or number or ""),
            number=None if number is None else str(number),
            label=raw.get("label"),
            type=raw.get("type"),
            enabled=bool(raw.get("enabled", True)),
            value=_as_float(raw.get("value")),
            units=raw.get("units"),
            status=raw.get("status"),
            probe_failed=bool(raw.get("probeFailed")),
            alarming=bool(raw.get("alarming")),
            alarm_high=Alarm.from_json(raw.get("alarmHigh")),
            alarm_low=Alarm.from_json(raw.get("alarmLow")),
            last_telemetry_saved=_parse_ts(raw.get("lastTelemetrySaved")),
            last_seen=_parse_ts(raw.get("lastSeen")),
        )


@dataclass(frozen=True)
class Device:
    id: str
    label: str | None = None
    serial: str | None = None
    model: str | None = None
    type: str | None = None
    firmware: str | None = None
    battery: float | None = None
    battery_state: str | None = None
    wifi_signal: float | None = None
    gateway_signal: float | None = None
    display_units: str | None = None
    last_seen: datetime | None = None
    channels: tuple[Channel, ...] = field(default_factory=tuple)

    @property
    def display_name(self) -> str:
        return self.label or self.serial or self.id

    def channel(self, number: str) -> Channel | None:
        wanted = str(number).strip()
        for ch in self.channels:
            if ch.number == wanted or ch.id == wanted:
                return ch
        return None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Device":
        raw_channels = raw.get("channels")
        channels = tuple(
            Channel.from_json(c) for c in raw_channels if isinstance(c, dict)
        ) if isinstance(raw_channels, list) else ()
        return cls(
            id=str(raw.get("id") or ""),
            label=raw.get("label"),
            serial=raw.get("serial"),
            model=raw.get("model"),
            type=raw.get("type"),
            firmware=raw.get("firmware"),
            battery=_as_float(raw.get("battery")),
            battery_state=raw.get("batteryState"),
            wifi_signal=_as_float(raw.get("wifiSignal")),
            gateway_signal=_as_float(raw.get("gatewaySignal")),
            display_units=raw.get("displayUnits"),
            last_seen=_parse_ts(raw.get("lastSeen")),
            channels=channels,
        )


@dataclass(frozen=True)
class Snapshot:
    generated_at: datetime | None
    devices: tuple[Device, ...]
    etag: str | None = None

    def __iter__(self) -> Iterator[Device]:
        return iter(self.devices)

    def by_label(self, label: str) -> Device | None:
        """Find a device by label, case-insensitively."""
        wanted = label.strip().casefold()
        for device in self.devices:
            if (device.label or "").strip().casefold() == wanted:
                return device
        return None

    def by_id_or_label(self, key: str) -> Device | None:
        wanted = key.strip()
        for device in self.devices:
            if device.id == wanted or device.serial == wanted:
                return device
        return self.by_label(wanted)


@dataclass(frozen=True)
class Limits:
    """Server-advertised polling guidance from GET /v1/me."""

    per_minute: int | None = None
    per_day: int | None = None
    recommended_poll_seconds: int | None = None
    api_tier: str | None = None


class ApiClient:
    """Minimal, dependency-free client for the Integrations API.

    Only the endpoints this app needs are implemented. Uses conditional
    requests so that repeated polls of unchanged data cost a 304 instead of a
    full payload.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise AuthError(
                "No API key configured. Create one in the ThermoWorks Cloud "
                "web app under Account Settings -> Integrations."
            )
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._devices_etag: str | None = None

    def _request(self, path: str, etag: str | None = None) -> tuple[int, dict[str, Any], str | None]:
        """Perform a GET. Returns (status, parsed_body, etag).

        A 304 yields (304, {}, etag). Raises on auth, rate-limit and transport
        failures so callers can distinguish "retry later" from "stop and tell
        the user to fix their key".
        """
        url = f"{self._base_url}{path}"
        headers = {
            # The spec documents both; Bearer is the primary form.
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if etag:
            headers["If-None-Match"] = etag

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
                new_etag = response.headers.get("ETag")
                if not body:
                    return response.status, {}, new_etag
                return response.status, json.loads(body), new_etag
        except urllib.error.HTTPError as exc:
            self._raise_for_http_error(exc, etag)
            raise  # unreachable; _raise_for_http_error always raises
        except urllib.error.URLError as exc:
            raise TransportError(f"Could not reach {self._base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"Request to {self._base_url} timed out") from exc
        except json.JSONDecodeError as exc:
            raise ThermoworksError(f"Malformed JSON from {url}: {exc}") from exc

    def _raise_for_http_error(self, exc: urllib.error.HTTPError, etag: str | None) -> None:
        if exc.code == 304:
            raise _NotModified(etag)

        detail = ""
        code = ""
        try:
            payload = json.loads(exc.read() or b"{}")
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                detail = str(error.get("message") or "")
        except (json.JSONDecodeError, ValueError, OSError):
            pass
        # The server's message usually ends in a full stop of its own.
        detail = detail.strip().rstrip(".")
        suffix = f": {detail}" if detail else ""

        if exc.code in (401, 403) or code in ("invalid_api_key", "expired_api_key", "insufficient_scope"):
            # Says only what happened. Callers add the instructions that suit
            # their medium, so the advice is not printed twice.
            raise AuthError(f"ThermoWorks rejected the API key ({code or exc.code}){suffix}.")
        if exc.code == 429 or code == "rate_limited":
            retry_after = None
            raw_retry = exc.headers.get("Retry-After") if exc.headers else None
            if raw_retry:
                try:
                    retry_after = int(float(raw_retry))
                except ValueError:
                    retry_after = None
            raise RateLimitError(
                f"Rate limited by ThermoWorks{suffix}", retry_after=retry_after
            )
        raise ThermoworksError(f"ThermoWorks API returned HTTP {exc.code}{suffix}")

    def get_limits(self) -> Limits:
        """Fetch key metadata and the server's own polling advice."""
        _status, payload, _etag = self._request("/v1/me")
        rate = payload.get("rateLimit") if isinstance(payload.get("rateLimit"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}

        def _as_int(value: Any) -> int | None:
            number = _as_float(value)
            return None if number is None else int(number)

        return Limits(
            per_minute=_as_int(rate.get("perMinute")),
            per_day=_as_int(rate.get("perDay")),
            recommended_poll_seconds=_as_int(payload.get("recommendedPollSeconds")),
            api_tier=plan.get("apiTier"),
        )

    def get_devices(self) -> Snapshot | None:
        """Fetch all devices and channels in one call.

        Returns None when the server reports the data is unchanged (304), in
        which case the caller should keep the snapshot it already has.
        """
        try:
            _status, payload, etag = self._request("/v1/devices", etag=self._devices_etag)
        except _NotModified:
            return None

        self._devices_etag = etag
        raw_devices = payload.get("devices")
        if not isinstance(raw_devices, list):
            raise ThermoworksError("Response from /v1/devices had no 'devices' array")

        devices = tuple(
            Device.from_json(d) for d in raw_devices if isinstance(d, dict)
        )
        return Snapshot(
            generated_at=_parse_ts(payload.get("generatedAt")),
            devices=devices,
            etag=etag,
        )


class _NotModified(Exception):
    """Internal signal that the server returned 304."""

    def __init__(self, etag: str | None) -> None:
        super().__init__("not modified")
        self.etag = etag
