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
# The stop waits for reconciliation/flatten. Inspect failure rather than deleting the journal.
systemctl stop lighter-scalper.service
if [[ "$(systemctl show lighter-scalper --property=Result --value)" != success ]]; then
    echo 'Service did not stop cleanly; inspect exchange exposure before updating.' >&2
    exit 1
fi
git pull --ff-only
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
systemctl restart lighter-scalper.service
systemctl --no-pager status lighter-scalper.service
