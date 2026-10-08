# Lighter BTC scalper

Python 3.12+ application for live mainnet BTC perpetual trading. It uses bounded microstructure windows to select a direction, enters with a price-capped IOC, and closes with a reduce-only IOC when the entire remaining position has positive estimated executable P&L after fees and buffers. Only one position is allowed. The application has no paper, testnet, shadow, or backtesting operating mode. Deterministic software tests never send exchange orders.

The current implementation and cloud development environment have been tested locally and against public mainnet data. **Authenticated signing-key validation, account-stream integration, leverage changes, and real order execution have not been validated with a funded account.** Those are outstanding acceptance checks; this repository is not a certified production release. The journal handles ambiguous submissions conservatively and can require manual exchange inspection. See [API verification](docs/api.md) and [validation evidence](docs/validation.md).

## Repository

```text
lightercodex/
├── src/scalper/
│   ├── main.py                 # run, flatten, status, doctor, account-info
│   ├── config.py               # explicit mainnet gates and validated settings
│   ├── lighter_client.py       # official native signing and API adapter
│   ├── market_data.py          # public/private streams and WS transaction transport
│   ├── orderbook.py            # absolute depth updates, continuity, VWAP, precision
│   ├── signals.py              # rolling deterministic microstructure scores
│   ├── strategy.py             # execution owner, risk priority, reconciliation
│   ├── state_machine.py        # atomic lifecycle transitions
│   ├── pnl.py                  # executable entire-position economics
│   ├── rate_limits.py          # sliding windows and exit reserves
│   ├── persistence.py          # durable intents, fills, SQLite, process lock
│   ├── metrics.py              # bounded latency samples and trade accounting
│   ├── logging_setup.py        # queued, rotating UTC logs
│   ├── dashboard.py            # local browser UI and guarded process controls
│   └── web/                    # packaged HTML, CSS, and JavaScript
├── tests/                     # math, transport, lifecycle, recovery, limits
├── deploy/                    # install.sh, update.sh, systemd unit
├── docs/                      # verified API contracts and validation record
├── .env.example
├── pyproject.toml
├── uv.lock
├── requirements.lock          # hashed runtime/build dependencies for pip
└── LICENSE
```

## Development setup

Use the existing isolated cloud checkout; a Git worktree is unnecessary unless explicitly requested. The cloud image already provides Python 3.12 and uv. These commands install the locked dependencies without changing the lockfile:

```bash
cd /workspace/lightercodex
UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen --group build --python python3
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/lighter-scalper doctor
.venv/bin/lighter-scalper status
```

Tests, lint, type checks, and CLI checks require no account credentials. `doctor` makes read-only mainnet requests, discovers BTC from current market metadata, and verifies an actual order-book snapshot and delta over WebSocket. It uses the officially documented `?readonly=true` public data connection, which is also supported in regions restricted from trading. This flag applies only to the market-data connection and does not create another bot operating mode.

Required network destinations are `mainnet.zklighter.elliot.ai` and PyPI's package hosts. `apidocs.lighter.xyz`, `docs.lighter.xyz`, and the official SDK GitHub repository support documentation and API verification. HTTPS proxy settings and the provided certificate trust are preserved. Never disable TLS verification.

## Lighter credentials and account inspection

Use a dedicated, funded BTC-only Lighter subaccount, a registered trading API key, and only one bot instance across all machines. Sharing the API key or L1-wide rate/quota capacity with another trading process invalidates this bot's assumptions. Wallet private keys and seed phrases are not needed by the running service.

The required credential fields are:

| Field | Meaning |
| --- | --- |
| `LIGHTER_ACCOUNT_INDEX` | Numeric index of the dedicated Lighter account |
| `LIGHTER_API_KEY_INDEX` | Registered API key slot, 3–254 |
| `LIGHTER_API_PRIVATE_KEY` | Lighter API signing private key: 40 bytes, 80 hexadecimal characters, optional `0x` |

Create/register the API key through Lighter's supported account UI or the current official [API key documentation](https://apidocs.lighter.xyz/docs/api-keys). The [SDK setup example](https://github.com/elliottech/lighter-python/blob/main/examples/system_setup.py) demonstrates generation and registration; review its current constructor and account settings rather than copying example credentials.

