import asyncio

import pytest

from scalper.config import ConfigError, load_config
from scalper.persistence import Journal, ProcessLock, local_status
from scalper.rate_limits import RateBudget
from scalper.state_machine import State, StateMachine


def valid_environment():
    return {
        "LIGHTER_ACCOUNT_INDEX": "123",
        "LIGHTER_API_KEY_INDEX": "3",
        "LIGHTER_API_PRIVATE_KEY": "0" * 80,
        "LEVERAGE": "25",
        "FEE_TICK_SCALE": "1000000",
        "EXPECTED_TAKER_FEE_TICK": "0",
        "TX_PER_MINUTE": "100",
        "HTTP_READS_PER_MINUTE": "300",
    }


def test_live_gates_and_secret_representation():
    values = valid_environment()
    with pytest.raises(ConfigError, match="Live execution requires"):
        load_config(None, live=True, environ=values)
    values.update(LIVE_TRADING="true", I_UNDERSTAND_THIS_USES_REAL_FUNDS="YES")
    config = load_config(None, live=True, environ=values)
    assert values["LIGHTER_API_PRIVATE_KEY"] not in repr(config)
    assert config.desired_notional == 250


@pytest.mark.parametrize(
    "name,value",
    [
        ("LEVERAGE", "0"),
        ("LEVERAGE", "30"),
        ("MAX_LOSS_USD", "0"),
        ("MIN_PROFIT_USD", "NaN"),
        ("MIN_PROFIT_USD", "-1"),
        ("POSITION_MODE", "martingale"),
        ("MARKET", "ETH"),
        ("LIGHTER_URL", "https://testnet.zklighter.elliot.ai"),
        ("LIGHTER_API_KEY_INDEX", "255"),
        ("LIGHTER_API_PRIVATE_KEY", "1" * 64),
        ("TX_PER_MINUTE", "4"),
        ("HTTP_READS_PER_MINUTE", "1"),
        ("MAX_EMERGENCY_EXIT_SLIPPAGE_BPS", "0.5"),
        ("ENTRY_SCORE_THRESHOLD", "2"),
        ("MARGIN_MODE", "1"),
    ],
)
def test_invalid_config_fails_closed(name, value):
    values = valid_environment()
    values[name] = value
    with pytest.raises(ConfigError):
        load_config(None, environ=values)


def test_env_permissions_and_direct_process_variables(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LEVERAGE=20\n")
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="permissions 600"):
        load_config(str(path), environ=valid_environment())
    path.chmod(0o600)
    assert load_config(str(path), environ=valid_environment()).leverage == 25
    assert load_config("/dev/null", environ=valid_environment()).leverage == 25


async def test_atomic_entry_transition_ignores_duplicate_signals():
    machine = StateMachine()
    machine.transition(State.SYNCING)
    machine.transition(State.FLAT)

    async def attempt():
        await asyncio.sleep(0)
        return machine.begin_entry()

    assert sum(await asyncio.gather(*(attempt() for _ in range(100)))) == 1
    machine.transition(State.PARTIALLY_FILLED)
    machine.transition(State.OPEN_LONG)
    machine.transition(State.EXIT_PENDING)
    machine.transition(State.PARTIAL_EXIT)
    machine.transition(State.EXIT_PENDING)
    machine.transition(State.FLAT)
    with pytest.raises(RuntimeError):
        machine.transition(State.OPEN_SHORT)


def test_reserved_rate_capacity_and_window_expiry():
    budget = RateBudget(10, 4)
    for _ in range(6):
        assert budget.consume(1, priority=False)
    assert not budget.consume(1, priority=False)
    for _ in range(4):
        assert budget.consume(1, priority=True)
    assert not budget.consume(1, priority=True)
    assert budget.headroom(60_000_000_001) == 10
    budget.rejected(60_000_000_001)
    assert not budget.available(60_000_000_002, priority=True)


async def test_durable_intents_status_and_reopen(config):
    journal = Journal(config.data_dir)
    journal.start()
    cid = await journal.prepare("entry", {"quantity": "1", "nonce": 10})
    assert 0 < cid < 2**48
    journal.emit("status", {"state": "ENTRY_PENDING"})
    await journal.close()
    assert local_status(config.data_dir)["state"] == "ENTRY_PENDING"
    reopened = Journal(config.data_dir)
    reopened.start()
    assert (await reopened.unresolved())[0]["client_id"] == cid
    reopened.emit("resolved", cid)
    await reopened.queue.join()
    assert await reopened.unresolved() == []
    await reopened.close()


def test_only_one_process_can_execute(config):
    lock = ProcessLock(config.data_dir)
    try:
        with pytest.raises(RuntimeError, match="Another"):
            ProcessLock(config.data_dir)
    finally:
        lock.close()
