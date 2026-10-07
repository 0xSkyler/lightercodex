"""Official SDK transport, signing, nonce serialization, and authoritative reads."""

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import lighter

from scalper.config import Config, D
from scalper.metrics import Metrics
from scalper.orderbook import Market
from scalper.pnl import Position
from scalper.rate_limits import RateBudget


class ExchangeError(RuntimeError):
    """Sanitized failure; never expose SDK bodies, authentication, or signed transactions."""

    def __init__(self, category: str, *, uncertain: bool = False) -> None:
        super().__init__(category)
        self.category, self.uncertain = category, uncertain


def checked(response: Any) -> dict[str, Any]:
    data = response.to_dict() if hasattr(response, "to_dict") else response
    if data.get("code") != 200:
        raise ExchangeError("EXCHANGE_ERROR")
    return data


def parse_position(row: dict[str, Any] | None, opened_ns: int = 0) -> Position:
    if row is None:
        return Position()
    size, entry = D(str(row["position"])), D(str(row["avg_entry_price"]))
    sign = int(row["sign"])
    if (
        not size.is_finite()
        or not entry.is_finite()
        or size < 0
        or (size and (sign not in (-1, 1) or entry <= 0))
    ):
        raise ExchangeError("STATE_MISMATCH")
    return Position(size * sign, entry, opened_ns)


def terminal(order: dict[str, Any]) -> bool:
    status = str(order.get("status", "")).lower()
    return status == "filled" or status.startswith(("canceled", "cancelled", "expired", "rejected"))


@dataclass
class Snapshot:
    account: dict[str, Any]
    position_row: dict[str, Any] | None
    position: Position
    orders: list[dict[str, Any]]
    fetched_ns: int


