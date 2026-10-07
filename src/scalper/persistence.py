"""SQLite journal and exclusive process lock. Disk I/O runs outside the event loop."""

import asyncio
import fcntl
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProcessLock:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (directory / "execution.lock").open("a")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError(
                "Another scalper or flatten process owns this data directory; stop it first"
            ) from None

    def close(self) -> None:
        self.file.close()


class Journal:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "journal.sqlite3"
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS intents (
                client_id INTEGER PRIMARY KEY, created_ns INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fills (
                trade_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                trade_uuid TEXT PRIMARY KEY, utc_day TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS status (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT);
        """)
        self.connection.commit()
        self.lock = asyncio.Lock()
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=4096)
        self.failure = False
        self.worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.worker = asyncio.create_task(self._writer(), name="journal")

    def emit(self, kind: str, payload: Any) -> bool:
        if self.failure:
            return False
        try:
            self.queue.put_nowait((kind, payload))
            return True
        except asyncio.QueueFull:
            self.failure = True
            return False

    async def prepare(self, kind: str, payload: dict[str, Any]) -> int:
        # A durable intent precedes submission. This runs in the execution worker,
        # never in the market-event callback, and is necessary for crash reconciliation.
        async with self.lock:
            return await asyncio.to_thread(self._prepare, kind, payload)

    def _prepare(self, kind: str, payload: dict[str, Any]) -> int:
        payload.setdefault("phase", "prepared")
        largest = self.connection.execute("SELECT MAX(client_id) FROM intents").fetchone()[0] or 0
        client_id = max(time.time_ns() // 1000 % (2**48), largest + 1)
        if client_id >= 2**48:
            raise RuntimeError("Client order ID space exhausted")
        self.connection.execute(
            "INSERT INTO intents(client_id,created_ns,kind,payload) VALUES(?,?,?,?)",
            (client_id, time.time_ns(), kind, json.dumps(payload, default=str)),
        )
        self.connection.commit()
        return client_id

    async def annotate(self, client_id: int, tx_hash: str, nonce: int) -> None:
        async with self.lock:
            await asyncio.to_thread(self._annotate, client_id, tx_hash, nonce)

    def _annotate(self, client_id: int, tx_hash: str, nonce: int) -> None:
        row = self.connection.execute(
            "SELECT payload FROM intents WHERE client_id=?", (client_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Missing durable order intent")
        payload = json.loads(row[0])
        payload.update(phase="signed", tx_hash=tx_hash, nonce=nonce)
        self.connection.execute(
            "UPDATE intents SET payload=? WHERE client_id=?", (json.dumps(payload), client_id)
        )
        self.connection.commit()

    async def unresolved(self) -> list[dict[str, Any]]:
        async with self.lock:
            return await asyncio.to_thread(
                lambda: [
                    dict(client_id=r[0], kind=r[1], **json.loads(r[2]))
                    for r in self.connection.execute(
                        "SELECT client_id,kind,payload FROM intents WHERE resolved=0"
                    )
                ]
            )

    async def _writer(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                async with self.lock:
                    await asyncio.to_thread(self._write, *item)
            except (sqlite3.Error, OSError, ValueError):
                self.failure = True
            finally:
                self.queue.task_done()

    def _write(self, kind: str, payload: Any) -> None:
        encoded = json.dumps(payload, default=str)
        if kind == "resolved":
            self.connection.execute(
                "UPDATE intents SET resolved=1 WHERE client_id=?", (int(payload),)
            )
        elif kind == "fill":
            self.connection.execute(
                "INSERT OR IGNORE INTO fills VALUES(?,?)", (int(payload["trade_id"]), encoded)
            )
        elif kind == "trade":
            self.connection.execute(
                "INSERT OR REPLACE INTO trades VALUES(?,?,?)",
                (payload["trade_uuid"], payload["utc_day"], encoded),
            )
        elif kind == "status":
            self.connection.execute("INSERT OR REPLACE INTO status VALUES(1,?)", (encoded,))
        else:
            raise ValueError("Unknown journal event")
        self.connection.commit()
        if kind == "trade":
            self._summary(payload["utc_day"])

    def _summary(self, day: str) -> None:
        rows = [
            json.loads(row[0])
            for row in self.connection.execute("SELECT payload FROM trades WHERE utc_day=?", (day,))
        ]
        complete = [r for r in rows if r.get("accounting_complete")]
        profits = [float(r["net_realized_pnl"]) for r in complete]
        result = {
            "utc_day": day,
            "trades": len(rows),
            "accounting_complete": len(complete),
            "wins": sum(p > 0 for p in profits),
            "losses": sum(p < 0 for p in profits),
            "net_realized_pnl": sum(profits),
            "largest_win": max(profits, default=0),
            "largest_loss": min(profits, default=0),
            "gross_pnl": sum(float(r["gross_pnl"]) for r in complete),
            "fees": sum(float(r["fees"]) for r in complete),
            "average_hold_ms": sum(r["holding_ms"] for r in rows) / len(rows),
        }
        target = self.path.parent / f"summary-{day}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(result, indent=2))
        temp.replace(target)

    async def close(self) -> None:
        await self.queue.join()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await asyncio.to_thread(self.connection.close)


def local_status(directory: Path) -> dict[str, Any]:
    path = directory / "journal.sqlite3"
    if not path.exists():
        return {"status": "never started"}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT payload FROM status WHERE id=1").fetchone()
        result = json.loads(row[0]) if row else {"status": "no heartbeat"}
        summary = directory / f"summary-{datetime.now(UTC).date().isoformat()}.json"
        if summary.is_file():
            result["today"] = json.loads(summary.read_text())
        return result
