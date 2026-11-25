"""Background polling of the ThermoWorks API.

Runs in its own thread so that serving the display never blocks on network
I/O. The previous version of this project drove a fresh asyncio event loop from
inside the UI's timer callback, which froze the screen on every slow request.

The last good snapshot is retained across failures. Panels keep showing their
last known values, and the age of that data is reported so the UI can show how
old it is rather than pretending it is current.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .api import (
    ApiClient,
    AuthError,
    RateLimitError,
    Snapshot,
    ThermoworksError,
    TransportError,
)
from .config import Config
from .state import build_state

_LOG = logging.getLogger(__name__)

# A rejected key will not fix itself; retry slowly so a revoked key does not
# burn the daily rate limit, but keep trying so the app recovers on its own
# once the user pastes a new one.
AUTH_RETRY_SECONDS = 300
MAX_BACKOFF_SECONDS = 300


class Poller:
    """Owns the API client, the poll loop and the current display state."""

    def __init__(self, config: Config, client: ApiClient | None = None) -> None:
        self._config = config
        self._client = client or ApiClient(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._poll_seconds = config.poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._snapshot: Snapshot | None = None
        self._connection: str = "starting"
        self._error: str | None = None
        self._last_success: datetime | None = None
        self._consecutive_failures = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="therm-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def refresh_now(self) -> None:
        """Ask the poll loop to fetch immediately (used by the UI's refresh)."""
        self._wake.set()

    # -- state -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Current display state. Safe to call from any thread."""
        with self._lock:
            snapshot = self._snapshot
            connection = self._connection
            error = self._error
            last_success = self._last_success

        # Data we hold but have not been able to confirm for several intervals
        # is reported as stale even while the connection itself looks fine.
        if connection == "ok" and last_success is not None:
            age = (datetime.now(timezone.utc) - last_success).total_seconds()
            if age > max(self._poll_seconds * 3, self._config.stale_after_seconds):
                connection = "stale"

        payload = build_state(
            config=self._config,
            snapshot=snapshot,
            connection=connection,  # type: ignore[arg-type]
            error=error,
            last_success=last_success,
        )
        # Report the interval actually in use, which the server may have raised.
        payload["pollSeconds"] = self._poll_seconds
        return payload

    def device_labels(self) -> list[str]:
        """Labels of every device on the account, for the setup helper."""
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return []
        return sorted(d.display_name for d in snapshot.devices)

    # -- internals ---------------------------------------------------------

    def _record_success(self, snapshot: Snapshot | None) -> None:
        with self._lock:
            # A 304 means "unchanged", so the snapshot we hold is still current.
            if snapshot is not None:
                self._snapshot = snapshot
            self._connection = "ok"
            self._error = None
            self._last_success = datetime.now(timezone.utc)
            self._consecutive_failures = 0

    def _record_failure(self, connection: str, message: str) -> None:
        with self._lock:
            self._connection = connection
            self._error = message
            self._consecutive_failures += 1

    def poll_once(self) -> float:
        """Perform one poll. Returns the number of seconds to wait next."""
        try:
            snapshot = self._client.get_devices()
        except AuthError as exc:
            _LOG.error("%s", exc)
            self._record_failure("auth_error", str(exc))
            return AUTH_RETRY_SECONDS
        except RateLimitError as exc:
            wait = exc.retry_after or self._poll_seconds * 2
            _LOG.warning("%s; waiting %ss", exc, wait)
            self._record_failure("error", str(exc))
            return float(wait)
        except TransportError as exc:
            _LOG.warning("%s", exc)
            self._record_failure("error", str(exc))
            return self._backoff()
        except ThermoworksError as exc:
            _LOG.warning("%s", exc)
            self._record_failure("error", str(exc))
            return self._backoff()
        except Exception as exc:  # noqa: BLE001 - last resort; must not kill the thread
            _LOG.exception("Unexpected error while polling")
            self._record_failure("error", f"Unexpected error: {exc}")
            return self._backoff()

        count = len(snapshot.devices) if snapshot else 0
        _LOG.info("poll ok (%s)", "304 unchanged" if snapshot is None else f"{count} devices")
        self._record_success(snapshot)
        return float(self._poll_seconds)

    def _backoff(self) -> float:
        with self._lock:
            failures = self._consecutive_failures
        # 1x, 2x, 4x the poll interval, capped.
        delay = self._poll_seconds * (2 ** min(failures - 1, 4))
        return float(min(delay, MAX_BACKOFF_SECONDS))

    def _run(self) -> None:
        # Ask the server how often it wants to be polled, and respect it if it
        # asks for less traffic than we planned.
        try:
            limits = self._client.get_limits()
            if limits.recommended_poll_seconds and limits.recommended_poll_seconds > self._poll_seconds:
                _LOG.info(
                    "Server recommends polling every %ss; raising interval from %ss",
                    limits.recommended_poll_seconds,
                    self._poll_seconds,
                )
                self._poll_seconds = limits.recommended_poll_seconds
            if limits.per_minute or limits.per_day:
                _LOG.info(
                    "API limits: %s/min, %s/day (tier %s)",
                    limits.per_minute,
                    limits.per_day,
                    limits.api_tier,
                )
        except AuthError as exc:
            # Surface this immediately rather than waiting for the first poll.
            _LOG.error("%s", exc)
            self._record_failure("auth_error", str(exc))
        except ThermoworksError as exc:
            _LOG.debug("Could not read /v1/me: %s", exc)

        while not self._stop.is_set():
            delay = self.poll_once()
            self._wake.clear()
            # Woken early by refresh_now(), or on timeout.
            self._wake.wait(timeout=delay)
