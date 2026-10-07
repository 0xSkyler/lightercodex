"""Isolated browser-test server. No exchange connections or order execution are possible."""

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from aiohttp import web

from scalper.dashboard import Controller, Settings, create_app


class FixtureController(Controller):
    async def diagnose(self, command: str) -> str:
        assert command == "account-info"
        return json.dumps(
            {
                "account_index": 123,
                "tier": 0,
                "tier_name": "Standard",
                "current_taker_fee_tick": 0,
                "available_balance": "37.50",
                "BTC_positions": [],
            }
        )

    async def spawn(self, command: str, live: bool = False) -> asyncio.subprocess.Process:
        assert command in ("run", "flatten") and live
        code = "print('LOCAL BROWSER FIXTURE — NO EXCHANGE ORDERS',flush=True);"
        if command == "run":
            code += "import signal,time;signal.signal(signal.SIGTERM,lambda *args:exit(0));time.sleep(120)"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )


async def run(root: Path) -> None:
    controller = FixtureController(Settings(root / "settings.env"), root / "data", root / "logs")
    now = time.time()
    controller.market.current = {
        "connected": True,
        "received_at": now,
        "mid": 83000.1,
        "bid": "83000.0",
        "ask": "83000.2",
        "spread_bps": "0.02",
    }
    controller.market.samples.extend(
        {"time": now - 50 + i, "value": 83000 + i / 10} for i in range(50)
    )
    runner = web.AppRunner(create_app(controller, market_feed=False))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    print(f"http://127.0.0.1:{runner.addresses[0][1]}", flush=True)
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.root))
