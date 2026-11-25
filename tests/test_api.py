"""Tests for API parsing and error classification."""

from __future__ import annotations

import json
import urllib.error
from datetime import timezone

import pytest

from therm.api import (
    ApiClient,
    AuthError,
    Channel,
    Device,
    RateLimitError,
    ThermoworksError,
    TransportError,
    _as_float,
    _parse_ts,
)


# -- field coercion ----------------------------------------------------------

def test_as_float_accepts_numbers_and_numeric_strings():
    """The old Firebase API sent values as int, float and string
    interchangeably, so be liberal here."""
    assert _as_float(70) == 70.0
    assert _as_float(69.9) == pytest.approx(69.9)
    assert _as_float("69.9") == pytest.approx(69.9)
    assert _as_float(" 70 ") == 70.0


def test_as_float_rejects_junk_rather_than_guessing():
    assert _as_float(None) is None
    assert _as_float("") is None
    assert _as_float("n/a") is None
    assert _as_float(True) is None
    assert _as_float({}) is None


def test_parse_ts_handles_z_suffix_and_offsets():
    parsed = _parse_ts("2026-09-09T12:49:36.000Z")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == 12
    assert _parse_ts("2026-09-09T12:49:36+00:00") == parsed


def test_parse_ts_rejects_junk():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not a date") is None
    assert _parse_ts(12345) is None


# -- channel health ----------------------------------------------------------

@pytest.mark.parametrize("status", ["NO PROBE", "OPEN", "SHORT", "ERROR", "OFFLINE", "no probe"])
def test_unhealthy_statuses_are_not_trusted(status):
    assert Channel(id="1", value=70.0, status=status).healthy is False


@pytest.mark.parametrize("status", ["NORMAL", "LOW", "HIGH", None, ""])
def test_reporting_statuses_are_trusted(status):
    """LOW and HIGH mean 'outside your alarm range', not 'broken' -- the
    reading is still real and must be displayed."""
    assert Channel(id="1", value=70.0, status=status).healthy is True


def test_channel_without_value_is_unhealthy():
    assert Channel(id="1", value=None, status="NORMAL").healthy is False


def test_disabled_channel_is_unhealthy():
    assert Channel(id="1", value=200.0, status="NORMAL", enabled=False).healthy is False


# -- device parsing ----------------------------------------------------------

def test_device_parses_real_api_shape():
    raw = {
        "id": "M100142781",
        "serial": "M100142781",
        "label": "RFX MEAT 1",
        "battery": 100,
        "displayUnits": "F",
        "lastSeen": "2026-11-25T18:03:30.136Z",
        "channels": [
            {
                "id": "1",
                "number": "1",
                "enabled": True,
                "probeFailed": False,
                "alarming": False,
                "status": "NORMAL",
                "value": 59.79999923706055,
                "units": "F",
                "lastTelemetrySaved": "2026-11-25T18:03:29.800Z",
                "alarmHigh": {"enabled": True, "value": 203, "alarming": False},
            }
        ],
    }
    device = Device.from_json(raw)
    assert device.label == "RFX MEAT 1"
    assert device.battery == 100.0
    assert device.display_units == "F"
    assert len(device.channels) == 1
    channel = device.channels[0]
    assert channel.value == pytest.approx(59.8)
    assert channel.healthy is True
    assert channel.alarm_high is not None
    assert channel.alarm_high.value == 203.0
    assert channel.last_telemetry_saved is not None


def test_device_survives_missing_and_null_fields():
    """Every field but id and channels is nullable in the spec."""
    device = Device.from_json({"id": "x", "channels": None, "label": None, "battery": None})
    assert device.channels == ()
    assert device.display_name == "x"
    assert device.battery is None


def test_device_skips_malformed_channel_entries():
    device = Device.from_json({"id": "x", "channels": [{"id": "1"}, "junk", None, 42]})
    assert len(device.channels) == 1


def test_channel_lookup_by_number():
    device = Device.from_json(
        {"id": "x", "channels": [{"id": "1", "number": "1"}, {"id": "2", "number": "2"}]}
    )
    assert device.channel("2") is not None
    assert device.channel(2) is not None          # int accepted
    assert device.channel("9") is None


# -- error classification ----------------------------------------------------

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, payload=None, headers=None):
        body = json.dumps(payload or {}).encode()
        super().__init__("http://x", code, "err", headers or {}, None)
        self._body = body

    def read(self):
        return self._body


def _client():
    return ApiClient(api_key="twk_test")


def test_empty_key_is_rejected_up_front_with_actionable_message():
    with pytest.raises(AuthError) as excinfo:
        ApiClient(api_key="   ")
    assert "Integrations" in str(excinfo.value)


