# Therm

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)

A dedicated temperature display for [ThermoWorks Cloud](https://cloud.thermoworks.com)
probes, built for a Raspberry Pi with a small touchscreen.

It polls the official ThermoWorks Cloud Integrations API and shows each probe as
a large, legible number you can read across a kitchen or a back yard. It is
designed to sit on a shelf and be glanced at.

```
┌──────────────────────────────────────────────┐
│ ● Connected              last poll 2:14pm    │
├──────────────────────────────────────────────┤
│ AMBIENT                          1 sensor    │
│                   243°                       │
│ updated: 2:14pm            target 275°  85%  │
├──────────────────────────────────────────────┤
│ MEAT 1                           4 sensors   │
│                   165°                       │
│ updated: 2:14pm            target 203°  100% │
├──────────────────────────────────────────────┤
│ MEAT 2                              STALE    │
│                   198°                       │
│ updated: 7:06am (7h 8m ago)             80%  │
└──────────────────────────────────────────────┘
```

## Design principle: it never makes a number up

If a reading is unavailable, old, or coming from a channel with no probe
attached, the display says so instead of showing a digit. A probe that is
unplugged still reports a plausible room temperature over the API, so channel
status is always checked, not just the value. Stale readings are shown with
their age, in amber, so a number on screen is never silently out of date.

This matters more than it sounds: a cooking thermometer showing a confident,
fabricated 203° is worse than one showing nothing at all.

## Requirements

- A ThermoWorks Cloud account with at least one Cloud-connected device
- An API key (free tier is plenty — see below)
- Python 3.11 or newer. **No pip packages.** The standard library only.
- For the kiosk display: a Raspberry Pi running Raspberry Pi OS Bookworm or
  newer, with `cage` and `chromium` (the installer handles both)

Developed against an RFX Gateway and two RFX Meat probes on a Pi 3 Model B with
an 800×480 DSI touchscreen, but nothing is specific to those.

## Get an API key

In the [ThermoWorks Cloud web app](https://cloud.thermoworks.com), go to
**Account Settings → Integrations** and create an API key. It looks like
`twk_…`, and **the full key is shown only once**, so copy it straight away.
You can scope a key to specific devices.

> **Note on the old password login.** Earlier versions of this and similar
> projects signed in with an email and password through Firebase. In November
> 2026 ThermoWorks enabled Firebase App Check on that endpoint, so password
> login now fails with `401 Firebase App Check token is invalid` and cannot be
> made to work from a third-party client. API keys are the supported path.
> If you are here because your old setup broke, that is why.

## Install on a Raspberry Pi

```bash
git clone https://github.com/chadwick-/thermowork-pi.git ~/therm
cd ~/therm
cp config.example.toml config.toml
nano config.toml          # paste your API key
python3 -m therm --check  # verify the key, list your devices
./scripts/install.sh
```

`--check` prints every device and channel on your account with the exact labels
to use in your config:

```
API key accepted. Tier: free
Rate limits: 4/min, 3000/day
Recommended poll interval: 60s

3 device(s):

  label: "RFX GATEWAY"  model: rfx gateway  battery: 85%  wifi: -59dBm
      channel 1: 243.6F  status=NORMAL  [ok]

  label: "RFX MEAT 1"  battery: 100%
      channel 1: 165.2F  status=NORMAL  [ok]
      channel 2: 168.1F  status=NORMAL  [ok]
      ...
```

The installer sets up two services:

| Service | Does |
|---|---|
| `therm.service` | Polls the API, serves the display on `127.0.0.1:8000` |
| `therm-kiosk.service` | Runs Chromium fullscreen via the `cage` compositor |

Both start on boot. `cage` is a minimal Wayland kiosk compositor — no desktop
environment is started, which keeps memory free on a 1GB Pi.

## Configuration

See [`config.example.toml`](config.example.toml) for the annotated version. The
part you will actually edit is the panel list:

```toml
[[panel]]
name = "Ambient"
device = "RFX GATEWAY"
channel = 1            # the gateway's wired probe port

[[panel]]
name = "Brisket"
device = "RFX MEAT 1"
channel = "min"
```

`channel` takes a channel number, or `"min"`, `"max"` or `"avg"`.

**Which channel for a meat probe?** RFX Meat probes have several sensors along
their length. The deepest one reads coolest when the probe is in meat, so
`"min"` tracks the centre of the cut — the number that decides doneness.
`"max"` sits nearer the surface. All three ignore channels with no probe or a
fault, so one bad sensor cannot skew the figure.

If you would rather not write the key to disk, leave `api_key` empty and set
`THERM_API_KEY` in the environment instead; it takes precedence.

### Polling

The default is every 60 seconds. The API caches responses for 15 seconds and
devices report about once a minute, so faster polling returns identical data and
just consumes your rate limit (free tier: 4/min, 3000/day). Therm uses
conditional requests, so unchanged data costs a `304` rather than a full
response, and it will slow itself down if the API asks it to.

## Touchscreen

- **Tap a panel** to give it the whole screen; tap again to go back.
- **Tap the status bar** to force an immediate refresh.

## Run it on your own machine

It is just a web page, so you do not need a Pi to work on it:

```bash
python3 -m therm --config config.toml
# then open http://127.0.0.1:8000/
```

Resize the browser window to 800×480 to see what the Pi will show.

## Troubleshooting

```bash
systemctl status therm therm-kiosk    # are both running?
journalctl -u therm -f                # what is the poller doing?
python3 -m therm --check               # is the key good?
curl -s localhost:8000/api/state | python3 -m json.tool
```

| Screen says | Means |
|---|---|
| `API key rejected` | Key is wrong, revoked or expired. Make a new one under Account Settings → Integrations, update `config.toml`, `sudo systemctl restart therm`. |
| `NOT FOUND` on a panel | No device with that label. Run `--check` and copy the label exactly. |
| `NO PROBE` | The channel is reporting no probe or a fault. Check the probe is seated. |
| `STALE` | The reading is older than `stale_after_seconds`. Normal for an idle RFX Meat probe, which transmits on temperature change rather than on a timer. |
| `Cannot reach ThermoWorks` | Network or API outage. Therm backs off and retries, keeping the last readings on screen. |
| `Display service not responding` | `therm.service` is down. Check `journalctl -u therm -n 50`. |

Blank screen with the service running? Check the kiosk:
`journalctl -u therm-kiosk -n 50`.

## Tests

```bash
python3 -m pytest        # 78 tests, no network required
```

## Not affiliated with ThermoWorks

This is a personal project. The Integrations API it uses is public and
documented at `https://cloud.thermoworks.com/api/docs/`, but this project is
not endorsed by or connected to ThermoWorks.

## License

MIT — see [LICENSE](LICENSE).