```bash
cp .env.example .env
chmod 600 .env
# Edit .env locally; never put credentials into Git or chat.
.venv/bin/lighter-scalper account-info
```

`account-info` requires only the three credentials. It reads the registered account's tier, fees, balance, and BTC position/IMF without configuring leverage or submitting a transaction. Use `current_taker_fee_tick` to set `EXPECTED_TAKER_FEE_TICK`. The official fee tick denominator is 1,000,000, so a tick value of 280 means a rate of 0.000280, or 2.8 bps. A missing fee field on a trade is documented as zero.

Account-position IMF has appeared as a fraction, percentage, or native 1/10000 ticks across API representations. Confirmation accepts only an exact match to the requested leverage in one of those three representations; it never uses tolerance or nearest-leverage inference. `ACCOUNT_IMF_SCALE` remains restricted to 1, 100, or 10000 for configuration compatibility. The native signed update always uses integer `10000 / LEVERAGE`; leverage must be exactly representable and within current BTC market constraints. Version 1 supports cross margin (`MARGIN_MODE=0`).

`LIGHTER_API_PRIVATE_KEY` must be an actual local signing key. A network-proxy placeholder cannot be used by the native signer. Supply it through a protected environment file or a suitable secure process-environment binding.

## Configuration

[.env.example](.env.example) lists every supported setting and safe installation defaults. The account credentials, leverage, account fee tick, and rate policy must be supplied. Keep the two live gates empty until an explicit live start.

| Settings | Behavior |
| --- | --- |
| `LEVERAGE`, `ACCOUNT_IMF_SCALE`, `MARGIN_MODE` | Configure/verify leverage only while flat; reject mismatched settings |
| `POSITION_MODE=fixed_margin`, `MARGIN_PER_TRADE_USD` | Notional = fixed margin × leverage |
| `POSITION_MODE=fixed_notional`, `FIXED_NOTIONAL_USD` | Fixed quote notional; quantity rounded down to native units |
| `EXPECTED_TAKER_FEE_TICK`, `FEE_TICK_SCALE` | Account fee verification and exact fee arithmetic |
| `MIN_PROFIT_USD`, `MIN_PROFIT_BPS` | Exit threshold is the larger of the two, strictly positive net P&L |
| `SAFETY_BUFFER_USD`, `EXECUTION_BUFFER_BPS` | Additional protection beyond book VWAP and trading fees |
| `MAX_ENTRY_SLIPPAGE_BPS` | Entry price cap; skip insufficient full-size liquidity |
| `MAX_NORMAL_EXIT_SLIPPAGE_BPS`, `MAX_EMERGENCY_EXIT_SLIPPAGE_BPS` | Normal and protection exit price caps |
| `MAX_LOSS_USD`, `MAX_ADVERSE_MOVE_BPS`, `MAX_HOLD_MS` | Always-active exposure protections |
| `MARKET_DATA_STALE_MS`, `ACCOUNT_STREAM_STALE_MS` | Block entries; reconcile/flatten exposure on unhealthy data |
| `RECONCILE_MS`, `ORDER_TIMEOUT_MS`, `REQUEST_TIMEOUT_MS` | Periodic authoritative checks and bounded uncertainty waits |
| `TX_PER_MINUTE`, `HTTP_READS_PER_MINUTE`, `EXIT_TX_RESERVE` | Explicit local caps with exit/recovery headroom |
| `INITIAL_VOLUME_QUOTA` | Plus/Premium: seed with currently verified available quota if accountLimits omits it |
| `MAX_ENTRIES_PER_MINUTE` | Maximum frequency, with no required trade count or winning-trade cooldown |
| `ENTRY_SCORE_THRESHOLD`, six signal weights | Auditable normalized directional score |
| `MAX_SPREAD_BPS`, `MAX_VOLATILITY_BPS`, `BOOK_LEVELS` | Spread, volatility, and depth filters |
| `DATA_DIR`, `LOG_DIR`, `LOG_LEVEL` | Local state and rotating logs |

