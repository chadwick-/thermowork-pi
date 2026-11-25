#!/usr/bin/env bash
#
# Install Therm as a pair of systemd services on a Raspberry Pi.
#
#   therm.service        polls ThermoWorks, serves the display on localhost
#   therm-kiosk.service  runs Chromium fullscreen via the cage compositor
#
# Run this on the Pi, from the directory containing the project:
#   ./scripts/install.sh
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
RUN_UID="$(id -u "$RUN_USER")"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
PORT="8000"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && die "Run this as your normal user, not root. It will call sudo where needed."

say "Checking Python"
python3 - <<'PY' || die "Python 3.11 or newer is required (Raspberry Pi OS Bookworm ships 3.11)."
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
python3 -c 'import tomllib, zoneinfo' || die "Python is missing tomllib/zoneinfo. Install python3-tzdata."
echo "Python $(python3 --version | cut -d' ' -f2) OK"

say "Checking config"
if [ ! -f "$PROJECT_DIR/config.toml" ]; then
    cp "$PROJECT_DIR/config.example.toml" "$PROJECT_DIR/config.toml"
    chmod 600 "$PROJECT_DIR/config.toml"
    warn "Created config.toml from the example. Add your API key before starting:"
    warn "  nano $PROJECT_DIR/config.toml"
    warn "Get a key from the ThermoWorks Cloud web app: Account Settings -> Integrations"
else
    # The file holds a credential; make sure it is not world-readable.
    chmod 600 "$PROJECT_DIR/config.toml"
    echo "config.toml present"
fi

say "Installing display packages"
MISSING=()
command -v cage     >/dev/null || MISSING+=(cage)
command -v chromium >/dev/null || command -v chromium-browser >/dev/null || MISSING+=(chromium)
command -v curl     >/dev/null || MISSING+=(curl)
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "Installing: ${MISSING[*]}"
    sudo apt-get update
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "cage, chromium and curl already present"
fi

CHROMIUM_BIN="$(command -v chromium || command -v chromium-browser)"
echo "Using browser: $CHROMIUM_BIN"

say "Installing systemd units"
for unit in therm.service therm-kiosk.service; do
    sed -e "s|^User=.*|User=$RUN_USER|" \
        -e "s|^Group=.*|Group=$RUN_GROUP|" \
        -e "s|/home/chad/therm|$PROJECT_DIR|g" \
        -e "s|/home/chad/\.config|$RUN_HOME/.config|g" \
        -e "s|Environment=HOME=.*|Environment=HOME=$RUN_HOME|" \
        -e "s|/run/user/1000|/run/user/$RUN_UID|g" \
        -e "s|/usr/bin/chromium |$CHROMIUM_BIN |" \
        "$PROJECT_DIR/systemd/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
    echo "  installed /etc/systemd/system/$unit"
done

# A lingering user session keeps XDG_RUNTIME_DIR alive for the compositor.
say "Enabling user session lingering for $RUN_USER"
sudo loginctl enable-linger "$RUN_USER" || warn "Could not enable lingering; the kiosk may fail to start on boot."

say "Reloading systemd"
sudo systemctl daemon-reload

say "Starting services"
sudo systemctl enable --now therm.service
sleep 3
if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    echo "therm.service is answering on port $PORT"
else
    warn "therm.service is not answering yet. Check: sudo journalctl -u therm -n 40"
fi
sudo systemctl enable --now therm-kiosk.service

cat <<EOF

Done.

  Status:   systemctl status therm therm-kiosk
  Logs:     journalctl -u therm -f
  Verify:   cd $PROJECT_DIR && python3 -m therm --check
  Restart:  sudo systemctl restart therm therm-kiosk

If the screen shows "API key rejected", put a valid key in
$PROJECT_DIR/config.toml and run: sudo systemctl restart therm
EOF
