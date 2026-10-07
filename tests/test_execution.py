import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scalper.config import D
from scalper.lighter_client import ExchangeError, LighterClient, Snapshot, parse_position
from scalper.metrics import Metrics, TradeCycle
from scalper.orderbook import BookError
from scalper.pnl import Position
from scalper.signals import Signal
from scalper.state_machine import State


def test_position_sign_and_unknown_state():
    assert parse_position({"position": "0.1", "sign": -1, "avg_entry_price": "100"}).quantity == D(
        "-0.1"
    )
    with pytest.raises(ExchangeError):
        parse_position({"position": "0.1", "sign": 0, "avg_entry_price": "100"})


async def test_official_signer_arguments_nonce_serialization_and_no_order_retries(config, market):
    client = LighterClient(config, Metrics())
    signer = SimpleNamespace(
        ORDER_TYPE_MARKET=1,
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0,
        DEFAULT_IOC_EXPIRY=0,
        sign_create_order=Mock(return_value=(14, "{}", "hash", None)),
    )
    client.signer = signer
    client.market = market
    client.nonce = 10
    client.transactions.send_tx = AsyncMock(
        return_value={"code": 200, "volume_quota_remaining": 100}
    )
    try:
        await client.order(1, 1000, 10000, sell=True, reduce_only=True, timestamps={})
        kwargs = signer.sign_create_order.call_args.kwargs
        assert kwargs["reduce_only"] is True and kwargs["nonce"] == 10
        assert kwargs["order_expiry"] == 0 and kwargs["time_in_force"] == 0
        await client.order(2, 1000, 10000, sell=False, reduce_only=False, timestamps={})
        assert signer.sign_create_order.call_args.kwargs["nonce"] == 11
        client.transactions.send_tx = AsyncMock(side_effect=TimeoutError)
        with pytest.raises(ExchangeError) as exc:
            await client.order(3, 1000, 10000, sell=True, reduce_only=True, timestamps={})
        assert exc.value.uncertain
        client.transactions.send_tx.assert_awaited_once()
    finally:
        client.signer = None
        await client.close()


async def test_unknown_stale_order_is_not_cancelled_automatically(bot, client):
    snapshot = client.snapshot.return_value
    snapshot.orders = [{"client_order_index": 999, "order_index": 1}]
    with pytest.raises(ExchangeError, match="UNKNOWN_BTC"):
        await bot._resolve_intents(snapshot)
    client.cancel.assert_not_awaited()


async def test_uncertain_entry_never_retried_when_lookup_is_absent(bot, client, journal):
    cid = await journal.prepare("entry", {"quantity": "1", "nonce": 10, "phase": "unknown"})
    bot.owned[cid] = "entry"
    client.lookup.return_value = None
    with pytest.raises(ExchangeError, match="UNRESOLVED_ORDER_INTENT"):
        await bot._resolve_intents(client.snapshot.return_value)
    client.order.assert_not_awaited()
    assert len(await journal.unresolved()) == 1


async def test_startup_adopts_exposure_only_for_central_flatten(bot, client):
    snap = Snapshot(
        {"pending_order_count": 0, "positions": []},
        None,
        Position(D("0.1"), D(100)),
        [],
        time.monotonic_ns(),
    )
    client.snapshot.return_value = snap
    bot.recover("startup")
    bot.flatten_position = AsyncMock()
    await bot.reconcile()
    bot.flatten_position.assert_awaited_once_with("startup")
    assert bot.cycle.recovered
    client.verify_leverage.assert_not_awaited()


async def test_partial_exit_uses_actual_remaining_size(bot):
    bot.machine.transition(State.ENTRY_PENDING)
    bot.machine.transition(State.OPEN_LONG)
    bot.machine.transition(State.EXIT_PENDING)
    bot.position = Position(D(1), D(100), time.monotonic_ns())

    async def first_submit(**kwargs):
        assert kwargs["quantity"] == 1 and kwargs["reduce_only"] is True
        bot.position = Position(D("0.4"), D(100), time.monotonic_ns())

    bot._submit = AsyncMock(side_effect=first_submit)
    await bot.exit("PROFIT_AVAILABLE", D(101))
    assert bot.machine.state == State.PARTIAL_EXIT
    bot._submit = AsyncMock()
    bot.machine.transition(State.EXIT_PENDING)
    await bot.exit("PROFIT_AVAILABLE", D(101))
    assert bot._submit.call_args.kwargs["quantity"] == D("0.4")