class LighterClient:
    def __init__(self, config: Config, metrics: Metrics) -> None:
        self.config, self.metrics = config, metrics
        configuration = lighter.Configuration(host="https://mainnet.zklighter.elliot.ai")
        configuration.ssl_ca_cert = os.environ.get("SSL_CERT_FILE")
        configuration.retries = 2  # SDK retries safe GETs; POST is excluded in its REST client.
        self.api = lighter.ApiClient(configuration=configuration)
        self.accounts = lighter.AccountApi(self.api)
        self.orders = lighter.OrderApi(self.api)
        self.transactions = lighter.TransactionApi(self.api)
        self.signer: lighter.SignerClient | None = None
        self.market: Market | None = None
        self.nonce: int | None = None
        self.tx_lock = asyncio.Lock()
        self.read_budget = RateBudget(config.reads_per_minute, 8)
        self.tx_budget = RateBudget(config.tx_per_minute, config.exit_reserve)
        self.volume_quota: int | None = None
        self.tier: str | None = None
        self.weighted_reads = False
        self.shared_budget: RateBudget | None = None
        self.cancel_budget = RateBudget(40, 0)
        self.leverage_budget = RateBudget(40, 0)
        self.ws_sender: Any = None
        self.on_signed: Any = None

    async def _read(self, function: Any, **kwargs: Any) -> dict[str, Any]:
        weights = {
            "next_nonce": 6,
            "account_active_orders": 100,
            "account_orders": 100,
            "trades": 200,
        }
        weight = weights.get(getattr(function, "__name__", ""), 300) if self.weighted_reads else 1
        await self.read_budget.acquire(priority=True, amount=weight)
        if self.shared_budget:
            await self.shared_budget.acquire(priority=True)
        if self.signer is not None and "_headers" not in kwargs:
            kwargs["_headers"] = {"Authorization": self.auth()}
        start = time.monotonic_ns()
        try:
            response = await function(
                **kwargs, _request_timeout=self.config.request_timeout_ms / 1000
            )
            return checked(response)
        except lighter.ApiException as e:
            if e.status == 429:
                self.read_budget.rejected(time.monotonic_ns())
            raise ExchangeError(
                "AUTH_ERROR"
                if e.status in (401, 403)
                else "RATE_LIMIT_ERROR"
                if e.status == 429
                else "EXCHANGE_ERROR"
            ) from None
        except (TimeoutError, OSError) as e:
            raise ExchangeError("NETWORK_ERROR") from e
        finally:
            self.metrics.measure("api_request", start, time.monotonic_ns())

    async def discover(self) -> Market:
        result = await self._read(self.orders.order_book_details, filter="perp")
        self.market = Market.discover(result["order_book_details"])
        if self.config.leverage * self.market.min_imf > 10000:
            raise ExchangeError("CONFIG_ERROR: requested leverage exceeds BTC market maximum")
        if self.market.quote_limit and self.config.desired_notional > self.market.quote_limit:
            raise ExchangeError("CONFIG_ERROR: desired notional exceeds order quote limit")
        return self.market

    async def connect(self) -> None:
        self.signer = lighter.SignerClient(
            url="https://mainnet.zklighter.elliot.ai",
            account_index=self.config.account_index,
            api_private_keys={self.config.api_key_index: self.config.private_key},
            nonce_management_type=lighter.nonce_manager.NonceManagerType.NONE,
            chain_id=304,
        )
        error = await asyncio.to_thread(self.signer.check_client)
        if error:
            raise ExchangeError("AUTH_ERROR")
        await self.refresh_nonce()
        await self.validate_limits()

    def auth(self) -> str:
        if self.signer is None:
            raise ExchangeError("AUTH_ERROR")
        token, error = self.signer.create_auth_token_with_expiry(
            api_key_index=self.config.api_key_index
        )
        if error or not token:
            raise ExchangeError("AUTH_ERROR")
        return token

    async def validate_limits(self) -> None:
        result = await self._read(
            self.accounts.account_limits,
            account_index=self.config.account_index,
            authorization=self.auth(),
        )
        label = (str(result["user_tier"]) + " " + str(result.get("user_tier_name", ""))).lower()
        tier = next((name for name in ("standard", "plus", "premium") if name in label), None)
        if tier is None:
            raise ExchangeError("CONFIG_ERROR: unknown account tier")
        if self.tier is not None and self.tier != tier:
            raise ExchangeError(
                "CONFIG_ERROR: account tier changed; restart after reviewing limits"
            )
        if self.tier is None:
            self.tier = tier
            self.weighted_reads = tier != "standard"
            cap = 60 if tier == "standard" else 24000
            tx_cap = 60 if tier == "standard" else 4000
            if self.config.reads_per_minute > cap or self.config.tx_per_minute > tx_cap:
                raise ExchangeError(
                    "CONFIG_ERROR: configured rate budgets exceed conservative current tier limits"
                )
            if self.weighted_reads:
                if self.config.reads_per_minute <= 1600:
                    raise ExchangeError(
                        "CONFIG_ERROR: weighted HTTP budget must exceed 1600 recovery units"
                    )
                self.read_budget.reserve = 1600
                observed = result.get("volume_quota_remaining", self.config.initial_volume_quota)
                if observed is None:
                    raise ExchangeError(
                        "CONFIG_ERROR: INITIAL_VOLUME_QUOTA is required for Plus/Premium when API does not expose it"
                    )
                self.volume_quota = int(observed)
            else:
                self.shared_budget = RateBudget(60, 8 + self.config.exit_reserve)
                self.shared_budget.used.extend(self.read_budget.used)
        if int(result["current_taker_fee_tick"]) != self.config.expected_taker_fee_tick:
            raise ExchangeError("CONFIG_ERROR: account taker fee changed; verify fee settings")

    async def snapshot(self) -> Snapshot:
        if self.market is None:
            raise ExchangeError("STATE_MISMATCH")
        result = await self._read(
            self.accounts.account,
            by="index",
            value=str(self.config.account_index),
            active_only=False,
        )
        rows = result["accounts"]
        if len(rows) != 1 or int(rows[0]["account_index"]) != self.config.account_index:
            raise ExchangeError("STATE_MISMATCH")
        account = rows[0]
        positions = [p for p in account["positions"] if int(p["market_id"]) == self.market.index]
        if len(positions) > 1:
            raise ExchangeError("STATE_MISMATCH")
        orders = await self._read(
            self.orders.account_active_orders,
            authorization=self.auth(),
            account_index=self.config.account_index,
            market_id=self.market.index,
        )
        row = positions[0] if positions else None
        return Snapshot(account, row, parse_position(row), orders["orders"], time.monotonic_ns())

    async def lookup(self, client_id: int) -> dict[str, Any] | None:
        result = await self._read(
            self.orders.account_orders,
            authorization=self.auth(),
            client_order_indexes=str(client_id),
            account_index=self.config.account_index,
        )
        matches = [r for r in result["orders"] if int(r["client_order_index"]) == client_id]
        if len(matches) > 1:
            raise ExchangeError("STATE_MISMATCH")
        return matches[0] if matches else None

    async def recent_fills(self) -> list[dict[str, Any]]:
        if self.market is None:
            raise ExchangeError("STATE_MISMATCH")
        result = await self._read(
            self.orders.trades,
            sort_by="timestamp",
            sort_dir="desc",
            limit=100,
            market_id=self.market.index,
            account_index=self.config.account_index,
            authorization=self.auth(),
        )
        return result["trades"]

    async def transaction(self, tx_hash: str) -> dict[str, Any]:
        return await self._read(self.transactions.tx, by="hash", value=tx_hash)

    async def refresh_nonce(self) -> None:
        result = await self._read(
            self.transactions.next_nonce,
            account_index=self.config.account_index,
            api_key_index=self.config.api_key_index,
        )
        self.nonce = int(result["nonce"])

    async def _send(
        self, tx_type: int, tx_info: str, timestamps: dict[str, int], prefix: str
    ) -> None:
        if self.shared_budget:
            await self.shared_budget.acquire(priority=prefix != "entry")
        timestamps[f"{prefix}_sent"] = time.monotonic_ns()
        try:
            # Deliberately call the official POST exactly once; a timeout is an uncertain outcome.
            if self.ws_sender is not None:
                response = await self.ws_sender(tx_type, tx_info)
            else:
                response = await self.transactions.send_tx(
                    tx_type=tx_type,
                    tx_info=tx_info,
                    _request_timeout=self.config.request_timeout_ms / 1000,
                )
            data = checked(response)
        except BaseException as error:
            if isinstance(error, lighter.ApiException) and error.status == 429:
                self.tx_budget.rejected(time.monotonic_ns())
            raise ExchangeError("ORDER_SUBMISSION_UNCERTAIN", uncertain=True) from None
        timestamps[f"{prefix}_ack"] = time.monotonic_ns()
        self.metrics.measure("order_ack", timestamps[f"{prefix}_sent"], timestamps[f"{prefix}_ack"])
        if self.weighted_reads and data.get("volume_quota_remaining") is not None:
            self.volume_quota = int(data["volume_quota_remaining"])

    async def order(
        self,
        client_id: int,
        quantity: int,
        price: int,
        *,
        sell: bool,
        reduce_only: bool,
        timestamps: dict[str, int],
    ) -> None:
        if self.signer is None or self.market is None or self.nonce is None or quantity <= 0:
            raise ExchangeError("STATE_MISMATCH")
        async with self.tx_lock:
            await self.tx_budget.acquire(priority=reduce_only)
            prefix = "exit" if reduce_only else "entry"
            tx_type, info, tx_hash, error = self.signer.sign_create_order(
                market_index=self.market.index,
                client_order_index=client_id,
                base_amount=quantity,
                price=price,
                is_ask=sell,
                order_type=self.signer.ORDER_TYPE_MARKET,
                time_in_force=self.signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=reduce_only,
                order_expiry=self.signer.DEFAULT_IOC_EXPIRY,
                nonce=self.nonce,
                api_key_index=self.config.api_key_index,
            )
            if error or info is None or tx_type is None:
                raise ExchangeError("ORDER_SIGNING_FAILED")
            timestamps[f"{prefix}_signed"] = time.monotonic_ns()
            if self.on_signed is not None:
                await self.on_signed(client_id, tx_hash, self.nonce)
            self.nonce += 1
            if self.weighted_reads and self.volume_quota is not None:
                self.volume_quota = max(0, self.volume_quota - 1)
            await self._send(int(tx_type), info, timestamps, prefix)

    async def cancel(self, order: dict[str, Any]) -> None:
        if self.signer is None or self.market is None or self.nonce is None:
            raise ExchangeError("STATE_MISMATCH")
        async with self.tx_lock:
            await self.tx_budget.acquire(priority=True)
            await self.cancel_budget.acquire(priority=True)
            tx_type, info, _, error = self.signer.sign_cancel_order(
                market_index=self.market.index,
                order_index=int(order["order_index"]),
                nonce=self.nonce,
                api_key_index=self.config.api_key_index,
            )
            if error or info is None or tx_type is None:
                raise ExchangeError("ORDER_SIGNING_FAILED")
            self.nonce += 1
            await self._send(int(tx_type), info, {}, "cancel")

    async def verify_leverage(self, snapshot: Snapshot, *, configure: bool) -> None:
        row = snapshot.position_row
        # The account JSON example uses fractional IMF. Explicit scale permits percentage
        # or native tick representations without inferring units from requested leverage.
        expected = self.config.account_imf_scale / self.config.leverage
        if (
            row is not None
            and D(str(row["initial_margin_fraction"])) == expected
            and int(row["margin_mode"]) == self.config.margin_mode
        ):
            return
        if (
            not configure
            or snapshot.position.size
            or snapshot.orders
            or int(snapshot.account["pending_order_count"])
        ):
            raise ExchangeError("CONFIG_ERROR: leverage cannot be verified safely")
        if self.signer is None or self.market is None or self.nonce is None:
            raise ExchangeError("STATE_MISMATCH")
        async with self.tx_lock:
            await self.tx_budget.acquire(priority=True)
            await self.leverage_budget.acquire(priority=True)
            tx_type, info, _, error = self.signer.sign_update_leverage(
                self.market.index,
                10000 // self.config.leverage,
                self.config.margin_mode,
                nonce=self.nonce,
                api_key_index=self.config.api_key_index,
            )
            if error or info is None or tx_type is None:
                raise ExchangeError("ORDER_SIGNING_FAILED")
            self.nonce += 1
            await self._send(int(tx_type), info, {}, "leverage")
        deadline = time.monotonic_ns() + self.config.order_timeout_ms * 1_000_000
        while time.monotonic_ns() < deadline:
            await asyncio.sleep(0.15)
            confirmed = await self.snapshot()
            row = confirmed.position_row
            if (
                row
                and D(str(row["initial_margin_fraction"])) == expected
                and int(row["margin_mode"]) == self.config.margin_mode
            ):
                return
        raise ExchangeError("CONFIG_ERROR: leverage change not confirmed")

    async def close(self) -> None:
        if self.signer:
            await self.signer.close()
        await self.api.close()
