#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo 'Run with sudo.' >&2
    exit 1
fi
cd /opt/lighter-scalper
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo 'Tracked files have local modifications; resolve them before updating.' >&2
    exit 1
fi
task_service=lighter-scalper.service
if systemctl is-enabled --quiet lighter-dashboard.service; then
    task_service=lighter-dashboard.service
fi
# The stop waits for reconciliation/flatten. Inspect failure rather than deleting the journal.
systemctl stop "$task_service"
if [[ "$(systemctl show "$task_service" --property=Result --value)" != success ]]; then
    echo 'Service did not stop cleanly; inspect exchange exposure before updating.' >&2
    exit 1
fi
git pull --ff-only
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
if [[ "$task_service" == lighter-dashboard.service ]]; then
    install -m 644 deploy/lighter-dashboard.service /etc/systemd/system/lighter-dashboard.service
    systemctl daemon-reload
fi
systemctl restart "$task_service"
systemctl --no-pager status "$task_service"
if [[ "$task_service" == lighter-dashboard.service ]]; then
    echo 'Dashboard updated. Open localhost:8787; the bot remains stopped until you confirm a new live start.'
fi
