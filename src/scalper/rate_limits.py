"""Sliding-window policy caps, with transaction and HTTP capacity reserved for recovery."""

import asyncio
import time
from collections import deque


class RateBudget:
    def __init__(self, capacity: int, reserve: int) -> None:
        self.capacity, self.reserve = capacity, reserve
        self.used: deque[int] = deque()
        self.blocked_until_ns = 0

    def headroom(self, now_ns: int) -> int:
        while self.used and self.used[0] <= now_ns - 60_000_000_000:
            self.used.popleft()
        return self.capacity - len(self.used)

    def available(self, now_ns: int, *, priority: bool, amount: int = 1) -> bool:
        return now_ns >= self.blocked_until_ns and self.headroom(now_ns) > (
            amount - 1 if priority else self.reserve + amount - 1
        )

    def consume(self, now_ns: int, *, priority: bool, amount: int = 1) -> bool:
        if not self.available(now_ns, priority=priority, amount=amount):
            return False
        self.used.extend([now_ns] * amount)
        return True

    async def acquire(self, *, priority: bool, amount: int = 1) -> None:
        if amount > self.capacity:
            raise ValueError("Endpoint weight exceeds configured rate budget")
        deadline = time.monotonic_ns() + 65_000_000_000
        while not self.consume(time.monotonic_ns(), priority=priority, amount=amount):
            if time.monotonic_ns() > deadline:
                raise TimeoutError("Rate-limit capacity unavailable")
            await asyncio.sleep(0.05)

    def rejected(self, now_ns: int) -> None:
        self.blocked_until_ns = now_ns + 60_000_000_000
