"""Loopback-only browser dashboard. The execution engine remains a separate process."""

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import sys
import tempfile
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import lighter
from aiohttp import web

from scalper.config import MAINNET, WS_URL, ConfigError, credentials, load_config, read_environment
from scalper.lighter_client import checked
from scalper.market_data import websocket_proxy
from scalper.orderbook import Market, OrderBook
from scalper.persistence import local_status

SECRET = "LIGHTER_API_PRIVATE_KEY"
DEFAULTS = {
    "LIVE_TRADING": "",
    "I_UNDERSTAND_THIS_USES_REAL_FUNDS": "",
    "LIGHTER_URL": MAINNET,
    "MARKET": "BTC",
    "LIGHTER_ACCOUNT_INDEX": "",
    "LIGHTER_API_KEY_INDEX": "",
    SECRET: "",
    "FEE_TICK_SCALE": "1000000",
    "ACCOUNT_IMF_SCALE": "1",
    "EXPECTED_TAKER_FEE_TICK": "",
    "TX_PER_MINUTE": "",
    "HTTP_READS_PER_MINUTE": "",
    "INITIAL_VOLUME_QUOTA": "",
    "EXIT_TX_RESERVE": "4",
    "MAX_ENTRIES_PER_MINUTE": "20",
    "LEVERAGE": "",
    "MARGIN_MODE": "0",
    "POSITION_MODE": "fixed_margin",
    "MARGIN_PER_TRADE_USD": "10",
    "FIXED_NOTIONAL_USD": "250",
    "MIN_PROFIT_USD": "0.01",
    "MIN_PROFIT_BPS": "0.1",
    "SAFETY_BUFFER_USD": "0.01",
    "EXECUTION_BUFFER_BPS": "0.2",
    "MAX_ENTRY_SLIPPAGE_BPS": "1",
    "MAX_NORMAL_EXIT_SLIPPAGE_BPS": "1",
    "MAX_EMERGENCY_EXIT_SLIPPAGE_BPS": "20",
    "MAX_ADVERSE_MOVE_BPS": "10",
    "MAX_LOSS_USD": "1",
    "MAX_HOLD_MS": "5000",
    "MAX_SPREAD_BPS": "1",
    "MAX_VOLATILITY_BPS": "5",
    "MARKET_DATA_STALE_MS": "500",
    "ACCOUNT_STREAM_STALE_MS": "15000",
    "RECONCILE_MS": "10000",
    "ORDER_TIMEOUT_MS": "3000",
    "REQUEST_TIMEOUT_MS": "2000",
    "ENTRY_SCORE_THRESHOLD": "0.65",
    "BOOK_IMBALANCE_WEIGHT": "1",
    "TRADE_FLOW_WEIGHT": "1",
    "MICRO_MOMENTUM_WEIGHT": "1",
    "BBO_MOMENTUM_WEIGHT": "0.5",
    "MICROPRICE_WEIGHT": "0.5",
    "VOLUME_ACCEL_WEIGHT": "0.5",
    "BOOK_LEVELS": "10",
    "MAX_BOOK_LEVELS": "5000",
    "LOG_LEVEL": "INFO",
}
EDITABLE = set(DEFAULTS) - {
    "LIVE_TRADING",
    "I_UNDERSTAND_THIS_USES_REAL_FUNDS",
    "LIGHTER_URL",
    "MARKET",
    "FEE_TICK_SCALE",
    "MARGIN_MODE",
}
REQUIRED = (
    "LIGHTER_ACCOUNT_INDEX",
    "LIGHTER_API_KEY_INDEX",
    SECRET,
    "LEVERAGE",
    "EXPECTED_TAKER_FEE_TICK",
    "TX_PER_MINUTE",
    "HTTP_READS_PER_MINUTE",
)
WEB_DIR = Path(__file__).with_name("web")


class DashboardError(ValueError):
    pass


