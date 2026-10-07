"""CLI: explicit live service, emergency flatten, local status, and read-only diagnostics."""

import argparse
import asyncio
import json
import os
import signal
import ssl
import sys
import time
from pathlib import Path

import aiohttp
import lighter
from dotenv import dotenv_values

from scalper.config import MAINNET, WS_URL, ConfigError, credentials, load_config, read_environment
from scalper.lighter_client import ExchangeError, LighterClient, checked
from scalper.logging_setup import configure
from scalper.market_data import websocket_proxy
from scalper.metrics import Metrics
from scalper.orderbook import Market, OrderBook
from scalper.persistence import Journal, ProcessLock, local_status
from scalper.state_machine import State
from scalper.strategy import Bot


async def doctor() -> None:
    """Read public mainnet metadata and an actual book snapshot/delta; never create a signer."""
    configuration = lighter.Configuration(host=MAINNET)
    configuration.ssl_ca_cert = os.environ.get("SSL_CERT_FILE")
    async with lighter.ApiClient(configuration=configuration) as api:
        metadata = checked(
            await lighter.OrderApi(api).order_book_details(filter="perp", _request_timeout=10.0)
        )
        market = Market.discover(metadata["order_book_details"])
    book = OrderBook()
    bbo = OrderBook(1)
    seen_snapshot = seen_delta = False
    seen_trade = seen_ticker = False
    async with aiohttp.ClientSession(
        trust_env=True, connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())
    ) as session:
        async with asyncio.timeout(20):
            public_url = WS_URL + "?readonly=true"
            async with session.ws_connect(
                public_url, heartbeat=5, **websocket_proxy(public_url)
            ) as ws:
                async for event in ws:
                    if event.type != aiohttp.WSMsgType.TEXT:
                        raise ExchangeError("PUBLIC_WEBSOCKET_CLOSED")
                    msg = json.loads(event.data)
                    kind = msg.get("type")
                    if kind == "connected":
                        for channel in ("order_book", "trade", "ticker"):
                            await ws.send_json(
                                {"type": "subscribe", "channel": f"{channel}/{market.index}"}
                            )
                    elif kind == "ping":
                        await ws.send_json({"type": "pong"})
                    elif kind in ("subscribed/order_book", "update/order_book"):
                        snapshot = kind == "subscribed/order_book"
                        book.update(msg["order_book"], time.monotonic_ns(), snapshot=snapshot)
                        seen_snapshot |= snapshot
                        seen_delta |= not snapshot
                        if seen_snapshot and seen_delta and seen_trade and seen_ticker:
                            print(
                                json.dumps(
                                    {
                                        "mainnet_public_api": "verified",
                                        "BTC_market_id": market.index,
                                        "book_snapshot_and_delta": "verified",
                                        "BBO_stream": "verified",
                                        "trade_stream": "verified",
                                        "bid": str(book.bid),
                                        "ask": str(book.ask),
                                        "size_decimals": market.size_decimals,
                                        "price_decimals": market.price_decimals,
                                        "min_size": str(market.min_size),
                                        "min_notional": str(market.min_notional),
                                    }
                                )
                            )
                            return
                    elif kind in ("subscribed/ticker", "update/ticker"):
                        ticker = msg["ticker"]
                        bbo.update(
                            {
                                "bids": [ticker["b"]],
                                "asks": [ticker["a"]],
                                "offset": int(msg["nonce"]),
                            },
                            time.monotonic_ns(),
                            snapshot=True,
                        )
                        seen_ticker = True
                    elif kind == "update/trade" and msg.get("trades"):
                        trade = msg["trades"][0]
                        if "trade_id" not in trade or "is_maker_ask" not in trade:
                            raise ExchangeError("TRADE_SCHEMA_ERROR")
                        seen_trade = True
                    elif kind == "error":
                        raise ExchangeError("PUBLIC_WEBSOCKET_ERROR")
    raise ExchangeError("PUBLIC_WEBSOCKET_NO_DATA")


async def account_info(path: str) -> None:
    """Inspect an authenticated account without configuring leverage or submitting a transaction."""
    account_index, key_index, private_key = credentials(read_environment(path))
    signer = lighter.SignerClient(
        url=MAINNET,
        account_index=account_index,
        api_private_keys={key_index: private_key},
        chain_id=304,
        nonce_management_type=lighter.nonce_manager.NonceManagerType.NONE,
    )
    try:
        if await asyncio.to_thread(signer.check_client):
            raise ExchangeError("AUTH_ERROR")
        token, error = signer.create_auth_token_with_expiry(api_key_index=key_index)
        if error:
            raise ExchangeError("AUTH_ERROR")
        api = lighter.AccountApi(signer.api_client)
        limits = checked(
            await api.account_limits(
                account_index=account_index, authorization=token, _request_timeout=10.0
            )
        )
        account = checked(
            await api.account(
                by="index",
                value=str(account_index),
                active_only=False,
                _headers={"Authorization": token},
                _request_timeout=10.0,
            )
        )["accounts"][0]
        print(
            json.dumps(
                {
                    "account_index": account_index,
                    "tier": limits["user_tier"],
                    "tier_name": limits["user_tier_name"],
                    "current_taker_fee_tick": limits["current_taker_fee_tick"],
                    "fee_tick_scale": 1000000,
                    "available_balance": account["available_balance"],
                    "BTC_positions": [
                        {
                            k: p[k]
                            for k in (
                                "position",
                                "sign",
                                "avg_entry_price",
                                "initial_margin_fraction",
                                "margin_mode",
                                "open_order_count",
                                "pending_order_count",
                            )
                        }
                        for p in account["positions"]
                        if p["symbol"] == "BTC"
                    ],
                },
                indent=2,
            )
        )
    finally:
        await signer.close()