Standard accounts share a 60-request-per-minute server bucket between reads and transactions. For example, `TX_PER_MINUTE=30` and `HTTP_READS_PER_MINUTE=40` are separate local upper bounds; the shared server budget also enforces the aggregate cap and reserves recovery capacity. Frequent reconciliation reduces actual possible trade frequency. Plus/Premium use weighted reads (24,000 units/minute at the documented base tier), so `HTTP_READS_PER_MINUTE` means **weighted units**, and must exceed the 1,600-unit recovery reserve. Configured transaction caps above the conservative current base allowance are rejected. Stakes that increase limits are not assumed automatically. Shared L1 quotas must be budgeted externally if another application uses them.

```bash
.venv/bin/lighter-scalper check-config
```

This checks local configuration only. Account tier, fees, metadata, leverage, orders, and positions are verified separately by the service at startup. Do not mistake a passing configuration check for authenticated exchange validation.

## Entry and GREEN calculation

The signal combines top-level book imbalance, aggressive trade-flow imbalance, micro-momentum at 100/250/500/1000 ms, BBO direction changes, microprice displacement, and directional volume acceleration. Components are weighted and normalized into a score in [-1, 1]. Positive scores select long; negative scores select short. Buffers are time bounded to ten seconds and count bounded; one second of fresh evidence warms the engine. There are no candles, remote inference, or autonomous parameter changes.

For a long, the bot sweeps bids; for a short, asks. It estimates the entire remaining position's closing VWAP within the normal slippage cap. With direction `s` (+1 long, -1 short), quantity `q`, entry `E`, closing VWAP `X`, and taker fee fraction `f`:

```text
gross = s × (X − E) × q
fees = (E + X) × q × f
buffer = X × q × EXECUTION_BUFFER_BPS / 10000 + SAFETY_BUFFER_USD
expected_net = gross − fees − buffer
threshold = max(MIN_PROFIT_USD, E × q × MIN_PROFIT_BPS / 10000)
GREEN = entire quantity executable AND expected_net > threshold
```

VWAP already incorporates depth/price impact; it is not charged a second time. Faster BBO updates can trigger an exit only if their advertised top-level size covers the entire position; new BBO prices are never combined with stale deeper liquidity. Emergency/loss protection takes precedence, followed by GREEN and holding-time protection. There is no minimum holding time or conventional take-profit target.

The state changes to pending before transmission. Normal orders use the official native signer and persistent WebSocket `jsonapi/sendtx`; HTTP is used for recovery or when the private connection is unavailable **before** sending. No transport fallback or blind resend occurs after a possibly transmitted order. Accepted transactions are not counted as fills. The private stream and authoritative account reads establish the actual remaining exposure. Every close is reduce-only and stays inside its configured slippage price cap.

Expected GREEN is an estimate at decision time. Fills can occur later at changed liquidity; realized results are recorded only after confirmed flat and matching entry/exit fills. Leverage changes margin requirements, not the price move needed to pay spread and fees.

## Linux deployment

### Browser dashboard for RustDesk

The dashboard provides Overview, Connection, Strategy, and Activity pages. It includes a real public BTC price stream, a chart of prices received during the dashboard session, available balance from account verification or the latest bot snapshot, confirmed trade history/P&L, position snapshots with freshness labels, local logs, and Start/Stop/Close BTC controls. An empty account or journal displays an empty state; no demo prices, trades, or profit figures are generated.

RustDesk controls another machine's desktop. Open **http://127.0.0.1:8787 in the browser on the VPS desktop you access through RustDesk**. This requires a graphical desktop and browser on that VPS; installing this application does not install RustDesk or a desktop environment. A shell-only VPS has no browser desktop to control. No inbound web firewall port is needed: the server listens only on loopback.

For an existing installation, run from the root VPS shell:

```bash
systemctl stop lighter-scalper
# Confirm BTC exposure is closed on Lighter before switching controllers.
cd /opt/lighter-scalper
git pull --ff-only
bash deploy/install-dashboard.sh
```

