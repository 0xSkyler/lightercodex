"""Dashboard boundary tests. All exchange access and trading processes are mocked."""

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from scalper.dashboard import (
    Controller,
    DashboardError,
    Settings,
    create_app,
    journal_view,
    redact,
    saved_activity,
)
from scalper.persistence import Journal, ProcessLock


def values():
    return {
        "LIGHTER_ACCOUNT_INDEX": "123",
        "LIGHTER_API_KEY_INDEX": "3",
        "LIGHTER_API_PRIVATE_KEY": "a" * 80,
        "LEVERAGE": "5",
        "EXPECTED_TAKER_FEE_TICK": "0",
        "TX_PER_MINUTE": "30",
        "HTTP_READS_PER_MINUTE": "40",
    }


@pytest.fixture
def controller(tmp_path):
    settings = Settings(tmp_path / "settings.env")
    settings.save(values())
    return Controller(settings, tmp_path / "data", tmp_path / "logs")


@asynccontextmanager
async def client_for(controller):
    runner = web.AppRunner(create_app(controller, market_feed=False, guard=False))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    url = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession(base_url=url) as client:
        async with client.get("/api/bootstrap") as response:
            token = (await response.json())["token"]
        yield client, {"X-Dashboard-Token": token, "Origin": url}
    await runner.cleanup()


def test_key_is_never_returned_and_blank_preserves_it(controller):
    public = controller.settings.public()
    assert public["key_saved"]
    assert "a" * 80 not in json.dumps(public)
    assert "LIGHTER_API_PRIVATE_KEY" not in public["values"]
    controller.settings.save({"LIGHTER_API_PRIVATE_KEY": "", "LEVERAGE": "10"})
    assert controller.settings.values()["LIGHTER_API_PRIVATE_KEY"] == "a" * 80
    assert controller.settings.path.stat().st_mode & 0o777 == 0o600
    assert controller.settings.values()["LIVE_TRADING"] == ""
    assert controller.settings.values()["I_UNDERSTAND_THIS_USES_REAL_FUNDS"] == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"LIGHTER_API_PRIVATE_KEY": "x" * 80},
        {"LIGHTER_ACCOUNT_INDEX": "12\nLIVE_TRADING=true"},
        {"LIGHTER_API_KEY_INDEX": "2"},
        {"LEVERAGE": "3"},
        {"DATA_DIR": "/tmp/other"},
        {"LIVE_TRADING": "true"},
        {"LIGHTER_URL": "https://evil.test"},
        {"LEVERAGE": 5},
    ],
)
def test_settings_reject_invalid_or_unapproved_values(controller, changes):
    before = controller.settings.path.read_bytes()
    with pytest.raises((DashboardError, ValueError)):
        controller.settings.save(changes)
    assert controller.settings.path.read_bytes() == before


def test_settings_ignore_parent_process_config(controller, monkeypatch):
    monkeypatch.setenv("LIGHTER_API_PRIVATE_KEY", "b" * 80)
    monkeypatch.setenv("LEVERAGE", "50")
    monkeypatch.setenv("LIVE_TRADING", "true")
    env = controller.environment()
    assert "LIGHTER_API_PRIVATE_KEY" not in env
    assert "LEVERAGE" not in env
    assert env["LIVE_TRADING"] == ""
    assert controller.settings.values()["LEVERAGE"] == "5"
    assert controller.environment(live=True)["I_UNDERSTAND_THIS_USES_REAL_FUNDS"] == "YES"


def test_partial_setup_can_save_credentials_without_risk_settings(tmp_path):
    settings = Settings(tmp_path / "settings.env")
    settings.save({k: v for k, v in values().items() if k.startswith("LIGHTER_")})
    assert "LEVERAGE" in settings.public()["missing"]
    assert settings.public()["key_saved"]


def test_symlink_settings_are_rejected(tmp_path):
    target = tmp_path / "target.env"
    target.write_text("value")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    with pytest.raises(DashboardError):
        Settings(link).values()


