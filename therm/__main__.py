"""Entry point: `python3 -m therm`.

Also provides `--check`, which verifies the API key and prints the devices and
channels on the account. Run that first: it tells you exactly what to put in
the [[panel]] sections of your config.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from .api import ApiClient, AuthError, ThermoworksError
from .config import Config, ConfigError, load
from .poller import Poller
from .server import make_server

_LOG = logging.getLogger("therm")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_devices(config: Config) -> int:
    """Verify credentials and dump the account's devices and channels."""
    client = ApiClient(api_key=config.api_key, base_url=config.base_url)

    try:
        limits = client.get_limits()
        print(f"API key accepted. Tier: {limits.api_tier or 'unknown'}")
        if limits.per_minute or limits.per_day:
            print(f"Rate limits: {limits.per_minute}/min, {limits.per_day}/day")
        if limits.recommended_poll_seconds:
            print(f"Recommended poll interval: {limits.recommended_poll_seconds}s")
        print()

        snapshot = client.get_devices()
    except AuthError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        print(
            "\nCreate a key in the ThermoWorks Cloud web app under\n"
            "Account Settings -> Integrations, then put it in your config.",
            file=sys.stderr,
        )
        return 1
    except ThermoworksError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    if snapshot is None or not snapshot.devices:
        print("No devices found on this account.")
        print("If the key is limited to specific devices, check that selection.")
        return 1

    print(f"{len(snapshot.devices)} device(s):\n")
    for device in snapshot.devices:
        bits = [f'label: "{device.display_name}"']
        if device.model:
            bits.append(f"model: {device.model}")
        if device.battery is not None:
            bits.append(f"battery: {int(device.battery)}%")
        if device.wifi_signal is not None:
            bits.append(f"wifi: {int(device.wifi_signal)}dBm")
        print("  " + "  ".join(bits))
        if not device.channels:
            print("      (no channels reported)")
        for channel in device.channels:
            health = "ok" if channel.healthy else "unusable"
            value = "--" if channel.value is None else f"{channel.value:.1f}"
            print(
                f"      channel {channel.number or channel.id}: "
                f"{value}{channel.units or ''}  "
                f"status={channel.status or '?'}  [{health}]"
            )
        print()

    print("Use the quoted label and a channel number in your config, e.g.:\n")
    first = snapshot.devices[0]
    print("  [[panel]]")
    print('  name = "Ambient"')
    print(f'  device = "{first.display_name}"')
    print("  channel = 1")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="therm",
        description="ThermoWorks Cloud temperature display.",
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the API key, list devices and channels, then exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    for warning in config.warnings:
        _LOG.warning("%s", warning)

    if args.check:
        try:
            return _print_devices(config)
        except KeyboardInterrupt:
            return 130

    _LOG.info("Loaded config from %s", config.source_path)
    _LOG.info(
        "Polling every %ss, flagging readings older than %ss",
        config.poll_seconds,
        config.stale_after_seconds,
    )

    poller = Poller(config)
    poller.start()

    try:
        httpd = make_server(config, poller)
    except OSError as exc:
        poller.stop()
        print(
            f"Could not bind {config.host}:{config.port}: {exc}\n"
            "Another process may already be using that port.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as exc:
        poller.stop()
        print(f"{exc}", file=sys.stderr)
        return 1

    _LOG.info("Display available at http://%s:%s/", config.host, config.port)

    stopping = threading.Event()

    def _shutdown(signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        _LOG.info("Received signal %s, shutting down", signum)
        # shutdown() blocks until serve_forever returns, so it cannot be
        # called from inside the handler's own thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        poller.stop()
        _LOG.info("Stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
