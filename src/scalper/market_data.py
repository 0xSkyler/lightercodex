"""Persistent public/account streams. A reconnect always invalidates prior synchronization."""

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import aiohttp
from aiohttp.helpers import get_env_proxy_for_url
from yarl import URL

from scalper.config import WS_URL
from scalper.lighter_client import ExchangeError, LighterClient

log = logging.getLogger("scalper")


def websocket_proxy(url: str) -> dict[str, Any]:
    # aiohttp looks for WSS_PROXY for wss URLs. The cloud provides HTTPS_PROXY;
    # reuse that supported CONNECT route, preserving NO_PROXY and authentication.
    try:
        proxy, auth = get_env_proxy_for_url(
            URL(url).with_scheme("https" if url.startswith("wss:") else "http")
        )
        return {"proxy": proxy, "proxy_auth": auth}
    except LookupError:
        return {}


class Streams:
    def __init__(
        self,
        client: LighterClient,
        market_id: int,
        on_public: Callable[[dict[str, Any]], None],
        on_account: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[str], None],
    ) -> None:
        self.client, self.market_id = client, market_id
        self.on_public, self.on_account, self.on_disconnect = on_public, on_account, on_disconnect
        self.public_ready = self.account_ready = False
        self.public_ns = self.account_ns = 0
        self.stop = asyncio.Event()
        self.session: aiohttp.ClientSession | None = None
        self.connections: dict[str, aiohttp.ClientWebSocketResponse] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.pending_tx: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            trust_env=True, connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())
        )
        self.tasks = [
            asyncio.create_task(self._connection(name), name=f"{name}-stream")
            for name in ("public", "account")
        ]

    async def reset_public(self) -> None:
        connection = self.connections.get("public")
        if connection:
            await connection.close()

    async def send_tx(self, tx_type: int, tx_info: str) -> dict[str, Any]:
        ws = self.connections.get("account")
        if ws is None or ws.closed or not self.account_ready:
            # A REST fallback is selected only before attempting any WebSocket send.
            response = await self.client.transactions.send_tx(
                tx_type=tx_type,
                tx_info=tx_info,
                _request_timeout=self.client.config.request_timeout_ms / 1000,
            )
            return response.to_dict()
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_tx[request_id] = future
        try:
            await ws.send_json(
                {
                    "type": "jsonapi/sendtx",
                    "data": {"id": request_id, "tx_type": tx_type, "tx_info": json.loads(tx_info)},
                }
            )
            return await asyncio.wait_for(future, self.client.config.request_timeout_ms / 1000)
        finally:
            self.pending_tx.pop(request_id, None)

    async def _connection(self, name: str) -> None:
        delay = 0.25
        while not self.stop.is_set():
            try:
                if self.session is None:
                    raise RuntimeError("Streams not initialized")
                stream_url = WS_URL + "?readonly=true" if name == "public" else WS_URL
                async with self.session.ws_connect(
                    stream_url,
                    heartbeat=5,
                    autoping=False,
                    max_msg_size=2_000_000,
                    timeout=aiohttp.ClientWSTimeout(ws_close=2),
                    **websocket_proxy(stream_url),
                ) as ws:
                    self.connections[name] = ws
                    started = time.monotonic_ns()
                    subscriptions = (
                        [
                            f"order_book/{self.market_id}",
                            f"trade/{self.market_id}",
                            f"ticker/{self.market_id}",
                        ]
                        if name == "public"
                        else [f"account_market/{self.market_id}/{self.client.config.account_index}"]
                    )
                    acknowledged: set[str] = set()
                    async for event in ws:
                        now = time.monotonic_ns()
                        if name == "public":
                            self.public_ns = now
                        else:
                            self.account_ns = now
                        if event.type == aiohttp.WSMsgType.PING:
                            await ws.pong(event.data)
                            continue
                        if event.type == aiohttp.WSMsgType.PONG:
                            continue
                        if event.type != aiohttp.WSMsgType.TEXT:
                            raise ExchangeError("NETWORK_ERROR")
                        message = json.loads(event.data)
                        kind = message.get("type", "")
                        body = message.get("data", message)
                        request_id = message.get(
                            "id", body.get("id") if isinstance(body, dict) else None
                        )
                        if request_id in self.pending_tx:
                            reply = body.get("result", body) if isinstance(body, dict) else message
                            future = self.pending_tx[request_id]
                            if not future.done():
                                future.set_result(reply)
                            continue
                        if kind == "connected":
                            for channel in subscriptions:
                                request = {"type": "subscribe", "channel": channel}
                                if name == "account":
                                    request["auth"] = self.client.auth()
                                await ws.send_json(request)
                        elif kind == "ping":
                            await ws.send_json({"type": "pong"})
                        elif kind == "shutdown":
                            raise ExchangeError("EXCHANGE_SHUTDOWN")
                        elif kind == "error" or "error" in message:
                            raise ExchangeError("STREAM_SUBSCRIPTION_ERROR")
                        elif kind.startswith(("subscribed/", "update/")):
                            if kind.startswith("subscribed/"):
                                acknowledged.add(kind.split("/", 1)[1])
                            if name == "public":
                                self.on_public(message)
                                self.public_ready = {
                                    "order_book",
                                    "trade",
                                    "ticker",
                                } <= acknowledged
                            else:
                                self.on_account(message)
                                self.account_ready = "account_market" in acknowledged
                            delay = 0.25
                        # Auth tokens expire after ten minutes. Reconnect with a fresh token and reconcile.
                        if name == "account" and now - started > 480_000_000_000:
                            break
                    if not self.stop.is_set():
                        raise ExchangeError("NETWORK_ERROR")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.client.metrics.counts[f"{name}_reconnects"] += 1
                log.warning(
                    "%s_STREAM_DISCONNECTED category=%s", name.upper(), type(error).__name__
                )
            finally:
                self.connections.pop(name, None)
                if name == "public":
                    self.public_ready = False
                else:
                    self.account_ready = False
                    for future in self.pending_tx.values():
                        if not future.done():
                            future.set_exception(
                                ExchangeError("WEBSOCKET_TX_UNCERTAIN", uncertain=True)
                            )
                self.on_disconnect(name)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, 10)

    async def close(self) -> None:
        self.stop.set()
        for connection in list(self.connections.values()):
            await connection.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.session:
            await self.session.close()
