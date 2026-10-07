"""Decimal price-level book with absolute size updates and bounded memory."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from scalper.config import BPS, D


class BookError(ValueError):
    """Book cannot be trusted until a new snapshot arrives."""


@dataclass(frozen=True)
class Market:
    index: int
    size_decimals: int
    price_decimals: int
    min_size: Decimal
    min_notional: Decimal
    min_imf: int
    quote_limit: Decimal

    @classmethod
    def discover(cls, rows: list[dict[str, Any]]) -> "Market":
        candidates = [
            r
            for r in rows
            if r.get("symbol") == "BTC"
            and r.get("market_type") == "perp"
            and r.get("status") == "active"
        ]
        if len(candidates) != 1:
            raise BookError("Exactly one active BTC perpetual market is required")
        r = candidates[0]
        m = cls(
            int(r["market_id"]),
            int(r["supported_size_decimals"]),
            int(r["supported_price_decimals"]),
            D(r["min_base_amount"]),
            D(r["min_quote_amount"]),
            int(r["min_initial_margin_fraction"]),
            D(r["order_quote_limit"]),
        )
        if not (
            0 <= m.size_decimals <= 12
            and 0 <= m.price_decimals <= 12
            and m.min_size > 0
            and m.min_notional > 0
            and m.min_imf > 0
        ):
            raise BookError("Invalid BTC market constraints")
        return m

    def size_units(self, quantity: Decimal) -> int:
        return int((quantity * 10**self.size_decimals).to_integral_value(rounding=ROUND_FLOOR))

    def size(self, quantity: Decimal) -> Decimal:
        return D(self.size_units(quantity)) / 10**self.size_decimals

    def price_units(self, price: Decimal, *, buy: bool) -> int:
        # Conservative rounding: a buy never exceeds its cap, a sell never falls below its floor.
        units = int(
            (price * 10**self.price_decimals).to_integral_value(
                rounding=ROUND_FLOOR if buy else ROUND_CEILING
            )
        )
        if units <= 0 or units > 2**32 - 1:
            raise BookError("Order price exceeds native signer bounds")
        return units


@dataclass(frozen=True)
class Sweep:
    vwap: Decimal
    filled: Decimal
    requested: Decimal
    worst_price: Decimal

    @property
    def complete(self) -> bool:
        return self.filled == self.requested


class OrderBook:
    def __init__(self, max_levels: int = 5000) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.max_levels = max_levels
        self.offset: int | None = None
        self.nonce: int | None = None
        self.received_ns = 0
        self.valid = False

    def update(self, payload: dict[str, Any], now_ns: int, *, snapshot: bool = False) -> bool:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
            self.offset = self.nonce = None
            self.valid = False
        elif not self.valid:
            raise BookError("Delta before snapshot")
        offset = payload.get("offset")
        if offset is None:
            raise BookError("Missing book offset")
        offset = int(offset)
        if self.offset is not None and offset <= self.offset:
            return False
        if "begin_nonce" in payload and self.nonce is not None:
            if int(payload["begin_nonce"]) != self.nonce:
                self.valid = False
                raise BookError("Book nonce gap; resubscription required")
        try:
            for name, side in (("bids", self.bids), ("asks", self.asks)):
                for row in payload[name]:
                    price, size = D(str(row["price"])), D(str(row["size"]))
                    if not price.is_finite() or not size.is_finite() or price <= 0 or size < 0:
                        raise BookError("Invalid book level")
                    if size:
                        side[price] = size
                    else:
                        side.pop(price, None)
                if len(side) > self.max_levels:
                    raise BookError("Book memory bound exceeded; resubscription required")
            if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
                raise BookError("Empty or crossed book")
        except (ValueError, KeyError, ArithmeticError):
            self.valid = False
            raise
        self.offset = offset
        self.nonce = int(payload["nonce"]) if "nonce" in payload else None
        self.received_ns = now_ns
        self.valid = True
        return True

    @property
    def bid(self) -> Decimal:
        return max(self.bids)

    @property
    def ask(self) -> Decimal:
        return min(self.asks)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * BPS

    def levels(self, *, buy: bool, count: int | None = None) -> list[tuple[Decimal, Decimal]]:
        side = self.asks if buy else self.bids
        prices = sorted(side, reverse=not buy)
        if count is not None:
            prices = prices[:count]
        return [(p, side[p]) for p in prices]

    def sweep(self, quantity: Decimal, *, buy: bool, limit: Decimal | None = None) -> Sweep:
        if not self.valid or quantity <= 0:
            raise BookError("Sweep requires a valid book and positive size")
        quote, filled, worst = D(0), D(0), D(0)
        for price, size in self.levels(buy=buy):
            if limit is not None and ((buy and price > limit) or (not buy and price < limit)):
                break
            taken = min(size, quantity - filled)
            filled += taken
            quote += price * taken
            worst = price
            if filled == quantity:
                break
        return Sweep(quote / filled if filled else D(0), filled, quantity, worst)

    def fresh(self, now_ns: int, stale_ms: int) -> bool:
        return self.valid and 0 <= now_ns - self.received_ns <= stale_ms * 1_000_000
