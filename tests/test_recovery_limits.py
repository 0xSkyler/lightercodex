import time
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from scalper.config import D
from scalper.lighter_client import ExchangeError, LighterClient
from scalper.metrics import Metrics
from scalper.pnl import Position
from scalper.rate_limits import RateBudget
from scalper.state_machine import State, StateMachine


async def test_prepared_only_crash_intent_proves_no_transmission(bot, client, journal):
    cid = await journal.prepare("entry", {"nonce": 10})
    bot.owned[cid] = "entry"
    await bot._resolve_intents(client.snapshot.return_value)
    client.lookup.assert_not_awaited()
    client.order.assert_not_awaited()
    assert await journal.unresolved() == []


async def test_signed_hash_is_durable_before_transmission(journal):
    cid = await journal.prepare("entry", {"nonce": 10})
    await journal.annotate(cid, "test-transaction-hash", 10)
    intent = (await journal.unresolved())[0]
    assert intent["phase"] == "signed" and intent["tx_hash"] == "test-transaction-hash"


async def test_weighted_endpoint_budget_uses_actual_official_weight(config):
    client = LighterClient(replace(config, reads_per_minute=24000), Metrics())
    client.weighted_reads = True

    async def account(**kwargs):
        return {"code": 200}

    try:
        await client._read(account)
        assert client.read_budget.headroom(time.monotonic_ns()) == 23700
    finally:
        await client.close()


async def test_standard_shares_transaction_and_read_limits(config):
    client = LighterClient(replace(config, reads_per_minute=40, tx_per_minute=40), Metrics())
    client.auth = Mock(return_value="unit-test-auth")
    client._read = AsyncMock(return_value={"user_tier": "standard", "current_taker_fee_tick": 0})
    try:
        await client.validate_limits()
        assert client.shared_budget.capacity == 60 and client.shared_budget.reserve == 12
    finally:
        await client.close()


async def test_tier_fee_change_and_excess_limits_fail_closed(config):
    client = LighterClient(config, Metrics())
    client.auth = Mock(return_value="unit-test-auth")
    client._read = AsyncMock(return_value={"user_tier": "standard", "current_taker_fee_tick": 1})
    try:
        with pytest.raises(ExchangeError, match="rate budgets"):
            await client.validate_limits()
        client.tier = "standard"
        with pytest.raises(ExchangeError, match="taker fee changed"):
            await client.validate_limits()
    finally:
        await client.close()


async def test_bbo_can_trigger_green_only_for_advertised_entire_position(bot):
    bot.machine.transition(State.ENTRY_PENDING)
    bot.machine.transition(State.OPEN_LONG)
    bot.position = Position(D(1), D(100), time.monotonic_ns())
    bot.exit = AsyncMock()
    bot.on_public(
        {
            "type": "update/ticker",
            "nonce": 10,
            "ticker": {
                "b": {"price": "100.2", "size": "0.5"},
                "a": {"price": "100.21", "size": "1"},
            },
        }
    )
    bot.evaluate()
    assert bot.work is None
    bot.on_public(
        {
            "type": "update/ticker",
            "nonce": 11,
            "ticker": {"b": {"price": "100.2", "size": "2"}, "a": {"price": "100.21", "size": "1"}},
        }
    )
    bot.evaluate()
    await bot.work
    assert bot.exit.call_args.args[0] == "PROFIT_AVAILABLE"


async def test_shared_reserve_blocks_entry_even_with_local_tx_capacity(bot, client):
    client.shared_budget = RateBudget(60, 12)
    client.shared_budget.used.extend([time.monotonic_ns()] * 48)
    bot.enter = AsyncMock()
    bot.evaluate()
    assert bot.work is None
    bot.enter.assert_not_awaited()


def test_halted_bot_can_recover_to_close_but_cannot_rearm_entries():
    machine = StateMachine()
    machine.transition(State.HALTED)
    machine.transition(State.RECOVERY)
    machine.transition(State.FLAT)
    assert not machine.begin_entry()


async def test_explicit_manual_flatten_can_cancel_foreign_order(bot, client):
    initial = client.snapshot.return_value
    initial.orders = [{"client_order_index": 999, "order_index": 1}]
    confirmed = replace(initial, orders=[])
    client.snapshot.return_value = confirmed
    await bot._resolve_intents(initial, allow_foreign_orders=True)
    client.cancel.assert_awaited_once_with(initial.orders[0])


async def test_stale_rest_replica_cannot_erase_newer_private_exposure(bot, client):
    bot.position = Position(D(1), D(100), time.monotonic_ns())
    bot.last_account_transaction = 200
    old = replace(
        client.snapshot.return_value, account={"transaction_time": 100}, position=Position()
    )
    with pytest.raises(ExchangeError, match="BEHIND_PRIVATE"):
        bot._apply_snapshot(old)
    assert bot.position.quantity == 1
