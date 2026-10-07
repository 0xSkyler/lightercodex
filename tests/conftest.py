import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scalper.config import Config, D
from scalper.lighter_client import Snapshot
from scalper.metrics import Metrics
from scalper.orderbook import Market, OrderBook
from scalper.persistence import Journal
from scalper.pnl import Position
from scalper.rate_limits import RateBudget
from scalper.state_machine import State
from scalper.strategy import Bot


@pytest.fixture
def config(tmp_path):
    return Config(
        account_index=123,
        api_key_index=3,
        private_key="0" * 80,
        leverage=25,
        fee_tick_scale=1000000,
        expected_taker_fee_tick=0,
        tx_per_minute=100,
        reads_per_minute=300,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def market():
    return Market(42, 5, 2, D("0.0001"), D(1), 100, D(100000))


@pytest.fixture
def book():
    b = OrderBook()
    b.update(
        {
            "bids": [{"price": "100", "size": "10"}],
            "asks": [{"price": "100.01", "size": "10"}],
            "offset": 1,
        },
        time.monotonic_ns(),
        snapshot=True,
    )
    return b


@pytest.fixture
async def journal(config):
    j = Journal(config.data_dir)
    j.start()
    yield j
    await j.close()


@pytest.fixture
def client(config, market):
    now = time.monotonic_ns()
    snap = Snapshot(
        {"available_balance": "1000", "pending_order_count": 0, "positions": []},
        None,
        Position(),
        [],
        now,
    )
    return SimpleNamespace(
        market=market,
        nonce=10,
        signer=SimpleNamespace(),
        tier="standard",
        volume_quota=None,
        tx_budget=RateBudget(100, 4),
        read_budget=RateBudget(300, 8),
        snapshot=AsyncMock(return_value=snap),
        refresh_nonce=AsyncMock(),
        lookup=AsyncMock(),
        recent_fills=AsyncMock(return_value=[]),
        cancel=AsyncMock(),
        order=AsyncMock(),
        verify_leverage=AsyncMock(),
        validate_limits=AsyncMock(),
        discover=AsyncMock(return_value=market),
        connect=AsyncMock(),
        metrics=Metrics(),
        config=config,
    )


@pytest.fixture
def bot(config, journal, client, market, book):
    b = Bot(config, journal, client, client.metrics)
    b.market = market
    b.book = book
    b.machine.transition(State.SYNCING)
    b.machine.transition(State.FLAT)
    now = time.monotonic_ns()
    b.streams = SimpleNamespace(public_ready=True, account_ready=True, account_ns=now)
    b.last_reconcile_ns = now
    b.snapshot = client.snapshot.return_value
    return b
