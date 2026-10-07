"""Single-owner event-driven strategy, execution lifecycle, and recovery coordinator."""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from scalper.config import BPS, Config, D
from scalper.lighter_client import ExchangeError, LighterClient, Snapshot, parse_position, terminal
from scalper.market_data import Streams
from scalper.metrics import Metrics, TradeCycle
from scalper.orderbook import BookError, Market, OrderBook
from scalper.persistence import Journal
from scalper.pnl import Position, estimate_close
from scalper.rate_limits import RateBudget
from scalper.signals import Signal, SignalEngine
from scalper.state_machine import State, StateMachine

log = logging.getLogger("scalper")


def market_rows(value: Any, market_id: int) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if "market_id" in value or "position" in value:
            return [value] if int(value.get("market_id", market_id)) == market_id else []
        value = value.get(str(market_id), value.get(market_id, []))
        if isinstance(value, dict):
            return [value]
    if isinstance(value, list):
        return [
            r
            for r in value
            if int(r.get("market_id", r.get("market_index", market_id))) == market_id
        ]
    return []


class Bot:
    def __init__(
        self, config: Config, journal: Journal, client: LighterClient, metrics: Metrics
    ) -> None:
        self.config, self.journal, self.client, self.metrics = config, journal, client, metrics
        self.machine = StateMachine()
        self.book = OrderBook(config.max_book_levels)
        self.bbo = OrderBook(1)
        self.bbo_nonce = -1
        self.signals = SignalEngine(
            config.book_levels, config.weights, config.entry_score_threshold
        )
        self.position = Position()
        self.market: Market | None = None
        self.streams: Streams | None = None
        self.snapshot: Snapshot | None = None
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.order_event = asyncio.Event()
        self.work: asyncio.Task[None] | None = None
        self.owned: dict[int, str] = {}
        self.orders: dict[int, dict[str, Any]] = {}
        self.fills_seen: set[int] = set()
        self.fill_ids: deque[int] = deque(maxlen=20000)
        self.pending: int | None = None
        self.cycle: TradeCycle | None = None
        self.entry_budget = RateBudget(config.max_entries_per_minute, 0)
        self.recovery_reason = "startup"
        self.last_reconcile_ns = 0
        self.position_update_ns = 0
        self.last_status_ns = 0
        self.last_limits_ns = 0
        self.last_account_transaction = 0
        self.last_position_row: dict[str, Any] | None = None
        self.halted_reason = ""
        self.background: list[asyncio.Task[None]] = []
        self.exit_reason: str | None = None
        self.client.on_signed = journal.annotate

    def recover(self, reason: str) -> None:
        if self.machine.state != State.HALTED:
            self.machine.transition(State.RECOVERY)
            self.recovery_reason = reason
            self.wake.set()

    def on_disconnect(self, name: str) -> None:
        if name == "public":
            self.book.valid = False
            self.bbo.valid = False
            self.bbo_nonce = -1
            self.signals.reset()
        if not self.stop.is_set():
            self.recover(f"{name}_stream_disconnected")

    def on_public(self, message: dict[str, Any]) -> None:
        now = time.monotonic_ns()
        kind = message["type"]
        if kind.endswith("/order_book"):
            if message.get("timestamp"):
                server_ms = int(message["timestamp"])
                lag_ms = time.time_ns() // 1_000_000 - server_ms
                if lag_ms > self.config.market_stale_ms or lag_ms < -5000:
                    self.book.valid = False
                    raise BookError("Delayed book event or unsynchronized clock")
            if self.book.update(
                message["order_book"], now, snapshot=kind.startswith("subscribed/")
            ):
                self.signals.quote(self.book, now)
        elif kind == "update/trade":
            trades = message.get("trades", [])
            if not isinstance(trades, list):
                raise BookError("Invalid trade stream payload")
            for trade in trades:
                self.signals.trade(
                    int(trade["trade_id"]),
                    float(trade["size"]),
                    buyer_aggressive=bool(trade["is_maker_ask"]),
                    now_ns=now,
                )
        elif kind.endswith("/ticker"):
            nonce = int(message["nonce"])
            if nonce > self.bbo_nonce:
                ticker = message["ticker"]
                self.bbo.update(
                    {"bids": [ticker["b"]], "asks": [ticker["a"]], "offset": nonce},
                    now,
                    snapshot=True,
                )
                self.bbo_nonce = nonce
                self.signals.quote(self.bbo, now)
        self.wake.set()

    def on_account(self, message: dict[str, Any]) -> None:
        if self.market is None:
            return
        now, index = time.monotonic_ns(), self.market.index
        transaction_clock = int(message.get("transaction_time", 0))
        # Fills are applied before positions, so flat confirmation cannot race accounting.
        for fill in market_rows(message.get("trades"), index):
            transaction_clock = max(transaction_clock, int(fill.get("transaction_time", 0)))
            self.on_fill(fill, now)
        for order in market_rows(message.get("orders"), index):
            transaction_clock = max(transaction_clock, int(order.get("transaction_time", 0)))
            cid = int(order["client_order_index"])
            if cid in self.owned:
                self.orders[cid] = order
                self.order_event.set()
            elif not terminal(order):
                self.recover("unknown_BTC_order")
        position_key = "positions" if "positions" in message else "position"
        if position_key in message:
            rows = market_rows(message[position_key], index)
            if len(rows) > 1:
                raise ExchangeError("STATE_MISMATCH")
            # An update containing other markets must not erase BTC exposure.
            full = message["type"].startswith("subscribed/")
            if rows or full:
                tx_time = transaction_clock
                if tx_time and tx_time < self.last_account_transaction:
                    return
                self.last_account_transaction = max(tx_time, self.last_account_transaction)
                self.last_position_row = rows[0] if rows else None
                self._position(parse_position(rows[0] if rows else None), now)
        self.last_account_transaction = max(self.last_account_transaction, transaction_clock)
        self.wake.set()

    def on_fill(self, fill: dict[str, Any], now_ns: int) -> None:
        tid = int(fill["trade_id"])
        if tid in self.fills_seen:
            return
        account = self.config.account_index
        if int(fill["ask_account_id"]) == account:
            cid = int(fill["ask_client_id"])
            maker = bool(fill["is_maker_ask"])
        elif int(fill["bid_account_id"]) == account:
            cid = int(fill["bid_client_id"])
            maker = not bool(fill["is_maker_ask"])
        else:
            return
        if cid not in self.owned:
            return
        if len(self.fill_ids) == self.fill_ids.maxlen:
            self.fills_seen.discard(self.fill_ids[0])
        self.fill_ids.append(tid)
        self.fills_seen.add(tid)
        self.journal.emit("fill", fill)
        if self.cycle:
            if maker:
                self.recover("unexpected_maker_fill")
            tick = fill.get(
                "maker_fee" if maker else "taker_fee", 0
            )  # Official omitempty: absent means zero.
            self.cycle.fill(
                entry=self.owned[cid] == "entry",
                size=D(fill["size"]),
                price=D(fill["price"]),
                fee_tick=int(tick) if tick is not None else None,
                fee_scale=self.config.fee_tick_scale,
                now_ns=now_ns,
            )
            log.info(
                "%s_FILL qty=%s price=%s",
                "ENTRY" if self.owned[cid] == "entry" else "EXIT",
                fill["size"],
                fill["price"],
            )
            sent = self.cycle.timestamps.get(
                "entry_sent" if self.owned[cid] == "entry" else "exit_sent"
            )
            if sent:
                self.metrics.measure("send_to_fill", sent, now_ns)

    def _position(self, position: Position, now_ns: int) -> None:
        previous = self.position
        if position.size and not previous.size:
            position.opened_ns = now_ns
            if self.machine.state not in (State.ENTRY_PENDING, State.RECOVERY, State.SYNCING):
                self.recover("unexpected_position")
        elif position.size:
            position.opened_ns = previous.opened_ns
            if previous.quantity * position.quantity < 0:
                self.recover("position_reversed")
            if position.size > previous.size and self.pending is None:
                self.recover("unexplained_exposure_increase")
        self.position = position
        self.position_update_ns = now_ns
        if (
            position.size
            and self.pending
            and self.owned[self.pending] == "entry"
            and self.machine.state == State.ENTRY_PENDING
        ):
            self.machine.transition(State.PARTIALLY_FILLED)

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        clock = int(snapshot.account.get("transaction_time", 0))
        if clock and clock < self.last_account_transaction:
            raise ExchangeError("ACCOUNT_SNAPSHOT_BEHIND_PRIVATE_STREAM")
        self.snapshot = snapshot
        self._position(snapshot.position, snapshot.fetched_ns)
        self.last_reconcile_ns = snapshot.fetched_ns

    def _finish_cycle(self) -> None:
        if self.cycle is None:
            return
        if not self.cycle.recovered and not self.cycle.entry_size and not self.cycle.exit_size:
            self.cycle = None
            return
        result = self.cycle.finish(time.monotonic_ns())
        self.journal.emit("trade", result)
        self.metrics.samples["holding"].append(result["holding_ms"])
        self.metrics.counts["trades"] += 1
        if result["accounting_complete"]:
            net = D(result["net_realized_pnl"])
            self.metrics.counts["wins" if net > 0 else "losses" if net < 0 else "breakeven"] += 1
        else:
            self.metrics.counts["accounting_incomplete"] += 1
        log.info(
            "FLAT accounting_complete=%s net_realized_pnl=%s",
            result["accounting_complete"],
            result["net_realized_pnl"],
        )
        self.cycle = None

    def _confirmed_state(self) -> None:
        if self.position.size:
            self.machine.transition(
                State.OPEN_LONG if self.position.quantity > 0 else State.OPEN_SHORT
            )
        else:
            self._finish_cycle()
            self.machine.transition(State.FLAT)

    async def initialize(self) -> None:
        self.machine.transition(State.SYNCING)
        self.market = await self.client.discover()
        await self.client.connect()
        for intent in await self.journal.unresolved():
            self.owned[int(intent["client_id"])] = intent["kind"]
        self.streams = Streams(
            self.client, self.market.index, self.on_public, self.on_account, self.on_disconnect
        )
        await self.streams.start()
        self.client.ws_sender = self.streams.send_tx
        self.recover("startup_reconciliation")

    async def _resolve_intents(
        self, snapshot: Snapshot, *, allow_foreign_orders: bool = False
    ) -> None:
        intents = await self.journal.unresolved()
        known = {int(i["client_id"]) for i in intents} | set(self.owned)
        for order in snapshot.orders:
            if int(order["client_order_index"]) not in known and not allow_foreign_orders:
                raise ExchangeError("UNKNOWN_BTC_ORDER: manual intervention required")
            # Only cancel orders provably owned by this bot; each cancellation is sent once.
            await self.client.cancel(order)
        if snapshot.orders:
            await asyncio.sleep(0.25)
            current = await self.client.snapshot()
            if current.orders:
                raise ExchangeError("ORDER_CANCEL_UNCONFIRMED")
        for intent in intents:
            cid = int(intent["client_id"])
            if intent.get("phase") == "prepared":
                # The durable signed-hash update must commit before any transmission.
                # A prepared-only intent therefore cannot have reached the exchange.
                self.journal.emit("resolved", cid)
                continue
            resolved_order = await self.client.lookup(cid)
            if resolved_order is None and intent.get("tx_hash"):
                transaction = await self.client.transaction(intent["tx_hash"])
                event = json.loads(transaction.get("event_info") or "{}")
                if int(transaction["status"]) == 0 or (
                    int(transaction["status"]) == 2 and event.get("ae")
                ):
                    self.journal.emit("resolved", cid)
                    continue
            if resolved_order is None or not terminal(resolved_order):
                # Absence is not proof of a failed send. No repeated entry/exit creation.
                raise ExchangeError(
                    "UNRESOLVED_ORDER_INTENT: inspect exchange order before resuming"
                )
            self.orders[cid] = resolved_order
            self.journal.emit("resolved", cid)
        if intents:
            await self.journal.queue.join()
        self.pending = None

    async def reconcile(self) -> None:
        if self.market is None:
            raise ExchangeError("STATE_MISMATCH")
        current = await self.client.snapshot()
        unresolved = await self.journal.unresolved()
        if unresolved or current.orders:
            await self._resolve_intents(current)
            current = await self.client.snapshot()
        self._apply_snapshot(current)
        periodic = self.recovery_reason == "periodic_reconciliation"
        if not periodic:
            await self.client.refresh_nonce()
        now = time.monotonic_ns()
        if unresolved or self.cycle:
            for fill in reversed(await self.client.recent_fills()):
                self.on_fill(fill, now)
        if now - self.last_limits_ns > 60_000_000_000:
            await self.client.validate_limits()
            self.last_limits_ns = now
        if current.position.size:
            if periodic and self.cycle and not self.cycle.recovered:
                await self.client.verify_leverage(current, configure=False)
                self._confirmed_state()
                return
            if self.cycle is None:
                self.cycle = TradeCycle(
                    1 if current.position.quantity > 0 else -1, self.config.leverage, recovered=True
                )
            # Reconnect/startup/unhealthy state is protection priority, ahead of green exits.
            await self.flatten_position(self.recovery_reason)
            return
        if int(current.account["pending_order_count"]):
            raise ExchangeError("PENDING_ACCOUNT_TRANSACTIONS")
        if any(
            D(p["position"])
            for p in current.account["positions"]
            if int(p["market_id"]) != self.market.index
        ):
            raise ExchangeError("CONFIG_ERROR: use a dedicated BTC-only subaccount")
        await self.client.verify_leverage(current, configure=True)
        self._confirmed_state()

    async def _rest_book(self) -> OrderBook:
        if self.market is None:
            raise ExchangeError("STATE_MISMATCH")
        result = await self.client._read(
            self.client.orders.order_book_orders, market_id=self.market.index, limit=100
        )
        payload: dict[str, Any] = {"bids": [], "asks": [], "offset": 0}
        for side in ("bids", "asks"):
            levels: dict[Any, Any] = {}
            for row in result[side]:
                p = D(row["price"])
                levels[p] = levels.get(p, D(0)) + D(row["remaining_base_amount"])
            payload[side] = [{"price": str(p), "size": str(q)} for p, q in levels.items()]
        book = OrderBook()
        book.update(payload, time.monotonic_ns(), snapshot=True)
        return book

    async def _submit(self, *, buy: bool, quantity: Any, limit: Any, reduce_only: bool) -> None:
        if self.market is None:
            raise ExchangeError("STATE_MISMATCH")
        kind = "exit" if reduce_only else "entry"
        price_units = self.market.price_units(limit, buy=buy)
        quantity_units = self.market.size_units(quantity)
        if quantity_units <= 0:
            raise ExchangeError("INVALID_ORDER_SIZE")
        cid = await self.journal.prepare(
            kind,
            {
                "quantity": str(quantity),
                "limit": str(limit),
                "buy": buy,
                "nonce": self.client.nonce,
                "reduce_only": reduce_only,
            },
        )
        self.owned[cid] = kind
        self.pending = cid
        self.order_event.clear()
        timestamps = self.cycle.timestamps if self.cycle else {}
        try:
            # A filesystem/queue delay must not turn an old signal into an entry.
            if not reduce_only and (
                not self.healthy(time.monotonic_ns())
                or self.position.size
                or not self.book.sweep(quantity, buy=buy, limit=limit).complete
            ):
                self.journal.emit("resolved", cid)
                self.pending = None
                return
            if reduce_only:
                if not self.position.size:
                    self.journal.emit("resolved", cid)
                    self.pending = None
                    return
                if buy != (self.position.quantity < 0):
                    raise ExchangeError("POSITION_REVERSED")
                quantity_units = min(quantity_units, self.market.size_units(self.position.size))
            await self.client.order(
                cid,
                quantity_units,
                price_units,
                sell=not buy,
                reduce_only=reduce_only,
                timestamps=timestamps,
            )
        except Exception as error:
            if not isinstance(error, ExchangeError) or not error.uncertain:
                self.journal.emit("resolved", cid)
                self.pending = None
            raise
        log.info(
            "%s_SENT client_id=%s qty=%s reduce_only=%s", kind.upper(), cid, quantity, reduce_only
        )
        decision = timestamps.get("exit_decision" if reduce_only else "entry_decision")
        if decision:
            self.metrics.measure(
                "profit_to_exit_send" if reduce_only else "signal_to_send",
                decision,
                timestamps[f"{kind}_sent"],
            )
        await self._await_terminal(cid)

    async def _await_terminal(self, cid: int) -> None:
        deadline = time.monotonic_ns() + self.config.order_timeout_ms * 1_000_000
        while True:
            order = self.orders.get(cid)
            if order and terminal(order):
                break
            remaining = (deadline - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                order = await self.client.lookup(cid)
                if order is None or not terminal(order):
                    raise ExchangeError("ORDER_CONFIRMATION_UNCERTAIN", uncertain=True)
                self.orders[cid] = order
                break
            self.order_event.clear()
            try:
                await asyncio.wait_for(self.order_event.wait(), timeout=remaining)
            except TimeoutError:
                continue
        self.journal.emit("resolved", cid)
        self.pending = None
        # A terminal order is not proof that the account is flat. Reconcile actual exposure.
        sent_ns = self.cycle.timestamps.get(f"{self.owned[cid]}_sent", 0) if self.cycle else 0
        row = self.last_position_row
        private_confirmation = bool(
            row
            and self.position_update_ns >= sent_ns
            and int(row.get("open_order_count", -1)) == 0
            and int(row.get("pending_order_count", -1)) == 0
        )
        # Manage an actual entry fill immediately when the private stream supplies complete state.
        # After exits, refresh balance/flat authority before any next entry.
        if self.owned[cid] == "exit" or not private_confirmation:
            current = await self.client.snapshot()
            self._apply_snapshot(current)
            if current.orders or int(current.account["pending_order_count"]):
                raise ExchangeError("STATE_MISMATCH")
        if self.cycle and (
            not self.cycle.entry_size
            or (not self.position.size and self.cycle.entry_size != self.cycle.exit_size)
        ):
            for fill in reversed(await self.client.recent_fills()):
                self.on_fill(fill, time.monotonic_ns())
        # Keep order ownership for late duplicate fills while bounding resident memory.
        while len(self.owned) > 10000:
            oldest = next(iter(self.owned))
            if oldest == self.pending:
                break
            self.owned.pop(oldest)
            self.orders.pop(oldest, None)

    async def enter(self, signal: Signal, quantity: Any, limit: Any) -> None:
        self.cycle = TradeCycle(
            signal.direction, self.config.leverage, signal.score, signal.components
        )
        self.cycle.timestamps["signal"] = self.cycle.timestamps["entry_decision"] = (
            time.monotonic_ns()
        )
        self.cycle.entry_reference = self.book.sweep(
            quantity, buy=signal.direction > 0, limit=limit
        ).vwap
        await self._submit(
            buy=signal.direction > 0, quantity=quantity, limit=limit, reduce_only=False
        )
        self._confirmed_state()

    async def exit(self, reason: str, limit: Any) -> None:
        if self.cycle:
            self.cycle.exit_reason = reason
            self.cycle.timestamps["exit_decision"] = time.monotonic_ns()
            self.cycle.timestamps.setdefault(
                "profit_detected", self.cycle.timestamps["exit_decision"]
            )
        await self._submit(
            buy=self.position.quantity < 0,
            quantity=self.position.size,
            limit=limit,
            reduce_only=True,
        )
        if self.position.size:
            self.machine.transition(State.PARTIAL_EXIT)
            self.exit_reason = reason
        else:
            self.exit_reason = None
            self._confirmed_state()

    async def flatten_position(self, reason: str, *, allow_foreign_orders: bool = False) -> None:
        """Reconcile, cancel conflicts, send one reduce-only IOC at a time, confirm flat."""
        for _ in range(8):
            current = await self.client.snapshot()
            self._apply_snapshot(current)
            if current.orders:
                for order in current.orders:
                    if (
                        not allow_foreign_orders
                        and int(order["client_order_index"]) not in self.owned
                    ):
                        raise ExchangeError(
                            "UNKNOWN_BTC_ORDER: flatten requires explicit manual command"
                        )
                    await self.client.cancel(order)
                await asyncio.sleep(0.25)
                check = await self.client.snapshot()
                if check.orders:
                    raise ExchangeError("ORDER_CANCEL_UNCONFIRMED")
                self._apply_snapshot(check)
            if self.snapshot is None:
                raise ExchangeError("STATE_MISMATCH")
            if int(self.snapshot.account["pending_order_count"]):
                raise ExchangeError("PENDING_ACCOUNT_TRANSACTIONS")
            if not self.position.size:
                self.pending = None
                self._confirmed_state()
                return
            if self.cycle is None:
                self.cycle = TradeCycle(
                    1 if self.position.quantity > 0 else -1, self.config.leverage, recovered=True
                )
            book = (
                self.book
                if self.book.fresh(time.monotonic_ns(), self.config.market_stale_ms)
                else await self._rest_book()
            )
            buy = self.position.quantity < 0
            reference = book.ask if buy else book.bid
            limit = reference * (
                1 + self.config.emergency_exit_slippage_bps / BPS * (1 if buy else -1)
            )
            self.machine.transition(State.EXIT_PENDING)
            await self.exit(reason, limit)
            if self.position.size:
                # The prior IOC is terminal and the remaining size has been read from the exchange.
                # This is a new reduce-only action for remaining exposure, not a retry of the old order.
                await self.client.refresh_nonce()
        raise ExchangeError("EMERGENCY_FLATTEN_INCOMPLETE")

    def healthy(self, now_ns: int) -> bool:
        return bool(
            self.streams
            and self.streams.public_ready
            and self.streams.account_ready
            and now_ns - self.streams.account_ns < self.config.account_stale_ms * 1_000_000
            and self.book.fresh(now_ns, self.config.market_stale_ms)
            and self.snapshot
            and now_ns - self.last_reconcile_ns < self.config.account_stale_ms * 1_000_000
        )

    def evaluate(self) -> None:
        if self.work is not None or self.machine.state == State.HALTED:
            return
        now = time.monotonic_ns()
        if self.journal.failure:
            self.recover("journal_failed")
        if self.machine.state == State.RECOVERY:
            self._launch(self.reconcile())
            return
        if not self.healthy(now):
            if self.position.size:
                self.recover("stale_data_or_account")
            return
        if now - self.last_reconcile_ns >= self.config.reconcile_ms * 1_000_000:
            self.recover("periodic_reconciliation")
            self._launch(self.reconcile())
            return
        if self.position.size:
            # Faster ticker updates are authoritative for the displayed size at the BBO.
            # Use only that size when its prices differ from the slower depth snapshot;
            # never combine a new BBO with stale deeper liquidity for a green decision.
            execution_book = self.book
            if self.bbo.fresh(now, self.config.market_stale_ms) and (
                self.bbo.bid != self.book.bid or self.bbo.ask != self.book.ask
            ):
                execution_book = self.bbo
            buy = self.position.quantity < 0
            reference = execution_book.ask if buy else execution_book.bid
            limit = reference * (
                1 + self.config.normal_exit_slippage_bps / BPS * (1 if buy else -1)
            )
            estimate = estimate_close(
                self.position,
                execution_book,
                taker_fee=self.config.taker_fee,
                min_profit_usd=self.config.min_profit_usd,
                min_profit_bps=self.config.min_profit_bps,
                execution_buffer_bps=self.config.execution_buffer_bps,
                safety_buffer_usd=self.config.safety_buffer_usd,
                limit=limit,
            )
            if self.cycle:
                self.cycle.favorable = max(self.cycle.favorable, estimate.expected_net_pnl)
                self.cycle.adverse = min(self.cycle.adverse, estimate.expected_net_pnl)
                self.cycle.exit_reference = estimate.close_vwap
                self.cycle.estimated_slippage_usd = estimate.estimated_slippage
            adverse = (
                (self.position.entry - reference)
                * (1 if self.position.quantity > 0 else -1)
                / self.position.entry
                * BPS
            )
            reason = self.exit_reason
            if adverse >= self.config.max_adverse_bps or (
                estimate.complete and estimate.expected_net_pnl <= -self.config.max_loss_usd
            ):
                reason = "HARD_LOSS"
            elif reason is None and estimate.profitable:
                reason = "PROFIT_AVAILABLE"
            elif (
                reason is None
                and now - self.position.opened_ns >= self.config.max_hold_ms * 1_000_000
            ):
                reason = "MAX_HOLD"
            if reason:
                if reason == "PROFIT_AVAILABLE":
                    log.info(
                        "GREEN expected_net_pnl=%s close_vwap=%s",
                        estimate.expected_net_pnl,
                        estimate.close_vwap,
                    )
                self.machine.transition(State.EXIT_PENDING)
                if reason in ("HARD_LOSS", "MAX_HOLD"):
                    self._launch(self.flatten_position(reason))
                else:
                    self._launch(self.exit(reason, limit))
            return
        if (
            self.machine.state != State.FLAT
            or self.pending is not None
            or self.snapshot is None
            or self.market is None
        ):
            return
        if not self.client.tx_budget.available(
            now, priority=False
        ) or not self.entry_budget.available(now, priority=True):
            return
        shared = getattr(self.client, "shared_budget", None)
        if shared and not shared.available(now, priority=False):
            return
        recovery_cost = 400 if getattr(self.client, "weighted_reads", False) else 2
        if not self.client.read_budget.available(now, priority=False, amount=recovery_cost):
            return
        if self.journal.failure or self.book.spread_bps > self.config.max_spread_bps:
            return
        if int(self.snapshot.account["pending_order_count"]) or self.snapshot.orders:
            return
        signal = self.signals.calculate(self.book, now)
        if not signal.direction or signal.volatility_bps > self.config.max_volatility_bps:
            return
        buy = signal.direction > 0
        reference = self.book.ask if buy else self.book.bid
        quantity = self.market.size(self.config.desired_notional / reference)
        limit = reference * (1 + self.config.entry_slippage_bps / BPS * (1 if buy else -1))
        rounded_limit = D(self.market.price_units(limit, buy=buy)) / 10**self.market.price_decimals
        sweep = self.book.sweep(quantity, buy=buy, limit=rounded_limit) if quantity > 0 else None
        if (
            quantity < self.market.min_size
            or quantity * reference < self.market.min_notional
            or not sweep
            or not sweep.complete
        ):
            return
        worst_cost = quantity * rounded_limit if buy else quantity * reference
        required_margin = worst_cost / self.config.leverage + worst_cost * self.config.taker_fee
        if D(self.snapshot.account["available_balance"]) < required_margin:
            return
        if (
            self.client.volume_quota is not None
            and self.client.volume_quota <= self.config.exit_reserve
        ):
            return
        if self.machine.begin_entry():
            self.entry_budget.consume(now, priority=True)
            self._launch(self.enter(signal, quantity, rounded_limit))

    def _launch(self, operation: Any) -> None:
        self.work = asyncio.create_task(self._action(operation), name="execution")

    async def _action(self, operation: Any) -> None:
        try:
            await operation
        except Exception as error:
            category = error.category if isinstance(error, ExchangeError) else type(error).__name__
            self.metrics.counts["execution_errors"] += 1
            log.error("EXECUTION_ERROR category=%s exposure=%s", category, self.position.quantity)
            if self.machine.state == State.RECOVERY or category.startswith(
                ("CONFIG_ERROR", "AUTH_ERROR", "UNKNOWN_BTC", "UNRESOLVED")
            ):
                self.halted_reason = category
                self.machine.transition(State.HALTED)
                self.stop.set()
            else:
                self.recover(category)
        finally:
            self.work = None
            self.wake.set()

    async def _watchdog(self) -> None:
        previous = time.monotonic_ns()
        while not self.stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic_ns()
            if now - previous > max(self.config.market_stale_ms, 250) * 1_000_000:
                self.recover("event_loop_stalled")
            previous = now
            self.wake.set()
            if now - self.last_status_ns >= 1_000_000_000:
                self.last_status_ns = now
                self.journal.emit("status", self.status())

    def status(self) -> dict[str, Any]:
        now = time.monotonic_ns()
        result = {
            "mode": "LIVE MAINNET",
            "running": not self.stop.is_set(),
            "state": self.machine.state,
            "market": "BTC perpetual",
            "heartbeat_utc_ns": time.time_ns(),
            "account_index": self.config.account_index,
            "position": str(self.position.quantity),
            "average_entry": str(self.position.entry),
            "leverage": self.config.leverage,
            "minimum_profit_usd": str(self.config.min_profit_usd),
            "bid": str(self.book.bid) if self.book.valid else None,
            "ask": str(self.book.ask) if self.book.valid else None,
            "spread_bps": str(self.book.spread_bps) if self.book.valid else None,
            "streams_healthy": self.healthy(now),
            "halted_reason": self.halted_reason,
            "tx_headroom": self.client.tx_budget.headroom(now),
            "read_headroom": self.client.read_budget.headroom(now),
            "account_tier": self.client.tier,
            "volume_quota_remaining": self.client.volume_quota,
            "metrics": self.metrics.snapshot(),
        }
        result["market_stream_connected"] = bool(self.streams and self.streams.public_ready)
        result["account_stream_connected"] = bool(self.streams and self.streams.account_ready)
        if self.position.size and self.book.valid:
            book = self.bbo if self.bbo.fresh(now, self.config.market_stale_ms) else self.book
            estimate = estimate_close(
                self.position,
                book,
                taker_fee=self.config.taker_fee,
                min_profit_usd=self.config.min_profit_usd,
                min_profit_bps=self.config.min_profit_bps,
                execution_buffer_bps=self.config.execution_buffer_bps,
                safety_buffer_usd=self.config.safety_buffer_usd,
            )
            result["executable_close"] = str(estimate.close_vwap)
            result["expected_net_pnl"] = str(estimate.expected_net_pnl)
            result["whole_position_liquidity"] = estimate.complete
        return result

    async def run(self) -> None:
        try:
            await self.initialize()
            self.background = [asyncio.create_task(self._watchdog(), name="watchdog")]
            while not self.stop.is_set():
                await self.wake.wait()
                self.wake.clear()
                self.evaluate()
        finally:
            self.stop.set()
            if self.work:
                # Never cancel an in-flight submission and assume it failed.
                await self.work
            for task in self.background:
                task.cancel()
            await asyncio.gather(*self.background, return_exceptions=True)
            unsafe_to_resubmit = self.halted_reason.startswith(
                ("UNRESOLVED", "UNKNOWN", "AUTH_ERROR")
            )
            was_halted = self.machine.state == State.HALTED
            if not unsafe_to_resubmit and self.client.signer and self.market:
                try:
                    self.machine.transition(State.RECOVERY)
                    current = await self.client.snapshot()
                    await self._resolve_intents(current)
                    await self.client.refresh_nonce()
                    await self.flatten_position("SHUTDOWN")
                except Exception as error:
                    self.halted_reason = (
                        error.category if isinstance(error, ExchangeError) else type(error).__name__
                    )
                    log.critical(
                        "SHUTDOWN_FLATTEN_FAILED category=%s; inspect Lighter immediately",
                        self.halted_reason,
                    )
                if was_halted:
                    self.machine.transition(State.HALTED)
            if self.streams:
                await self.streams.close()
            self.journal.emit("status", self.status())