@pytest.mark.parametrize("code", [401, 403])
def test_http_401_403_is_an_auth_error(code):
    error = _FakeHTTPError(code, {"error": {"code": "invalid_api_key", "message": "bad key"}})
    with pytest.raises(AuthError) as excinfo:
        _client()._raise_for_http_error(error, None)
    message = str(excinfo.value)
    assert "invalid_api_key" in message
    assert "bad key" in message


def test_auth_error_does_not_duplicate_the_servers_full_stop():
    """The UI appends its own instructions, so a trailing '.' from the API
    used to produce '...required.. Check it'."""
    error = _FakeHTTPError(
        401, {"error": {"code": "invalid_api_key", "message": "A valid API key is required."}}
    )
    with pytest.raises(AuthError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert ".." not in str(excinfo.value)


def test_auth_error_leaves_instructions_to_the_caller():
    """Both the CLI and the web UI add advice suited to their medium; the
    exception carrying it too printed it twice on screen."""
    error = _FakeHTTPError(401, {"error": {"code": "invalid_api_key", "message": "bad key"}})
    with pytest.raises(AuthError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert "Integrations" not in str(excinfo.value)


def test_expired_key_code_is_an_auth_error_even_on_a_200_ish_status():
    error = _FakeHTTPError(400, {"error": {"code": "expired_api_key", "message": "expired"}})
    with pytest.raises(AuthError):
        _client()._raise_for_http_error(error, None)


def test_rate_limit_captures_retry_after():
    error = _FakeHTTPError(
        429, {"error": {"code": "rate_limited", "message": "slow down"}}, {"Retry-After": "42"}
    )
    with pytest.raises(RateLimitError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert excinfo.value.retry_after == 42


def test_rate_limit_without_retry_after_is_still_a_rate_limit():
    error = _FakeHTTPError(429, {})
    with pytest.raises(RateLimitError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert excinfo.value.retry_after is None


def test_server_error_is_a_generic_api_error():
    error = _FakeHTTPError(500, {"error": {"code": "internal", "message": "boom"}})
    with pytest.raises(ThermoworksError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert "500" in str(excinfo.value)


def test_unparseable_error_body_does_not_mask_the_status():
    error = _FakeHTTPError(502)
    error._body = b"<html>gateway</html>"
    with pytest.raises(ThermoworksError) as excinfo:
        _client()._raise_for_http_error(error, None)
    assert "502" in str(excinfo.value)


# -- request plumbing --------------------------------------------------------

def test_get_devices_parses_payload_and_stores_etag(monkeypatch):
    client = _client()
    payload = {
        "generatedAt": "2026-11-25T18:05:49.870Z",
        "devices": [{"id": "d1", "label": "RFX GATEWAY", "channels": [{"id": "1", "value": 69.9}]}],
    }
    monkeypatch.setattr(client, "_request", lambda path, etag=None: (200, payload, 'W/"abc"'))

    snapshot = client.get_devices()
    assert snapshot is not None
    assert len(snapshot.devices) == 1
    assert snapshot.by_label("rfx gateway") is not None   # case-insensitive
    assert client._devices_etag == 'W/"abc"'


def test_get_devices_sends_stored_etag_on_the_next_call(monkeypatch):
    client = _client()
    seen = {}

    def fake_request(path, etag=None):
        seen["etag"] = etag
        return 200, {"devices": []}, 'W/"v2"'

    monkeypatch.setattr(client, "_request", fake_request)
    client._devices_etag = 'W/"v1"'
    client.get_devices()
    assert seen["etag"] == 'W/"v1"'


def test_get_devices_returns_none_on_304(monkeypatch):
    """304 means 'what you have is current', so the caller keeps its snapshot."""
    from therm.api import _NotModified

    client = _client()

    def fake_request(path, etag=None):
        raise _NotModified(etag)

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.get_devices() is None


def test_missing_devices_array_is_an_error(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_request", lambda path, etag=None: (200, {"oops": 1}, None))
    with pytest.raises(ThermoworksError):
        client.get_devices()


def test_get_limits_reads_polling_advice(monkeypatch):
    client = _client()
    payload = {
        "plan": {"apiTier": "free"},
        "rateLimit": {"perMinute": 4, "perDay": 3000},
        "recommendedPollSeconds": 60,
    }
    monkeypatch.setattr(client, "_request", lambda path, etag=None: (200, payload, None))
    limits = client.get_limits()
    assert limits.per_minute == 4
    assert limits.per_day == 3000
    assert limits.recommended_poll_seconds == 60
    assert limits.api_tier == "free"
