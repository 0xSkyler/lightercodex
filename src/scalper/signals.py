"""Bounded event windows and deterministic, symmetric microstructure arithmetic."""

import math
from collections import deque
from dataclasses import dataclass

from scalper.orderbook import OrderBook


@dataclass(frozen=True)
class Signal:
    direction: int
    score: float
    components: tuple[float, ...]
    volatility_bps: float


class SignalEngine:
    def __init__(self, depth: int, weights: tuple[float, ...], threshold: float) -> None:
        self.depth, self.weights, self.threshold = depth, weights, threshold
        self.quotes: deque[tuple[int, float, float, float]] = deque(maxlen=20000)
        self.trades: deque[tuple[int, float]] = deque(maxlen=20000)
        self.trade_ids: deque[int] = deque(maxlen=20000)
        self.seen: set[int] = set()

    def quote(self, book: OrderBook, now_ns: int) -> None:
        self.quotes.append((now_ns, float(book.mid), float(book.bid), float(book.ask)))
        self._prune(now_ns)

    def trade(self, trade_id: int, size: float, *, buyer_aggressive: bool, now_ns: int) -> None:
        if trade_id in self.seen:
            return
        if len(self.trade_ids) == self.trade_ids.maxlen:
            self.seen.discard(self.trade_ids[0])
        self.trade_ids.append(trade_id)
        self.seen.add(trade_id)
        self.trades.append((now_ns, size if buyer_aggressive else -size))
        self._prune(now_ns)

    def reset(self) -> None:
        self.quotes.clear()
        self.trades.clear()
        self.trade_ids.clear()
        self.seen.clear()

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - 10_000_000_000
        for values in (self.quotes, self.trades):
            while values and values[0][0] < cutoff:
                values.popleft()

    def calculate(self, book: OrderBook, now_ns: int) -> Signal:
        bids, asks = (
            book.levels(buy=False, count=self.depth),
            book.levels(buy=True, count=self.depth),
        )
        bd, ad = sum(float(q) for _, q in bids), sum(float(q) for _, q in asks)
        imbalance = (bd - ad) / (bd + ad)
        active = [q for t, q in self.trades if t >= now_ns - 500_000_000]
        volume = sum(abs(q) for q in active)
        flow = sum(active) / volume if volume else 0.0
        recent = [row for row in self.quotes if row[0] >= now_ns - 1_000_000_000]
        mid = float(book.mid)
        returns = []
        for horizon in (100_000_000, 250_000_000, 500_000_000, 1_000_000_000):
            reference = next(
                (q[1] for q in reversed(self.quotes) if q[0] <= now_ns - horizon), None
            )
            if reference:
                returns.append((mid / reference - 1) * 10000)
        momentum = math.tanh(sum(returns) / len(returns)) if returns else 0.0
        changes = [(b[2] - a[2]) + (b[3] - a[3]) for a, b in zip(recent, recent[1:], strict=False)]
        bbo = sum((x > 0) - (x < 0) for x in changes) / len(changes) if changes else 0.0
        bq, aq = float(book.bids[book.bid]), float(book.asks[book.ask])
        micro = (float(book.ask) * bq + float(book.bid) * aq) / (bq + aq)
        micro_bias = (micro - mid) / (float(book.ask - book.bid) / 2)
        prior_volume = sum(
            abs(q) for t, q in self.trades if now_ns - 1_000_000_000 <= t < now_ns - 500_000_000
        )
        acceleration = (
            flow * max(0.0, (volume - prior_volume) / (volume + prior_volume))
            if volume + prior_volume
            else 0.0
        )
        components = (imbalance, flow, momentum, bbo, micro_bias, acceleration)
        score = sum(w * c for w, c in zip(self.weights, components, strict=True)) / sum(
            self.weights
        )
        increments = [(b[1] / a[1] - 1) * 10000 for a, b in zip(recent, recent[1:], strict=False)]
        volatility = math.sqrt(sum(r * r for r in increments))
        # Warm up one second and require current trade-flow evidence; historical subscription fills do not qualify.
        warm = bool(self.quotes and now_ns - self.quotes[0][0] >= 1_000_000_000 and active)
        direction = (
            (1 if score >= self.threshold else -1 if score <= -self.threshold else 0) if warm else 0
        )
        return Signal(direction, score, components, volatility)
