"""Queue-backed rotating structured UTC logging with credential redaction."""

import json
import logging
import logging.handlers
import queue
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self.secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        text = record.getMessage()
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return json.dumps(
            {
                "utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "event": text,
            }
        )


class DropHandler(logging.handlers.QueueHandler):
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # Logging cannot block exits. Journal failure is handled separately by the watchdog.
            pass


def configure(directory: Path, level: str, secret: str) -> logging.handlers.QueueListener:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            directory / "scalper.jsonl", maxBytes=5_000_000, backupCount=5
        ),
    ]
    formatter = JsonFormatter((secret, secret.removeprefix("0x")))
    for handler in outputs:
        handler.setFormatter(formatter)
    records: queue.Queue[Any] = queue.Queue(maxsize=4096)
    log = logging.getLogger("scalper")
    log.setLevel(level)
    log.handlers = [DropHandler(records)]
    log.propagate = False
    # SDK debug logs contain signed transaction information; never forward them.
    for name in ("lighter", "aiohttp", "websockets"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    listener = logging.handlers.QueueListener(records, *outputs)
    listener.start()
    return listener