async def test_requests_require_local_host_origin_and_session(controller):
    async with client_for(controller) as (client, headers):
        async with client.get("/api/state") as response:
            assert response.status == 403
        async with client.get("/api/bootstrap", headers={"Host": "attacker.example"}) as response:
            assert response.status == 403
        async with client.get(
            "/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"}
        ) as response:
            assert response.status == 403
        bad = {**headers, "Origin": "https://attacker.example"}
        async with client.post("/api/settings", json={"values": {}}, headers=bad) as response:
            assert response.status == 403
        async with client.post(
            "/api/settings",
            json={"values": {}},
            headers={"X-Dashboard-Token": headers["X-Dashboard-Token"]},
        ) as response:
            assert response.status == 403
        async with client.get("/api/state", headers=headers) as response:
            assert response.status == 200
            body = await response.text()
            assert "a" * 80 not in body
            assert response.headers["Cache-Control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_assets_are_packaged_and_do_not_allow_path_traversal(controller):
    async with client_for(controller) as (client, _):
        for asset in ("/", "/assets/dashboard.css", "/assets/dashboard.js"):
            async with client.get(asset) as response:
                assert response.status == 200
                assert len(await response.read()) > 1000
        async with client.get("/assets/dashboard.py") as response:
            assert response.status == 404


async def test_explicit_confirmation_is_required_for_start_and_flatten(controller, monkeypatch):
    start, flatten = AsyncMock(), AsyncMock()
    monkeypatch.setattr(controller, "start", start)
    monkeypatch.setattr(controller, "flatten", flatten)
    async with client_for(controller) as (client, headers):
        for action in ("start", "flatten"):
            async with client.post(f"/api/{action}", json={}, headers=headers) as response:
                assert response.status == 400
        start.assert_not_awaited()
        flatten.assert_not_awaited()
        async with client.post(
            "/api/start", json={"confirmation": "START LIVE"}, headers=headers
        ) as response:
            assert response.status == 200
        async with client.post(
            "/api/flatten", json={"confirmation": "FLATTEN BTC"}, headers=headers
        ) as response:
            assert response.status == 200
        start.assert_awaited_once()
        flatten.assert_awaited_once()


async def test_credentials_change_invalidates_account_verification(controller, monkeypatch):
    data = {
        "account_index": 123,
        "current_taker_fee_tick": 0,
        "available_balance": "25",
        "tier_name": "Standard",
        "tier": 0,
        "BTC_positions": [],
    }
    diagnose = AsyncMock(return_value=json.dumps(data))
    monkeypatch.setattr(controller, "diagnose", diagnose)
    assert await controller.check_account() == data
    diagnose.assert_awaited_once_with("account-info")
    assert (await controller.state())["account"] == data
    controller.settings.save({"LIGHTER_API_KEY_INDEX": "4"})
    assert (await controller.state())["account"] is None
    with pytest.raises(DashboardError, match="Verify"):
        await controller.start()


async def test_unverified_and_wrong_fee_starts_never_spawn_a_bot(controller, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(controller, "spawn", spawn)
    with pytest.raises(DashboardError, match="Verify"):
        await controller.start()
    controller.account = {"current_taker_fee_tick": 280}
    controller.account_checked_at = time.time()
    controller.account_fingerprint = controller.settings.fingerprint()
    with pytest.raises(DashboardError, match="fee"):
        await controller.start()
    spawn.assert_not_awaited()


async def test_an_external_bot_blocks_dashboard_mutations(controller):
    lock = ProcessLock(controller.data_dir)
    try:
        assert controller.external_owner()
        with pytest.raises(DashboardError):
            await controller.start()
        async with client_for(controller) as (client, headers):
            async with client.post(
                "/api/settings", json={"values": {"LEVERAGE": "10"}}, headers=headers
            ) as response:
                assert response.status == 400
    finally:
        lock.close()


async def test_concurrent_control_requests_are_not_queued(controller):
    async with client_for(controller) as (client, headers):
        async with controller.lock:
            controller.busy = "flatten"
            async with client.post(
                "/api/start", json={"confirmation": "START LIVE"}, headers=headers
            ) as response:
                assert response.status == 409


async def test_start_stop_control_a_real_local_fake_process(controller, monkeypatch):
    # This process only sleeps/prints and never imports the exchange or execution engine.
    async def fake_spawn(command, live=False):
        assert command == "run" and live
        script = "import signal,time; signal.signal(signal.SIGTERM,lambda *args:exit(0)); print('fake ready',flush=True); time.sleep(60)"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    monkeypatch.setattr(controller, "spawn", fake_spawn)
    controller.account = {"current_taker_fee_tick": 0}
    controller.account_fingerprint = controller.settings.fingerprint()
    controller.account_checked_at = time.time()
    await controller.start()
    try:
        assert controller.running
        for _ in range(100):
            if controller.lines:
                break
            await asyncio.sleep(0.01)
        assert controller.lines[-1]["text"] == "fake ready"
        await controller.stop()
        assert not controller.running
        assert controller.last_exit == 0
    finally:
        if controller.running:
            controller.process.kill()
            await controller.process.wait()


async def test_flatten_completion_is_not_interrupted_by_stop(controller, monkeypatch):
    ready = asyncio.Event()

    async def fake_spawn(command, live=False):
        assert command == "flatten" and live
        script = "import time; print('flatten fixture',flush=True); time.sleep(.2)"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        ready.set()
        return process

    monkeypatch.setattr(controller, "spawn", fake_spawn)
    task = asyncio.create_task(controller.flatten())
    await ready.wait()
    await controller.stop()
    await task
    assert controller.last_exit == 0


async def test_journal_view_reads_actual_records_and_does_not_create_state(tmp_path):
    directory = tmp_path / "data"
    assert journal_view(directory)["trades"] == []
    assert not directory.exists()
    journal = Journal(directory)
    journal.start()
    journal.emit(
        "trade",
        {
            "trade_uuid": "fixture",
            "utc_day": "2026-10-07",
            "utc_completed": "2026-10-07T12:00:00Z",
            "accounting_complete": False,
            "holding_ms": 50,
        },
    )
    await journal.queue.join()
    assert journal_view(directory)["trades"][0]["trade_uuid"] == "fixture"
    await journal.close()


def test_secret_and_hex_payloads_are_redacted():
    assert "a" * 80 not in redact("credential " + "a" * 80)
    assert redact("key=secret", "secret") == "key=[REDACTED]"


async def test_expired_verification_cannot_start_an_order_process(controller, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(controller, "spawn", spawn)
    controller.account = {"current_taker_fee_tick": 0}
    controller.account_fingerprint = controller.settings.fingerprint()
    controller.account_checked_at = time.time() - 901
    with pytest.raises(DashboardError, match="expired"):
        await controller.start()
    spawn.assert_not_awaited()


def test_saved_activity_is_bounded_and_redacted(tmp_path):
    path = tmp_path / "scalper.jsonl"
    entries = [
        {"utc": "2026-10-07T00:00:00Z", "level": "INFO", "event": f"event {i} " + "a" * 80}
        for i in range(120)
    ]
    path.write_text("\n".join(json.dumps(row) for row in entries))
    rows = saved_activity(tmp_path, "a" * 80)
    assert len(rows) == 100
    assert "a" * 80 not in json.dumps(rows)
    assert "event 119" in rows[-1]["text"]


def test_log_redaction_precedes_display_truncation(controller):
    controller._line("prefix " + "x" * 1980 + " " + "a" * 80)
    assert "a" * 10 not in controller.lines[-1]["text"]
    assert len(controller.lines[-1]["text"]) <= 2000


async def test_oversized_json_is_rejected(controller):
    async with client_for(controller) as (client, headers):
        async with client.post(
            "/api/settings", json={"values": {"LEVERAGE": "1" * 20000}}, headers=headers
        ) as response:
            assert response.status == 413