async def test_green_exit_has_priority_over_signal_generation(bot):
    now = time.monotonic_ns()
    bot.machine.transition(State.ENTRY_PENDING)
    bot.machine.transition(State.OPEN_LONG)
    bot.position = Position(D(1), D(99), now)
    bot.signals.calculate = Mock(
        side_effect=AssertionError("must not calculate entry while exposed")
    )
    bot.exit = AsyncMock()
    bot.evaluate()
    work = bot.work
    assert work is not None
    await work
    assert bot.exit.call_args.args[0] == "PROFIT_AVAILABLE"


async def test_hard_loss_precedes_profit_or_timeout(bot):
    bot.machine.transition(State.ENTRY_PENDING)
    bot.machine.transition(State.OPEN_LONG)
    bot.position = Position(D(1), D(101), time.monotonic_ns() - 10_000_000_000)
    bot.flatten_position = AsyncMock()
    bot.evaluate()
    await bot.work
    bot.flatten_position.assert_awaited_once_with("HARD_LOSS")


async def test_stale_data_blocks_entries_and_requests_recovery_with_exposure(bot):
    bot.book.received_ns = time.monotonic_ns() - 10_000_000_000
    bot.enter = AsyncMock()
    bot.evaluate()
    assert bot.work is None
    bot.enter.assert_not_awaited()
    bot.machine.transition(State.ENTRY_PENDING)
    bot.machine.transition(State.OPEN_LONG)
    bot.position = Position(D(1), D(100), time.monotonic_ns())
    bot.evaluate()
    assert bot.machine.state == State.RECOVERY


async def test_account_other_market_update_does_not_erase_btc(bot):
    bot.position = Position(D(1), D(100), time.monotonic_ns())
    bot.on_account(
        {
            "type": "update/account_all",
            "positions": {
                "88": {"market_id": 88, "position": "0", "avg_entry_price": "0", "sign": 1}
            },
        }
    )
    assert bot.position.quantity == 1


async def test_delayed_server_data_is_rejected(bot):
    with pytest.raises(BookError, match="Delayed"):
        bot.on_public(
            {"type": "update/order_book", "timestamp": time.time_ns() // 1_000_000 - 10000}
        )
    assert not bot.book.valid


async def test_fill_deduplication_and_actual_fee_accounting(bot):
    bot.owned[10] = "entry"
    bot.cycle = TradeCycle(1, 25)
    fill = {
        "trade_id": 1,
        "ask_account_id": 999,
        "bid_account_id": 123,
        "ask_client_id": 22,
        "bid_client_id": 10,
        "is_maker_ask": True,
        "size": "0.1",
        "price": "100",
        "taker_fee": 1000,
    }
    bot.on_fill(fill, 1)
    bot.on_fill(fill, 2)
    assert bot.cycle.entry_size == D("0.1")
    assert bot.cycle.fees == D("0.01")


async def test_terminal_order_does_not_imply_flat(bot, client, journal):
    cid = await journal.prepare("exit", {"quantity": "1", "nonce": 10})
    bot.owned[cid] = "exit"
    bot.pending = cid
    bot.orders[cid] = {"status": "filled"}
    client.snapshot.return_value = Snapshot(
        {"pending_order_count": 0, "positions": []},
        None,
        Position(D("0.4"), D(100)),
        [],
        time.monotonic_ns(),
    )
    await bot._await_terminal(cid)
    assert bot.position.size == D("0.4")
    assert bot.pending is None


async def test_live_entry_submission_has_explicit_exposure_lock(bot):
    signal = Signal(1, 1, (1, 1, 1, 1, 1, 1), 0)
    bot.signals.calculate = Mock(return_value=signal)
    bot.enter = AsyncMock()
    bot.evaluate()
    assert bot.machine.state == State.ENTRY_PENDING
    first = bot.work
    bot.evaluate()
    assert bot.work is first
    await first
    bot.enter.assert_awaited_once()