For a fresh VPS:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/0xSkyler/lightercodex.git
cd lightercodex
sudo bash deploy/install-dashboard.sh
```

The dashboard installer installs the application if needed, refuses an active legacy bot, disables the legacy bot service, and enables/starts `lighter-dashboard.service`. It reuses `/var/lib/lighter-scalper` and `/var/log/lighter-scalper`, preserving the execution journal. Existing `/etc/lighter-scalper.env` is copied to `/var/lib/lighter-scalper/dashboard.env` only on the first dashboard installation. The dashboard's editable settings thereafter live in **`/var/lib/lighter-scalper/dashboard.env`**, owned by the unprivileged service user with mode 600. Editing `/etc/lighter-scalper.env` does not change dashboard settings.

In the browser:

1. Open **Connection**, enter your three Lighter credential fields, and choose **Save & verify account**. Verification does not submit orders.
2. Choose **Use verified fee & tier limits** to fill the actual fee and conservative limits for a recognized tier. Review these settings, leverage, position size, loss limits, and IMF representation in **Strategy**. Supply a verified volume quota if Plus/Premium needs it. Save and validate.
3. Return to **Overview**, choose **Start trading**, and type `START LIVE` to authorize real-money execution. Verification expires after 15 minutes; recheck the account if prompted. The dashboard passes live gates only to that child process and does not save live consent in settings.
4. **Stop bot** sends SIGTERM and waits for the engine's reconciliation and protective flatten. **Close BTC exposure** requires typing `FLATTEN BTC`; it stops the controlled bot and invokes the existing explicit flatten workflow with the same account and journal. If a close is unconfirmed, inspect Lighter immediately.

Credentials are written atomically to a mode-600 file and never returned by the HTTP API. The browser does not save them to local/session storage. Saved keys and long hexadecimal payloads are redacted from activity output. This is filesystem protection, not encryption at rest. The HTTP boundary enforces loopback/Host checks, same-origin requests, a per-process session token, a content security policy, and bounded request size. There is no public HTTP login or remote-bind option. An unrelated bot with the same journal blocks dashboard mutations; only one account controller may run across machines.

The dashboard starts after reboot; **the trading bot does not automatically restart or resume trading**. Closing the browser leaves the dashboard and an already-started bot running. Stopping/restarting the dashboard service stops its controlled bot and attempts protective reconciliation. Do not start the legacy `lighter-scalper.service` while using the UI. If you switch back to the legacy service, stop/disable the dashboard first and deliberately update the legacy environment file from your chosen settings.

```bash
sudo systemctl status lighter-dashboard
sudo journalctl -u lighter-dashboard -f
sudo systemctl restart lighter-dashboard
sudo /opt/lighter-scalper/deploy/update.sh
```

The update script detects dashboard deployment and updates/restarts the dashboard while leaving trading stopped. It preserves settings and the journal. Activity shows output from controlled processes, or a bounded tail of saved application logs after restart. Durable bot logs are also under `/var/log/lighter-scalper`.

If RustDesk controls your own computer rather than a desktop on the VPS, open an SSH tunnel on that computer:

```bash
ssh -N -L 8787:127.0.0.1:8787 root@YOUR_VPS_IP
```

Keep the SSH connection open and visit `http://127.0.0.1:8787` in that computer's browser. Do not expose port 8787 publicly.

For local development, no credentials are needed to open the UI and view public prices:

```bash
.venv/bin/lighter-scalper --env .env ui
```

### CLI service deployment

Target Ubuntu 24.04 with Python 3.12, systemd, synchronized time, and a trading-permitted network region. The cloud workspace is a development environment; it is not a persistent trading VPS.

Commit and push the generated source and lockfiles to GitHub before using the GitHub installation workflow. The onboarding task creates local files; it does not publish a GitHub branch.

```bash
git clone https://github.com/0xSkyler/lightercodex.git
cd lightercodex
sudo ./deploy/install.sh
sudoedit /etc/lighter-scalper.env
chronyc tracking
timedatectl show --property=NTPSynchronized --value
```

The installer creates a dedicated user, a checkout at `/opt/lighter-scalper`, a virtual environment with hash-verified dependencies, state/log directories, and an enabled systemd unit. It preserves an existing environment file and does not start trading. The installation checkout must contain a commit. Run updates through the installed checkout's update script. The CLI service needs no Docker, Redis, GPU, browser, or desktop.

After account inspection and configuration validation, explicitly set both confirmations in the protected environment file:

```dotenv
LIVE_TRADING=true
I_UNDERSTAND_THIS_USES_REAL_FUNDS=YES
```

An explicit local live start is:

