"""Executable closing economics; depth impact is included once in the VWAP."""

from dataclasses import dataclass
from decimal import Decimal

from scalper.config import BPS, D
from scalper.orderbook import OrderBook

ZERO = D(0)


@dataclass
class Position:
    quantity: Decimal = D(0)  # signed BTC, positive is long
    entry: Decimal = D(0)
    opened_ns: int = 0

    @property
    def size(self) -> Decimal:
        return abs(self.quantity)


@dataclass(frozen=True)
class CloseEstimate:
    close_vwap: Decimal
    gross_pnl: Decimal
    fees: Decimal
    estimated_slippage: Decimal
    expected_net_pnl: Decimal
    available_liquidity: Decimal
    complete: bool
    profitable: bool


def estimate_close(
    position: Position,
    book: OrderBook,
    *,
    taker_fee: Decimal,
    min_profit_usd: Decimal,
    min_profit_bps: Decimal = ZERO,
    execution_buffer_bps: Decimal = ZERO,
    safety_buffer_usd: Decimal = ZERO,
    limit: Decimal | None = None,
) -> CloseEstimate:
    sweep = book.sweep(position.size, buy=position.quantity < 0, limit=limit)
    direction = D(1) if position.quantity > 0 else D(-1)
    gross = direction * (sweep.vwap - position.entry) * sweep.filled
    fees = (position.entry + sweep.vwap) * sweep.filled * taker_fee
    buffer = sweep.vwap * sweep.filled * execution_buffer_bps / BPS
    net = gross - fees - buffer - safety_buffer_usd
    minimum = max(min_profit_usd, position.entry * position.size * min_profit_bps / BPS)
    return CloseEstimate(
        sweep.vwap,
        gross,
        fees,
        buffer,
        net,
        sweep.filled,
        sweep.complete,
        sweep.complete and net > minimum,
    )
