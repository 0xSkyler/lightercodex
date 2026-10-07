#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo. Installation enables the service but does not start live trading.' >&2
    exit 1
fi
task_source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_app_dir=/opt/lighter-scalper
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3.12 python3.12-venv git ca-certificates chrony
systemctl enable --now chrony
if [[ "$(timedatectl show --property=NTPSynchronized --value)" != yes ]]; then
    echo 'Clock synchronization is not confirmed. Check chronyc tracking before starting.' >&2
fi
if ! id lighter-scalper >/dev/null 2>&1; then
    useradd --system --home /var/lib/lighter-scalper --shell /usr/sbin/nologin lighter-scalper
fi
if [[ "$task_source_dir" != "$task_app_dir" ]]; then
    if [[ -e "$task_app_dir" ]]; then
        echo 'Existing /opt/lighter-scalper found; use its deploy/update.sh to preserve local state.' >&2
        exit 1
    fi
    git -C "$task_source_dir" rev-parse --verify HEAD >/dev/null
    git clone --no-local "$task_source_dir" "$task_app_dir"
    git -C "$task_app_dir" remote set-url origin https://github.com/0xSkyler/lightercodex.git
fi
cd "$task_app_dir"
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
chown -R root:root "$task_app_dir"
find src deploy .venv -type d -exec chmod 755 {} +
find src deploy -type f -exec chmod a+r {} +
install -d -m 700 -o lighter-scalper -g lighter-scalper /var/lib/lighter-scalper /var/log/lighter-scalper
if [[ ! -e /etc/lighter-scalper.env ]]; then
    install -m 600 -o root -g root .env.example /etc/lighter-scalper.env
fi
install -m 644 deploy/lighter-scalper.service /etc/systemd/system/lighter-scalper.service
systemctl daemon-reload
systemctl enable lighter-scalper.service
echo 'Installed. Configure /etc/lighter-scalper.env, then explicitly start with systemctl start lighter-scalper.'