```bash
.venv/bin/lighter-scalper run
```

VPS service commands are:

```bash
sudo systemctl start lighter-scalper
sudo systemctl status lighter-scalper
sudo journalctl -u lighter-scalper -f
sudo systemctl restart lighter-scalper
sudo systemctl stop lighter-scalper
sudo /opt/lighter-scalper/deploy/update.sh
```

The unit uses a protected root-owned environment file, an unprivileged service user, restricted write paths, and bounded rotated logs. Configuration/authentication/manual-intervention failures use exit code 78, which prevents restart loops. Other failures restart after 15 seconds with a burst limit. Every restart reconciles exposure before entries; discovered exposure is flattened rather than treated as a new flat account. Restarting does not erase SQLite or replace credentials.

## Status, journal, and emergency stop

```bash
.venv/bin/lighter-scalper status
.venv/bin/lighter-scalper status --watch
/opt/lighter-scalper/.venv/bin/lighter-scalper status --data-dir /var/lib/lighter-scalper
```

Status is read-only and local. Inspect heartbeat age, stream readiness, lifecycle state, remaining quantity, executable close estimates, rate headroom, and rolling latency percentiles. A stale heartbeat is not proof the process is currently running. SQLite contains intents, deduplicated fills, complete trade records, and heartbeat state. Daily UTC summaries are written alongside the database. Recovered or incomplete-accounting trades are labeled explicitly and do not count as wins. Execution P&L excludes external deposits/withdrawals and separately assessed funding payments; inspect exchange account accounting for total cash changes.

SIGINT/SIGTERM waits for in-flight submission, reconciles pending orders, attempts a bounded reduce-only flatten, and closes transports/journal. To invoke an explicit local emergency flatten, stop the running instance first and use the same credentials and data directory:

```bash
.venv/bin/lighter-scalper flatten
```

On the VPS, keep the same service user and environment without exposing credentials on the command line:

```bash
sudo systemctl stop lighter-scalper
sudo systemd-run --wait --pipe --collect \
  -p User=lighter-scalper -p Group=lighter-scalper \
  -p EnvironmentFile=/etc/lighter-scalper.env \
  -p WorkingDirectory=/opt/lighter-scalper \
  -E DATA_DIR=/var/lib/lighter-scalper -E LOG_DIR=/var/log/lighter-scalper \
  /opt/lighter-scalper/.venv/bin/lighter-scalper --env /dev/null flatten
```

The flatten command cancels conflicting BTC orders only as part of the explicitly requested manual operation, uses authoritative remaining quantity, and confirms zero exposure/orders. The automatic service cancels only journal-owned orders. An ambiguous earlier submission remains a blocker until transaction/order status proves its outcome. Do not delete the journal to bypass uncertainty. If flatten cannot be confirmed, inspect and close BTC exposure through Lighter immediately before restarting the service.

## Troubleshooting

* `CONFIG_ERROR`: inspect `.env` file permissions and required fields; process variables override the file. Empty live gates deliberately prevent execution.
* `AUTH_ERROR`: verify the account/key slot and registered Lighter API signing key using `account-info`. Wallet keys are a different format.
* Public data succeeds but private WebSocket fails: confirm trading is permitted from the VPS region. Read-only public access does not prove trading access.
* Book nonce gap, disconnect, or stale data: entries stop; exposure goes through authoritative recovery. A fresh snapshot resets the book; offsets may jump across servers.
* `UNRESOLVED_ORDER_INTENT`: signed hash and client ID are in SQLite. Inspect the corresponding transaction and order on Lighter. Missing lookup results alone do not establish failure; do not submit the entry again.
* `UNKNOWN_BTC_ORDER`: another client used the dedicated account, or state was replaced. Automatic cancellation is blocked. Inspect the order and use the explicit flatten command if appropriate.
* `RATE_LIMIT_ERROR`: reduce frequency and account for shared L1 usage. Standard reads and transactions share a bucket; Plus/Premium endpoint weights matter.
* Missing/degraded profit metrics: incomplete fill/fee accounting is deliberately excluded from win totals. Review actual fills and funding in the exchange account history.

The official SDK currently emits two WebSocket deprecation warnings during import. The bot uses aiohttp for its own stream transport; warnings remain visible during validation.
