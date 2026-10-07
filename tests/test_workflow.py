import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from aiohttp import web

from scalper.config import D
from scalper.lighter_client import Snapshot
from scalper.market_data import Streams
from scalper.pnl import Position
from scalper.signals import Signal
from scalper.state_machine import State


async def test_event_to_entry_fill_green_exit_and_confirmed_flat(bot, client, journal, market):
    bot.signals.calculate = Mock(return_value=Signal(1, 0.9, (0.9,) * 6, 0))
    calls = []

    async def exchange_order(cid, qty_units, price_units, *, sell, reduce_only, timestamps):
        calls.append((cid, qty_units, sell, reduce_only))
        size = D(qty_units) / 10**market.size_decimals
        execution_price = D("100.20") if reduce_only else D("100.01")
        prefix = "exit" if reduce_only else "entry"
        timestamps[f"{prefix}_sent"] = time.monotonic_ns()
        position_row = {
            "position": "0" if reduce_only else str(size),
            "sign": 1,
            "avg_entry_price": "0" if reduce_only else str(execution_price),
            "pending_order_count": 0,
            "open_order_count": 0,
        }
        position = Position() if reduce_only else Position(size, execution_price)
        client.snapshot.return_value = Snapshot(
            {"available_balance": "1000", "pending_order_count": 0, "positions": []},
            position_row,
            position,
            [],
            time.monotonic_ns(),
        )
        fill = {
            "trade_id": len(calls),
            "ask_account_id": 123 if reduce_only else 999,
            "bid_account_id": 999 if reduce_only else 123,
            "ask_client_id": cid if reduce_only else 0,
            "bid_client_id": 0 if reduce_only else cid,
            "is_maker_ask": not reduce_only,
            "size": str(size),
            "price": str(execution_price),
            "taker_fee": 0,
        }
        bot.on_account(
            {
                "type": "update/account_all",
                "trades": {"42": [fill]},
                "orders": {"42": [{"client_order_index": cid, "status": "filled"}]},
                "positions": {"42": position_row},
            }
        )
        timestamps[f"{prefix}_ack"] = time.monotonic_ns()

    client.order.side_effect = exchange_order
    bot.evaluate()
    await bot.work
    assert bot.machine.state == State.OPEN_LONG and bot.position.size > 0
    bot.on_public(
        {
            "type": "update/order_book",
            "order_book": {
                "offset": 2,
                "bids": [{"price": "100.20", "size": "10"}],
                "asks": [{"price": "100.01", "size": "0"}, {"price": "100.21", "size": "10"}],
            },
        }
    )
    bot.evaluate()
    await bot.work
    await journal.queue.join()
    assert bot.machine.state == State.FLAT and not bot.position.size
    assert len(calls) == 2
    assert calls[0][2:] == (False, False) and calls[1][2:] == (True, True)
    assert calls[0][0] != calls[1][0] and calls[0][1] == calls[1][1]
    assert bot.metrics.counts["wins"] == 1
    row = journal.connection.execute("SELECT payload FROM trades").fetchone()
    assert row is not None
    import json

    result = json.loads(row[0])
    assert result["accounting_complete"] and D(result["net_realized_pnl"]) > 0


async def test_websocket_subscriptions_auth_and_real_local_transport(monkeypatch, config):
    messages = []
    requests = []
    ready = {name: asyncio.Event() for name in ("public", "account", "delta")}
    stream = None

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "connected"})
        first = await ws.receive_json()
        requests.append(first)
        if first["channel"].startswith("order_book/"):
            requests.append(await ws.receive_json())
            requests.append(await ws.receive_json())
            await ws.send_json(
                {
                    "type": "subscribed/order_book",
                    "channel": "order_book:42",
                    "order_book": {
                        "offset": 1,
                        "bids": [{"price": "100", "size": "1"}],
                        "asks": [{"price": "101", "size": "1"}],
                    },
                }
            )
            await ws.send_json({"type": "subscribed/trade", "trades": []})
            await ws.send_json({"type": "subscribed/ticker", "ticker": {}})
            await ws.send_json(
                {"type": "update/order_book", "order_book": {"offset": 2, "bids": [], "asks": []}}
            )
        else:
            await ws.send_json({"type": "subscribed/account_market", "position": []})
        await ws.send_json({"type": "ping"})
        assert await ws.receive_json() == {"type": "pong"}
        async for event in ws:
            request = json.loads(event.data)
            if request.get("type") == "jsonapi/sendtx":
                await ws.send_json(
                    {
                        "type": "jsonapi/sendtx",
                        "id": request["data"]["id"],
                        "code": 200,
                        "volume_quota_remaining": 100,
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/stream", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr("scalper.market_data.WS_URL", f"ws://127.0.0.1:{port}/stream")
    client = SimpleNamespace(
        config=config,
        auth=Mock(return_value="unit-test-auth"),
        metrics=SimpleNamespace(counts={"public_reconnects": 0, "account_reconnects": 0}),
    )

    def consume(message):
        messages.append(message)
        if message["type"] == "subscribed/ticker":
            ready["public"].set()
        elif message["type"] == "subscribed/account_market":
            ready["account"].set()
        elif message["type"] == "update/order_book":
            ready["delta"].set()

    stream = Streams(client, 42, consume, consume, lambda _: None)
    try:
        await stream.start()
        async with asyncio.timeout(3):
            await asyncio.gather(*(event.wait() for event in ready.values()))
        assert stream.public_ready and stream.account_ready
        assert {r["channel"] for r in requests} == {
            "order_book/42",
            "trade/42",
            "ticker/42",
            "account_market/42/123",
        }
        account_request = next(r for r in requests if r["channel"].startswith("account_market"))
        assert account_request["auth"] == "unit-test-auth"
        assert any(m["type"] == "update/order_book" for m in messages)
        assert stream.account_ns > 0
        reply = await stream.send_tx(14, "{}")
        assert reply["code"] == 200
    finally:
        await stream.close()
        await runner.cleanup()