def redact(value: str, private: str = "") -> str:
    for key in (private, private.removeprefix("0x")):
        if key:
            value = value.replace(key, "[REDACTED]")
    return re.sub(r"\b(?:0x)?[0-9a-fA-F]{64,}\b", "[REDACTED]", value)


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path

    def values(self) -> dict[str, str]:
        if self.path.is_symlink():
            raise DashboardError("The dashboard settings file must not be a symbolic link.")
        return {**DEFAULTS, **{k: v or "" for k, v in read_environment(str(self.path), {}).items()}}

    def public(self) -> dict[str, Any]:
        values = self.values()
        missing = [name for name in REQUIRED if not values.get(name)]
        return {
            "values": {k: values[k] for k in EDITABLE if k != SECRET},
            "key_saved": bool(values.get(SECRET)),
            "missing": missing,
        }

    def fingerprint(self) -> str:
        values = self.values()
        return hashlib.sha256(
            json.dumps([values.get(k) for k in REQUIRED[:3]]).encode()
        ).hexdigest()

    def save(self, changes: dict[str, Any]) -> None:
        if not isinstance(changes, dict) or set(changes) - EDITABLE:
            raise DashboardError("Unknown configuration field.")
        values = self.values()
        for name, value in changes.items():
            if not isinstance(value, str) or len(value) > 128:
                raise DashboardError("Configuration values must be short text fields.")
            value = value.strip()
            if not re.fullmatch(r"[a-zA-Z0-9_.+\-]*", value):
                raise DashboardError("Configuration values contain unsupported characters.")
            # An empty password input preserves a saved key. The secret is never returned.
            if name == SECRET and not value:
                continue
            values[name] = value
        if values[SECRET]:
            if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{80}", values[SECRET]):
                raise DashboardError("Use the 80-character Lighter API signing key.")
        for name, lower, upper in (
            ("LIGHTER_ACCOUNT_INDEX", 0, 2**48 - 1),
            ("LIGHTER_API_KEY_INDEX", 3, 254),
        ):
            if values[name]:
                try:
                    number = int(values[name])
                except ValueError:
                    raise DashboardError(f"{name} must be an integer.") from None
                if not lower <= number <= upper:
                    raise DashboardError(f"{name} must be between {lower} and {upper}.")
        if all(values.get(name) for name in REQUIRED):
            load_config(None, environ=values)
        # Real-money consent is per action, never a persistent dashboard setting.
        values.update(LIVE_TRADING="", I_UNDERSTAND_THIS_USES_REAL_FUNDS="")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                for name, value in sorted(values.items()):
                    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                    output.write(f"{name}='{escaped}'\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def journal_view(directory: Path) -> dict[str, Any]:
    """Read real records only, without creating or changing an execution journal."""
    result: dict[str, Any] = {"status": local_status(directory), "trades": []}
    path = directory / "journal.sqlite3"
    if path.exists():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as db:
            result["trades"] = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM trades ORDER BY json_extract(payload,'$.utc_completed') DESC LIMIT 100"
                )
            ]
    return result


