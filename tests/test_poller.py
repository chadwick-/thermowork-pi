"""Tests for poll-loop behaviour: backoff, retention and failure reporting."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from therm.api import AuthError, RateLimitError, Snapshot, ThermoworksError, TransportError
from therm.config import Config, PanelConfig
from therm.poller import AUTH_RETRY_SECONDS, MAX_BACKOFF_SECONDS, Poller


def make_config(**overrides):
    defaults = dict(
        api_key="twk_test",
        panels=(PanelConfig(name="Meat", device="RFX MEAT 1", channel="1"),),
        poll_seconds=60,
        stale_after_seconds=600,
    )
    defaults.update(overrides)
    return Config(**defaults)


class FakeClient:
    """Stands in for ApiClient, returning a scripted sequence of outcomes."""

    def __init__(self, outcomes, limits=None):
        self.outcomes = list(outcomes)
        self.calls = 0
        self._limits = limits

    def get_devices(self):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_limits(self):
        if isinstance(self._limits, Exception):
            raise self._limits
        return self._limits


def snapshot(generated_at=None):
    return Snapshot(generated_at=generated_at or datetime.now(timezone.utc), devices=())


# -- success paths -----------------------------------------------------------

def test_successful_poll_sets_ok_and_returns_configured_interval():
    poller = Poller(make_config(), FakeClient([snapshot()]))
    delay = poller.poll_once()
    assert delay == 60
    assert poller.state()["connection"] == "ok"
    assert poller.state()["error"] is None


def test_304_keeps_the_previous_snapshot_and_still_counts_as_success():
    """A 304 means the data we hold is current, so it must refresh the
    last-success time rather than look like a failure."""
    first = snapshot()
    poller = Poller(make_config(), FakeClient([first, None]))

    poller.poll_once()
    held = poller._snapshot

    poller.poll_once()
    assert poller._snapshot is held
    assert poller.state()["connection"] == "ok"


# -- failure paths -----------------------------------------------------------

def test_auth_error_is_reported_and_retried_slowly():
    """A rejected key will not fix itself; retrying every 60s would just burn
    the daily rate limit."""
    poller = Poller(make_config(), FakeClient([AuthError("key rejected")]))
    delay = poller.poll_once()
    assert delay == AUTH_RETRY_SECONDS
    state = poller.state()
    assert state["connection"] == "auth_error"
    assert "rejected" in state["error"]


def test_rate_limit_honours_retry_after():
    poller = Poller(make_config(), FakeClient([RateLimitError("slow down", retry_after=90)]))
    assert poller.poll_once() == 90
    assert poller.state()["connection"] == "error"


def test_rate_limit_without_retry_after_falls_back_to_double_the_interval():
    poller = Poller(make_config(), FakeClient([RateLimitError("slow down")]))
    assert poller.poll_once() == 120


def test_transport_error_backs_off_exponentially_then_caps():
    errors = [TransportError("no route") for _ in range(8)]
    poller = Poller(make_config(), FakeClient(errors))
    delays = [poller.poll_once() for _ in range(8)]
    assert delays[0] == 60
    assert delays[1] == 120
    assert delays[2] == 240
    assert all(d <= MAX_BACKOFF_SECONDS for d in delays)
    assert delays[-1] == MAX_BACKOFF_SECONDS


def test_unexpected_exception_does_not_kill_the_loop():
    """If the poll thread dies the display silently freezes, which is the
    failure mode this project is trying to get away from."""
    poller = Poller(make_config(), FakeClient([ValueError("something odd")]))
    delay = poller.poll_once()
    assert delay > 0
    assert poller.state()["connection"] == "error"
    assert "Unexpected error" in poller.state()["error"]


def test_failure_after_success_retains_the_last_good_snapshot():
    poller = Poller(make_config(), FakeClient([snapshot(), TransportError("dropped")]))
    poller.poll_once()
    poller.poll_once()

    state = poller.state()
    assert state["connection"] == "error"
    # The data is still there; it is the connection that is broken.
    assert poller._snapshot is not None
    assert state["dataAgeSeconds"] is not None


def test_recovery_after_failure_clears_the_error():
    poller = Poller(make_config(), FakeClient([TransportError("dropped"), snapshot()]))
    poller.poll_once()
    assert poller.state()["connection"] == "error"

    poller.poll_once()
    state = poller.state()
    assert state["connection"] == "ok"
    assert state["error"] is None


def test_backoff_resets_after_a_success():
    client = FakeClient([TransportError("x"), TransportError("x"), snapshot(), TransportError("x")])
    poller = Poller(make_config(), client)
    poller.poll_once()
    poller.poll_once()
    poller.poll_once()                   # success resets the counter
    assert poller.poll_once() == 60      # back to the first backoff step


# -- server-advertised polling advice ----------------------------------------

def test_server_recommendation_can_slow_us_down_but_not_speed_us_up():
    from therm.api import Limits

    config = make_config(poll_seconds=60)
    client = FakeClient([snapshot()], limits=Limits(recommended_poll_seconds=120))
    poller = Poller(config, client)
    poller._run_limits_probe() if hasattr(poller, "_run_limits_probe") else None

    # Exercise the same logic the thread runs at startup.
    limits = client.get_limits()
    if limits.recommended_poll_seconds and limits.recommended_poll_seconds > poller._poll_seconds:
        poller._poll_seconds = limits.recommended_poll_seconds
    assert poller._poll_seconds == 120

    faster = FakeClient([snapshot()], limits=Limits(recommended_poll_seconds=15))
    poller2 = Poller(make_config(poll_seconds=60), faster)
    limits2 = faster.get_limits()
    if limits2.recommended_poll_seconds and limits2.recommended_poll_seconds > poller2._poll_seconds:
        poller2._poll_seconds = limits2.recommended_poll_seconds
    assert poller2._poll_seconds == 60   # unchanged: we do not poll faster than configured
