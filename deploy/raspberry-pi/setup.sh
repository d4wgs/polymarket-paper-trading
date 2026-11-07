#!/usr/bin/env bash
# One-time setup on a Raspberry Pi (Raspberry Pi OS, 64-bit recommended).
#
#   cd ~/polymarket-paper
#   bash deploy/raspberry-pi/setup.sh
#
# It installs Python packages into .venv, then installs two background services that start on
# boot and restart themselves if they crash:
#   polymarket-bot        python run.py              (collects data into ./data on the Pi)
#   polymarket-dashboard  streamlit run dashboard.py (http://<pi>:8501 from any device on your Wi-Fi)
#
# Re-running it is safe (it just updates packages and the service files).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="$(whoami)"
PORT="${PORT:-8501}"
LOG="$APP_DIR/setup.log"

# Everything below is also written to setup.log, and a failure says which step broke.
exec > >(tee "$LOG") 2>&1
STEP="starting"
trap 'echo; echo "!!! SETUP FAILED during: $STEP"; echo "!!! Nothing was started. The full output is in $LOG"; echo "!!! Send the last 40 lines:  tail -40 $LOG"' ERR

echo "==> App folder: $APP_DIR   user: $RUN_USER   dashboard port: $PORT"
echo "==> System: $(uname -m), $(getconf LONG_BIT)-bit OS, $(python3 --version 2>&1), $(. /etc/os-release && echo "$PRETTY_NAME")"

STEP="checking the Python version"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'EOF' || { echo "Python 3.10 or newer is needed (found $PYV). Use Raspberry Pi OS Bookworm or newer."; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

if [ "$(getconf LONG_BIT)" = "32" ]; then
  echo
  echo "!!! This is a 32-bit operating system. The dashboard needs pyarrow (via Streamlit), which has no"
  echo "!!! ready-made package for 32-bit Raspberry Pi OS, so installing it fails or compiles for hours."
  echo "!!! Fix: re-flash the SD card with 'Raspberry Pi OS (64-bit)' in Raspberry Pi Imager, then run this again."
  echo "!!! (To try anyway: FORCE_32BIT=1 bash deploy/raspberry-pi/setup.sh)"
  [ "${FORCE_32BIT:-0}" = "1" ] || exit 1
fi

STEP="installing system packages (apt)"
echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip python3-dev sqlite3

STEP="creating the Python environment (.venv)"
echo "==> Creating the Python environment (.venv) and installing requirements (can take 5-15 min on a Pi)"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel

STEP="installing Python packages (pip install -r requirements.txt)"
# --prefer-binary: use ready-made packages instead of compiling; pyarrow must be ready-made (compiling
# it on a Pi takes hours), so fail fast with a clear message if none exists for this system.
.venv/bin/pip install --prefer-binary --only-binary=pyarrow -r requirements.txt
mkdir -p data

STEP="running the tests"
echo "==> Running the tests"
if ! .venv/bin/python -m unittest discover -s tests > "$APP_DIR/data/test_output.txt" 2>&1; then
  echo "!!! Some tests failed (see data/test_output.txt). Continuing; send me that file if the bot misbehaves."
fi
tail -3 "$APP_DIR/data/test_output.txt"

STEP="installing the services (systemd)"

echo "==> Installing the services"
sudo tee /etc/systemd/system/polymarket-bot.service >/dev/null <<EOF
[Unit]
Description=Polymarket paper trading bot
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=always
RestartSec=10
# run.py writes its own log to data/bot.log; this keeps the console output out of the journal's way
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/polymarket-dashboard.service >/dev/null <<EOF
[Unit]
Description=Polymarket paper trading dashboard (Streamlit)
Wants=network-online.target
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
# --server.address 0.0.0.0 lets other devices on your network open it (the Windows setup stays localhost-only)
ExecStart=$APP_DIR/.venv/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot.service polymarket-dashboard.service

echo
echo "==> Clock check (the bot times every round off this clock; 'System clock synchronized: yes' is required)"
timedatectl | grep -E "synchronized|NTP service|Time zone" || true

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Both services are running and will start again after every reboot."
echo "  Dashboard:   http://$IP:$PORT     (or http://$(hostname).local:$PORT)"
echo "  Bot log:     tail -f $APP_DIR/data/bot.log"
echo "  Status:      systemctl status polymarket-bot polymarket-dashboard"