def saved_activity(directory: Path, private: str) -> list[dict[str, str]]:
    """Bounded tail of durable application logs, including after a dashboard restart."""
    path = directory / "scalper.jsonl"
    if not path.is_file():
        return []
    with path.open("rb") as file:
        size = file.seek(0, os.SEEK_END)
        file.seek(max(0, size - 65536))
        lines = file.read(65536).decode(errors="replace").splitlines()
    result = []
    for line in lines[-100:]:
        try:
            event = json.loads(line)
            result.append(
                {
                    "utc": str(event["utc"]),
                    "text": redact(f"{event.get('level', '')} {event['event']}", private)[:2000],
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    return result


class MarketFeed:
    def __init__(self) -> None:
        self.current: dict[str, Any] = {"connected": False}
        self.samples: deque[dict[str, float]] = deque(maxlen=1800)
        self.task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        delay = 1
        while True:
            try:
                config = lighter.Configuration(host=MAINNET)
                config.ssl_ca_cert = os.environ.get("SSL_CERT_FILE")
                async with lighter.ApiClient(configuration=config) as api:
                    details = checked(
                        await lighter.OrderApi(api).order_book_details(
                            filter="perp", _request_timeout=10
                        )
                    )
                    market = Market.discover(details["order_book_details"])
                url = WS_URL + "?readonly=true"
                book = OrderBook(1)
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.ws_connect(url, heartbeat=15, **websocket_proxy(url)) as ws:
                        async for event in ws:
                            if event.type != aiohttp.WSMsgType.TEXT:
                                raise DashboardError("Market connection closed.")
                            msg = json.loads(event.data)
                            kind = msg.get("type")
                            if kind == "connected":
                                await ws.send_json(
                                    {"type": "subscribe", "channel": f"ticker/{market.index}"}
                                )
                            elif kind == "ping":
                                await ws.send_json({"type": "pong"})
                            elif kind in ("subscribed/ticker", "update/ticker"):
                                ticker = msg["ticker"]
                                book.update(
                                    {
                                        "bids": [ticker["b"]],
                                        "asks": [ticker["a"]],
                                        "offset": int(msg["nonce"]),
                                    },
                                    time.monotonic_ns(),
                                    snapshot=True,
                                )
                                now = time.time()
                                mid = float((book.bid + book.ask) / 2)
                                self.current = {
                                    "connected": True,
                                    "received_at": now,
                                    "bid": str(book.bid),
                                    "ask": str(book.ask),
                                    "mid": mid,
                                    "spread_bps": str(book.spread_bps),
                                    "market_index": market.index,
                                    "min_size": str(market.min_size),
                                    "min_notional": str(market.min_notional),
                                }
                                if not self.samples or now - self.samples[-1]["time"] >= 1:
                                    self.samples.append({"time": now, "value": mid})
                                delay = 1
                            elif kind == "error":
                                raise DashboardError("Public market data unavailable.")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.current["connected"] = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


class Controller:
    def __init__(self, settings: Settings, data_dir: Path, log_dir: Path) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.command = ""
        self.lock = asyncio.Lock()
        self.busy = ""
        self.stopping = False
        self.last_exit: int | None = None
        self.lines: deque[dict[str, str]] = deque(maxlen=100)
        self.account: dict[str, Any] | None = None
        self.account_fingerprint = ""
        self.account_checked_at: float | None = None
        self.market = MarketFeed()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def external_owner(self) -> bool:
        path = self.data_dir / "execution.lock"
        if not path.exists():
            return False
        with path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return not self.running
        return False

    def idle(self) -> None:
        if self.running or self.external_owner():
            raise DashboardError(
                "Stop the running bot before changing settings or checking the account."
            )

    def environment(self, live: bool = False) -> dict[str, str]:
        values = self.settings.values()
        env = {k: v for k, v in os.environ.items() if k not in values}
        env.update(DATA_DIR=str(self.data_dir), LOG_DIR=str(self.log_dir), PYTHONUNBUFFERED="1")
        env.update(
            LIVE_TRADING="true" if live else "",
            I_UNDERSTAND_THIS_USES_REAL_FUNDS="YES" if live else "",
        )
        return env

    async def spawn(self, command: str, live: bool = False) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scalper.main",
            "--env",
            str(self.settings.path),
            command,
            env=self.environment(live),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def diagnose(self, command: str) -> str:
        process = await self.spawn(command)
        try:
            async with asyncio.timeout(45):
                output, _ = await process.communicate()
        except (TimeoutError, asyncio.CancelledError):
            process.kill()  # Only doctor/account-info: these commands cannot send orders.
            await process.wait()
            raise DashboardError(
                "The read-only account check timed out. Check your network and key."
            ) from None
        text = redact(output.decode(errors="replace"), self.settings.values()[SECRET])
        if process.returncode:
            raise DashboardError(text[-1500:].strip() or "Account verification failed.")
        # Upstream import warnings can precede the JSON.
        start = text.find("{")
        return text[start:] if start >= 0 else text

    async def check_account(self) -> dict[str, Any]:
        self.idle()
        credentials(self.settings.values())
        self.account = None
        data = json.loads(await self.diagnose("account-info"))
        self.account = data
        self.account_fingerprint = self.settings.fingerprint()
        self.account_checked_at = time.time()
        return data

    async def start(self) -> None:
        self.idle()
        config = load_config(str(self.settings.path), environ={})
        if not self.account or self.account_fingerprint != self.settings.fingerprint():
            raise DashboardError("Verify the saved Lighter account before starting live trading.")
        if time.time() - (self.account_checked_at or 0) > 900:
            raise DashboardError(
                "Account verification expired. Check the account again before starting."
            )
        if int(self.account["current_taker_fee_tick"]) != config.expected_taker_fee_tick:
            raise DashboardError("Expected taker fee tick must match the verified account fee.")
        if self.reader:
            await self.reader
        self.command = "run"
        self.process = await self.spawn("run", live=True)
        self.last_exit = None
        self.reader = asyncio.create_task(self._read_process(self.process))

    async def _read_process(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout
        pending = ""
        while chunk := await process.stdout.read(4096):
            pending += chunk.decode(errors="replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self._line(line)
            if len(pending) > 8192:
                self._line(pending)
                pending = ""
        if pending:
            self._line(pending)
        self.last_exit = await process.wait()

    def _line(self, value: str) -> None:
        self.lines.append(
            {
                "utc": datetime.now(UTC).isoformat(),
                "text": redact(value, self.settings.values()[SECRET])[:2000],
            }
        )

    async def stop(self) -> None:
        if self.external_owner():
            raise DashboardError(
                "Another service owns the bot. Stop lighter-scalper.service on the VPS first."
            )
        if not self.running:
            return
        assert self.process
        self.stopping = True
        try:
            # A manual flatten has no trading loop and must finish its order reconciliation.
            if self.command != "flatten":
                self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=175)
            except TimeoutError:
                raise DashboardError(
                    "Shutdown is still pending. Inspect BTC exposure on Lighter; the bot was not force-killed."
                ) from None
            if self.reader:
                await self.reader
            if self.process.returncode:
                raise DashboardError(
                    "The bot stopped with an error. Inspect Lighter exposure and the activity log."
                )
        finally:
            self.stopping = False

    async def flatten(self) -> None:
        load_config(str(self.settings.path), environ={})
        await self.stop()
        self.command = "flatten"
        self.process = await self.spawn("flatten", live=True)
        self.reader = asyncio.create_task(self._read_process(self.process))
        # Never cancel or retry a flatten merely because an HTTP client disconnects.
        await self.reader
        if self.process.returncode:
            raise DashboardError(
                "Flatten could not be confirmed. Inspect BTC exposure on Lighter immediately."
            )

    async def state(self) -> dict[str, Any]:
        try:
            journal = await asyncio.to_thread(journal_view, self.data_dir)
        except (OSError, sqlite3.Error, ValueError):
            journal = {"status": {"status": "journal unavailable"}, "trades": []}
        status = journal["status"]
        heartbeat = status.get("heartbeat_utc_ns")
        age = max(0, (time.time_ns() - heartbeat) / 1e9) if heartbeat else None
        fresh = age is not None and age < 5
        account = self.account if self.account_fingerprint == self.settings.fingerprint() else None
        market = dict(self.market.current)
        market["connected"] = bool(
            market.get("connected") and time.time() - market.get("received_at", 0) < 5
        )
        activity = list(self.lines)
        if not activity:
            with contextlib.suppress(OSError):
                activity = await asyncio.to_thread(
                    saved_activity, self.log_dir, self.settings.values()[SECRET]
                )
        return {
            "process": {
                "running": self.running,
                "external": self.external_owner(),
                "stopping": self.stopping,
                "busy": self.busy,
                "last_exit": self.last_exit,
            },
            "bot": status,
            "heartbeat_age": age,
            "heartbeat_fresh": fresh,
            "trades": journal["trades"],
            "logs": activity,
            "account": account,
            "account_checked_at": self.account_checked_at if account else None,
            "market": market,
            "price_history": list(self.market.samples),
            "settings": self.settings.public(),
        }


CONTROL = web.AppKey("controller", Controller)
TOKEN = web.AppKey("csrf", str)


@web.middleware
async def security(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    if request.remote not in ("127.0.0.1", "::1") or not re.fullmatch(
        r"(?:localhost|127\.0\.0\.1)(?::\d+)?", request.host
    ):
        raise web.HTTPForbidden(text="This dashboard is only available on localhost.")
    origin = request.headers.get("Origin")
    if request.headers.get("Sec-Fetch-Site") == "cross-site" or (
        origin and origin != f"http://{request.host}"
    ):
        raise web.HTTPForbidden(text="Cross-origin access is blocked.")
    if request.path.startswith("/api/") and request.path != "/api/bootstrap":
        if not hmac.compare_digest(
            request.headers.get("X-Dashboard-Token", ""), request.app[TOKEN]
        ):
            raise web.HTTPForbidden(text="Reload the dashboard to renew its session.")
    if request.method != "GET" and (
        origin != f"http://{request.host}" or request.content_type != "application/json"
    ):
        raise web.HTTPForbidden(text="Use the dashboard controls to perform this action.")
    try:
        response = await handler(request)
    except (DashboardError, ConfigError) as error:
        response = web.json_response({"error": str(error)}, status=400)
    except (ValueError, TypeError, KeyError):
        response = web.json_response(
            {"error": "Invalid request or response. Check the selected settings."}, status=400
        )
    except web.HTTPException:
        raise
    except Exception:
        response = web.json_response(
            {"error": "The operation failed. Check filesystem permissions and server logs."},
            status=500,
        )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        }
    )
    return response


async def bootstrap(request: web.Request) -> web.Response:
    return web.json_response(
        {"token": request.app[TOKEN], "settings": request.app[CONTROL].settings.public()}
    )


async def state(request: web.Request) -> web.Response:
    return web.json_response(await request.app[CONTROL].state())


async def action(request: web.Request) -> web.Response:
    controller = request.app[CONTROL]
    name = request.match_info["action"]
    if name not in ("settings", "account", "validate", "start", "stop", "flatten"):
        raise web.HTTPNotFound()
    body = await request.json()
    if not isinstance(body, dict):
        raise DashboardError("Expected a configuration object.")
    if controller.lock.locked():
        return web.json_response(
            {"error": f"Wait for {controller.busy or 'the current operation'} to finish."},
            status=409,
        )
    async with controller.lock:
        controller.busy = name
        try:
            if name == "settings":
                controller.idle()
                await asyncio.to_thread(controller.settings.save, body.get("values", {}))
                return web.json_response(
                    {
                        "message": "Settings saved securely.",
                        "settings": controller.settings.public(),
                    }
                )
            if name == "account":
                result = await controller.check_account()
                return web.json_response(
                    {"message": "Lighter account verified. No orders were sent.", "account": result}
                )
            if name == "validate":
                load_config(str(controller.settings.path), environ={})
                return web.json_response(
                    {"message": "Configuration valid. Ready for account verification."}
                )
            if name == "start":
                if body.get("confirmation") != "START LIVE":
                    raise DashboardError("Confirm START LIVE to authorize real-money trading.")
                await controller.start()
                return web.json_response(
                    {"message": "Bot launched. Reconciling the account before trading."}
                )
            if name == "stop":
                await controller.stop()
                return web.json_response(
                    {"message": "Bot stopped. Review the final exposure status below."}
                )
            if body.get("confirmation") != "FLATTEN BTC":
                raise DashboardError(
                    "Confirm FLATTEN BTC to authorize closing BTC exposure and cancelling BTC orders."
                )
            await controller.flatten()
            return web.json_response({"message": "BTC exposure and pending orders confirmed zero."})
        finally:
            controller.busy = ""


async def asset(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "index.html")
    types = {
        "index.html": "text/html",
        "dashboard.css": "text/css",
        "dashboard.js": "text/javascript",
    }
    if name not in types:
        raise web.HTTPNotFound()
    return web.Response(body=(WEB_DIR / name).read_bytes(), content_type=types[name])


def create_app(
    controller: Controller, *, market_feed: bool = True, guard: bool = True
) -> web.Application:
    app = web.Application(
        middlewares=[security], client_max_size=16384, handler_args={"handler_cancellation": False}
    )
    app[CONTROL] = controller
    app[TOKEN] = secrets.token_urlsafe(32)

    async def lifecycle(_: web.Application) -> AsyncIterator[None]:
        file = None
        if guard:
            controller.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            file = (controller.data_dir / "dashboard.lock").open("a")
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                file.close()
                raise DashboardError("Another dashboard owns this data directory.") from None
        if market_feed:
            controller.market.task = asyncio.create_task(controller.market.run())
        try:
            yield
        finally:
            if controller.market.task:
                controller.market.task.cancel()
                await asyncio.gather(controller.market.task, return_exceptions=True)
            with contextlib.suppress(DashboardError):
                await controller.stop()
            if file:
                file.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", asset)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/{action}", action)
    return app


def serve(env_path: str, port: int, data: str | None = None, logs: str | None = None) -> None:
    if not 1024 <= port <= 65535:
        raise ConfigError("Dashboard port must be between 1024 and 65535.")
    settings = Settings(Path(env_path).absolute())
    values = settings.values()
    controller = Controller(
        settings,
        Path(data or values.get("DATA_DIR") or "data").resolve(),
        Path(logs or values.get("LOG_DIR") or "logs").resolve(),
    )
    print(
        f"Open http://127.0.0.1:{port} in the browser on your RustDesk desktop. Trading starts only from the dashboard controls."
    )
    web.run_app(
        create_app(controller), host="127.0.0.1", port=port, access_log=None, shutdown_timeout=180
    )
