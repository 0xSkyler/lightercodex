"""Bounded metrics, monotonic nanosecond timing, and exact fill accounting."""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from scalper.config import D


class Metrics:
    def __init__(self) -> None:
        self.samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=2048))
        self.counts: dict[str, int] = defaultdict(int)

    def measure(self, name: str, start_ns: int, finish_ns: int) -> None:
        self.samples[name].append((finish_ns - start_ns) / 1_000_000)

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.counts)
        for name, rows in self.samples.items():
            values = sorted(rows)
            result[name] = {
                "p50_ms": values[int((len(values) - 1) * 0.5)],
                "p95_ms": values[int((len(values) - 1) * 0.95)],
            }
        return result


@dataclass
class TradeCycle:
    direction: int
    leverage: int
    score: float = 0
    components: tuple[float, ...] = ()
    trade_uuid: str = field(default_factory=lambda: str(uuid4()))
    timestamps: dict[str, int] = field(default_factory=dict)
    entry_size: Decimal = D(0)
    entry_quote: Decimal = D(0)
    exit_size: Decimal = D(0)
    exit_quote: Decimal = D(0)
    fees: Decimal = D(0)
    fees_known: bool = True
    recovered: bool = False
    favorable: Decimal = D(0)
    adverse: Decimal = D(0)
    exit_reason: str = ""
    entry_reference: Decimal = D(0)
    exit_reference: Decimal = D(0)
    estimated_slippage_usd: Decimal = D(0)
    utc_started: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def fill(
        self,
        *,
        entry: bool,
        size: Decimal,
        price: Decimal,
        fee_tick: int | None,
        fee_scale: int,
        now_ns: int,
    ) -> None:
        if entry:
            self.entry_size += size
            self.entry_quote += size * price
            self.timestamps.setdefault("first_fill", now_ns)
            self.timestamps["entry_fill"] = now_ns
        else:
            self.exit_size += size
            self.exit_quote += size * price
            self.timestamps["exit_fill"] = now_ns
        if fee_tick is None:
            self.fees_known = False
        else:
            self.fees += size * price * D(fee_tick) / fee_scale

    def finish(self, now_ns: int) -> dict[str, Any]:
        self.timestamps["flat"] = now_ns
        complete = (
            not self.recovered
            and self.fees_known
            and self.entry_size > 0
            and self.entry_size == self.exit_size
        )
        gross = D(self.direction) * (self.exit_quote - self.entry_quote)
        start = self.timestamps.get("first_fill", now_ns)
        average_entry = self.entry_quote / self.entry_size if self.entry_size else D(0)
        average_exit = self.exit_quote / self.exit_size if self.exit_size else D(0)
        return {
            "trade_uuid": self.trade_uuid,
            "utc_day": datetime.now(UTC).date().isoformat(),
            "side": "LONG" if self.direction > 0 else "SHORT",
            "leverage": self.leverage,
            "size": str(self.entry_size),
            "signal_score": self.score,
            "signal_components": self.components,
            "utc_started": self.utc_started,
            "utc_completed": datetime.now(UTC).isoformat(),
            "timestamps_ns": self.timestamps,
            "average_entry": str(average_entry),
            "average_exit": str(average_exit),
            "entry_slippage_bps": str(
                D(self.direction)
                * (average_entry - self.entry_reference)
                / self.entry_reference
                * 10000
            )
            if self.entry_reference
            else None,
            "exit_slippage_bps": str(
                D(self.direction)
                * (self.exit_reference - average_exit)
                / self.exit_reference
                * 10000
            )
            if self.exit_reference
            else None,
            "estimated_slippage_usd": str(self.estimated_slippage_usd),
            "holding_ms": (now_ns - start) / 1_000_000,
            "gross_pnl": str(gross) if complete else None,
            "fees": str(self.fees),
            "net_realized_pnl": str(gross - self.fees) if complete else None,
            "accounting_complete": complete,
            "recovered": self.recovered,
            "maximum_favorable_excursion_usd": str(self.favorable),
            "maximum_adverse_excursion_usd": str(self.adverse),
            "exit_reason": self.exit_reason,
        }
