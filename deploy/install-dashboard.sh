#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo (or from your root VPS shell).' >&2
    exit 1
fi
task_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_app_dir=/opt/lighter-scalper
if [[ ! -d "$task_app_dir" ]]; then
    bash "$task_source_dir/deploy/install.sh"
elif [[ "$task_source_dir" != "$task_app_dir" ]]; then
    echo 'Existing installation found. Run git pull --ff-only in /opt/lighter-scalper, then run its deploy/install-dashboard.sh.' >&2
    exit 1
fi
cd "$task_app_dir"
if systemctl is-active --quiet lighter-scalper.service; then
    echo 'Stop the existing bot first: systemctl stop lighter-scalper. Confirm BTC exposure on Lighter, then rerun this installer.' >&2
    exit 1
fi
if systemctl is-active --quiet lighter-dashboard.service; then
    echo 'Dashboard is already active. Use deploy/update.sh to update it.' >&2
    exit 1
fi
# The dashboard runs unprivileged and owns only its settings, journal, logs, and child bot.
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
install -d -m 700 -o lighter-scalper -g lighter-scalper /var/lib/lighter-scalper /var/log/lighter-scalper
if [[ ! -e /var/lib/lighter-scalper/dashboard.env ]]; then
    install -m 600 -o lighter-scalper -g lighter-scalper /etc/lighter-scalper.env /var/lib/lighter-scalper/dashboard.env
fi
install -m 644 deploy/lighter-dashboard.service /etc/systemd/system/lighter-dashboard.service
systemctl disable lighter-scalper.service
systemctl daemon-reload
systemctl enable --now lighter-dashboard.service
echo 'Dashboard installed. Open http://127.0.0.1:8787 in the browser on your RustDesk desktop.'
echo 'Trading is stopped. Save/verify your credentials and configure the strategy in the dashboard.'
