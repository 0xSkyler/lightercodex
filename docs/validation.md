# Validation evidence

Validated in the current cloud machine on 2026-10-07 with Python 3.12.14, uv 0.12.19, and lighter-sdk 1.1.6.

| Check | Result |
| --- | --- |
| `uv sync --frozen --group build --python python3` | Passed; repeat execution reuses installed dependencies and preserves the lockfile |
| Hashed `requirements.lock` installation in a clean virtual environment | Passed with TLS/checksum verification retained |
| `python -m pip install --no-deps --no-build-isolation .` in the clean environment | Passed; installed console entry point works |
| Native SDK signer loading | Passed on Linux x86_64 |
| `pytest -q` | **61 passed**, no failures/skips/expected failures; two upstream SDK WebSocket deprecation warnings |
| `ruff check src tests` and format check | Passed |
| `mypy src` | Passed for all 14 source modules |
| `lighter-scalper doctor` | Passed against current public mainnet metadata, actual book snapshot/delta, BBO, and individual-trade streams |
| CLI help and local status | Passed from development and clean installed environments |
| Missing live confirmations/credentials | `run` and `account-info` fail closed with exit code 78 |
| Deployment shell syntax | `bash -n deploy/install.sh deploy/update.sh` passed |
| systemd unit structure | `systemd-analyze verify` passed with ExecStart mapped to the actual cloud CLI path in a temporary unit |

Tests exercise exact long/short P&L, full-size depth VWAP, fee/buffer subtraction, strict profit thresholds, precision caps, absolute book updates and nonce continuity, stale data, symmetric signals, duplicate prevention, partial fills/exits, durable prepared/signed intents, unknown-order handling, startup exposure recovery, weighted/shared rate reserves, BBO liquidity, private-vs-REST transaction ordering, WebSocket subscriptions/auth request routing, and a full entry-to-fill-to-green-exit-to-confirmed-flat lifecycle using deterministic exchange test fixtures. No test sends exchange orders.

The public smoke check discovers BTC dynamically. Observed metadata: market ID 1; quantity precision 5; price precision 1; minimum 0.00007 BTC and $10 quote amount; minimum IMF 200 ticks (maximum leverage 50x). These values remain runtime-discovered.

Outstanding checks require outside prerequisites:

* The actual account index, registered API key index, and raw local API signing key are absent. Authenticated account inspection, private stream integration, native key registration matching, and leverage confirmation are therefore unrun against the user's account.
* Funded order execution and exchange latency have not been tested. The uploaded specification requires an explicit production-service start and prohibits real orders in software tests.
* The full root-level installer, chrony/systemd daemon operation, and 24/7 service behavior require the target Ubuntu VPS. Only the dependency/package installation path, shell syntax, and unit structure were validated here.
* Files are local to the empty repository checkout and have not been committed or pushed to GitHub.
* Cloud configuration saving is separate from publishing a snapshot. Fresh-task restoration was not tested.

Reusable install and startup instructions, Lighter network destinations, and missing runtime variable requirements are saved in the cloud environment draft for review. The development workflow is installed and validated; production readiness remains pending the account/VPS checks above.