async def execute(args: argparse.Namespace) -> int:
    config = load_config(args.env, live=args.command in ("run", "flatten"))
    if args.command == "check-config":
        print("Configuration valid. No exchange requests or orders were made.")
        return 0
    lock = ProcessLock(config.data_dir)
    journal = Journal(config.data_dir)
    journal.start()
    listener = configure(config.log_dir, config.log_level, config.private_key)
    metrics = Metrics()
    client = LighterClient(config, metrics)
    bot = Bot(config, journal, client, metrics)
    try:
        if args.command == "flatten":
            bot.machine.transition(State.SYNCING)
            bot.market = await client.discover()
            await client.connect()
            bot.recover("MANUAL_FLATTEN")
            for intent in await journal.unresolved():
                bot.owned[int(intent["client_id"])] = intent["kind"]
            await bot._resolve_intents(await client.snapshot(), allow_foreign_orders=True)
            await client.refresh_nonce()
            await bot.flatten_position("MANUAL_FLATTEN", allow_foreign_orders=True)
            confirmed = await client.snapshot()
            if (
                confirmed.position.size
                or confirmed.orders
                or int(confirmed.account["pending_order_count"])
            ):
                raise ExchangeError("FLAT_NOT_CONFIRMED")
            print("BTC exposure, active orders, and pending account orders confirmed zero.")
            return 0
        loop = asyncio.get_running_loop()

        def stop_bot() -> None:
            bot.stop.set()
            bot.wake.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_bot)
        print(
            f"LIGHTER BTC SCALPER | LIVE MAINNET | ACCOUNT {config.account_index} | BTC perpetual | "
            f"LEVERAGE {config.leverage} | NOTIONAL {config.desired_notional} | "
            f"MIN PROFIT {config.min_profit_usd} | MAX LOSS {config.max_loss_usd} | "
            f"MAX HOLD {config.max_hold_ms} ms | reconciling before enabling entries",
            flush=True,
        )
        await bot.run()
        if bot.halted_reason:
            raise ExchangeError(bot.halted_reason)
        return 0
    finally:
        await client.close()
        await journal.close()
        listener.stop()
        lock.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Live mainnet BTC scalper; run/flatten can use real funds"
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Environment file (must be mode 600); process variables override it",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "flatten", "check-config", "doctor", "account-info"):
        subparsers.add_parser(command)
    status = subparsers.add_parser("status")
    status.add_argument("--watch", action="store_true")
    status.add_argument("--data-dir", default=None)
    ui = subparsers.add_parser("ui", help="Local browser dashboard for RustDesk desktops")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--data-dir", default=None)
    ui.add_argument("--log-dir", default=None)
    args = parser.parse_args()
    try:
        if args.command == "ui":
            from scalper.dashboard import serve

            serve(args.env, args.port, args.data_dir, args.log_dir)
            return
        if args.command == "doctor":
            asyncio.run(doctor())
            return
        if args.command == "account-info":
            asyncio.run(account_info(args.env))
            return
        if args.command == "status":
            env_values = (
                dotenv_values(args.env, interpolate=False) if Path(args.env).exists() else {}
            )
            directory = Path(
                args.data_dir or os.environ.get("DATA_DIR") or env_values.get("DATA_DIR") or "data"
            )
            while True:
                value = local_status(directory)
                value["heartbeat_age_seconds"] = (
                    (time.time_ns() - value["heartbeat_utc_ns"]) / 1e9
                    if "heartbeat_utc_ns" in value
                    else None
                )
                print(json.dumps(value, indent=2))
                if not args.watch:
                    return
                time.sleep(1)
        sys.exit(asyncio.run(execute(args)))
    except ConfigError as error:
        print(f"CONFIG_ERROR: {error}", file=sys.stderr)
        sys.exit(78)
    except ExchangeError as error:
        print(
            f"{error.category}. Inspect authoritative exchange state before restarting if an order is uncertain.",
            file=sys.stderr,
        )
        sys.exit(
            78
            if error.category.startswith(("AUTH_ERROR", "CONFIG_ERROR", "UNKNOWN", "UNRESOLVED"))
            else 70
        )
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        # Do not print SDK exception bodies: they can contain authorization or signed payloads.
        print(f"STARTUP_OR_RUNTIME_ERROR: {type(error).__name__}", file=sys.stderr)
        sys.exit(70)


if __name__ == "__main__":
    main()
