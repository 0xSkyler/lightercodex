"""Explicit mainnet configuration; secrets are never included in representations."""

import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import dotenv_values

MAINNET = "https://mainnet.zklighter.elliot.ai"
WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
D = Decimal
BPS = D(10000)


class ConfigError(ValueError):
    """Invalid configuration; safe to print without exception chaining."""


@dataclass(frozen=True)
class Config:
    account_index: int
    api_key_index: int
    private_key: str = field(repr=False)
    leverage: int
    fee_tick_scale: int
    expected_taker_fee_tick: int
    tx_per_minute: int
    reads_per_minute: int
    initial_volume_quota: int | None = None
    account_imf_scale: Decimal = D(1)
    margin_usd: Decimal = D(10)
    notional_usd: Decimal = D(250)
    position_mode: str = "fixed_margin"
    min_profit_usd: Decimal = D("0.01")
    min_profit_bps: Decimal = D("0.1")
    safety_buffer_usd: Decimal = D("0.01")
    execution_buffer_bps: Decimal = D("0.2")
    entry_slippage_bps: Decimal = D(1)
    normal_exit_slippage_bps: Decimal = D(1)
    emergency_exit_slippage_bps: Decimal = D(20)
    max_loss_usd: Decimal = D(1)
    max_adverse_bps: Decimal = D(10)
    max_spread_bps: Decimal = D(1)
    max_volatility_bps: float = 5
    max_hold_ms: int = 5000
    market_stale_ms: int = 500
    account_stale_ms: int = 15000
    reconcile_ms: int = 10000
    order_timeout_ms: int = 3000
    request_timeout_ms: int = 2000
    exit_reserve: int = 4
    max_entries_per_minute: int = 20
    entry_score_threshold: float = 0.65
    weights: tuple[float, ...] = (1, 1, 1, 0.5, 0.5, 0.5)
    book_levels: int = 10
    max_book_levels: int = 5000
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    margin_mode: int = 0

    @property
    def taker_fee(self) -> Decimal:
        return D(self.expected_taker_fee_tick) / D(self.fee_tick_scale)

    @property
    def desired_notional(self) -> Decimal:
        if self.position_mode == "fixed_margin":
            return self.margin_usd * self.leverage
        return self.notional_usd


def read_environment(
    path: str | None = ".env", environ: Mapping[str, str] | None = None
) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    if path and Path(path).is_file():
        if stat.S_IMODE(Path(path).stat().st_mode) & 0o077:
            raise ConfigError("Environment file must have permissions 600 (chmod 600 .env)")
        values.update(dotenv_values(path, interpolate=False))
    values.update(os.environ if environ is None else environ)
    return values


def credentials(values: Mapping[str, str | None]) -> tuple[int, int, str]:
    names = ("LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY")
    if any(not values.get(name) for name in names):
        raise ConfigError(
            "LIGHTER_ACCOUNT_INDEX, LIGHTER_API_KEY_INDEX, and LIGHTER_API_PRIVATE_KEY are required"
        )
    try:
        account, key = int(values[names[0]] or ""), int(values[names[1]] or "")
    except ValueError:
        raise ConfigError("Account and API key indices must be integers") from None
    private = (values[names[2]] or "").strip()
    if not (0 <= account < 2**48 and 3 <= key <= 254) or not re.fullmatch(
        r"(?:0x)?[0-9a-fA-F]{80}", private
    ):
        raise ConfigError(
            "Invalid account index, API key index (3..254), or 40-byte Lighter API signing key"
        )
    return account, key, private


def load_config(
    path: str | None = ".env", *, live: bool = False, environ: Mapping[str, str] | None = None
) -> Config:
    values = read_environment(path, environ)
    if values.get("MARKET", "BTC") != "BTC" or values.get("LIGHTER_URL", MAINNET) != MAINNET:
        raise ConfigError("Only the official Lighter mainnet BTC perpetual endpoint is allowed")
    if live and (
        values.get("LIVE_TRADING") != "true"
        or values.get("I_UNDERSTAND_THIS_USES_REAL_FUNDS") != "YES"
    ):
        raise ConfigError(
            "Live execution requires LIVE_TRADING=true and I_UNDERSTAND_THIS_USES_REAL_FUNDS=YES"
        )

    def string(name: str, default: str | None = None) -> str:
        val = values.get(name, default)
        if val is None or not val.strip():
            raise ConfigError(f"{name} is required")
        return val.strip()

    def integer(name: str, default: str | None = None, minimum: int = 1) -> int:
        try:
            result = int(string(name, default))
        except ValueError:
            raise ConfigError(f"{name} must be an integer") from None
        if result < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return result

    def decimal(name: str, default: str, zero: bool = False) -> Decimal:
        try:
            val = D(string(name, default))
        except InvalidOperation:
            raise ConfigError(f"{name} must be a decimal") from None
        if not val.is_finite() or val < 0 or (val == 0 and not zero):
            raise ConfigError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
        return val

    private_key = string("LIGHTER_API_PRIVATE_KEY")
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{80}", private_key):
        raise ConfigError(
            "LIGHTER_API_PRIVATE_KEY must be the 40-byte Lighter API signing key, not an Ethereum wallet key"
        )
    config = Config(
        account_index=integer("LIGHTER_ACCOUNT_INDEX", minimum=0),
        api_key_index=integer("LIGHTER_API_KEY_INDEX", minimum=3),
        private_key=private_key,
        leverage=integer("LEVERAGE"),
        fee_tick_scale=integer("FEE_TICK_SCALE"),
        expected_taker_fee_tick=integer("EXPECTED_TAKER_FEE_TICK", minimum=0),
        tx_per_minute=integer("TX_PER_MINUTE"),
        reads_per_minute=integer("HTTP_READS_PER_MINUTE"),
        initial_volume_quota=(
            integer("INITIAL_VOLUME_QUOTA", minimum=0)
            if values.get("INITIAL_VOLUME_QUOTA")
            else None
        ),
        margin_usd=decimal("MARGIN_PER_TRADE_USD", "10"),
        account_imf_scale=decimal("ACCOUNT_IMF_SCALE", "1"),
        notional_usd=decimal("FIXED_NOTIONAL_USD", "250"),
        position_mode=string("POSITION_MODE", "fixed_margin"),
        min_profit_usd=decimal("MIN_PROFIT_USD", "0.01"),
        min_profit_bps=decimal("MIN_PROFIT_BPS", "0.1", True),
        safety_buffer_usd=decimal("SAFETY_BUFFER_USD", "0.01", True),
        execution_buffer_bps=decimal("EXECUTION_BUFFER_BPS", "0.2", True),
        entry_slippage_bps=decimal("MAX_ENTRY_SLIPPAGE_BPS", "1"),
        normal_exit_slippage_bps=decimal("MAX_NORMAL_EXIT_SLIPPAGE_BPS", "1"),
        emergency_exit_slippage_bps=decimal("MAX_EMERGENCY_EXIT_SLIPPAGE_BPS", "20"),
        max_loss_usd=decimal("MAX_LOSS_USD", "1"),
        max_adverse_bps=decimal("MAX_ADVERSE_MOVE_BPS", "10"),
        max_spread_bps=decimal("MAX_SPREAD_BPS", "1"),
        max_volatility_bps=float(decimal("MAX_VOLATILITY_BPS", "5")),
        max_hold_ms=integer("MAX_HOLD_MS", "5000"),
        market_stale_ms=integer("MARKET_DATA_STALE_MS", "500"),
        account_stale_ms=integer("ACCOUNT_STREAM_STALE_MS", "15000"),
        reconcile_ms=integer("RECONCILE_MS", "10000"),
        order_timeout_ms=integer("ORDER_TIMEOUT_MS", "3000"),
        request_timeout_ms=integer("REQUEST_TIMEOUT_MS", "2000"),
        exit_reserve=integer("EXIT_TX_RESERVE", "4"),
        max_entries_per_minute=integer("MAX_ENTRIES_PER_MINUTE", "20"),
        entry_score_threshold=float(decimal("ENTRY_SCORE_THRESHOLD", "0.65")),
        weights=tuple(
            float(decimal(n, v, True))
            for n, v in zip(
                (
                    "BOOK_IMBALANCE_WEIGHT",
                    "TRADE_FLOW_WEIGHT",
                    "MICRO_MOMENTUM_WEIGHT",
                    "BBO_MOMENTUM_WEIGHT",
                    "MICROPRICE_WEIGHT",
                    "VOLUME_ACCEL_WEIGHT",
                ),
                ("1", "1", "1", "0.5", "0.5", "0.5"),
                strict=True,
            )
        ),
        book_levels=integer("BOOK_LEVELS", "10"),
        max_book_levels=integer("MAX_BOOK_LEVELS", "5000"),
        data_dir=Path(string("DATA_DIR", "data")).resolve(),
        log_dir=Path(string("LOG_DIR", "logs")).resolve(),
        log_level=string("LOG_LEVEL", "INFO"),
        margin_mode=integer("MARGIN_MODE", "0", minimum=0),
    )
    if config.api_key_index > 254 or config.leverage > 10000 or 10000 % config.leverage:
        raise ConfigError(
            "API key index must be 3..254; leverage must be exactly representable as an integer IMF tick (10000 / leverage)"
        )
    if config.account_index >= 2**48 or not all(
        math.isfinite(v)
        for v in (*config.weights, config.max_volatility_bps, config.entry_score_threshold)
    ):
        raise ConfigError("Account index exceeds signer bounds or a signal parameter is not finite")
    if config.position_mode not in ("fixed_margin", "fixed_notional") or config.margin_mode not in (
        0,
        1,
    ):
        raise ConfigError("Invalid POSITION_MODE or MARGIN_MODE")
    if config.margin_mode == 1:
        raise ConfigError(
            "Version 1 requires cross margin (MARGIN_MODE=0); isolated collateral transfer is unsupported"
        )
    if config.tx_per_minute <= config.exit_reserve or config.reads_per_minute < 10:
        raise ConfigError(
            "Rate budgets must leave exit capacity and >=10 recovery reads per minute"
        )
    if not 0 < config.entry_score_threshold <= 1 or sum(config.weights) <= 0:
        raise ConfigError("Signal threshold must be in (0,1] and weights must have positive total")
    if config.taker_fee >= D("0.01"):
        raise ConfigError("Fee tick configuration implies >=1% taker fees; verify units")
    if config.fee_tick_scale != 1_000_000 or config.account_imf_scale not in (
        D(1),
        D(100),
        D(10000),
    ):
        raise ConfigError(
            "Official fee tick scale is 1000000; ACCOUNT_IMF_SCALE must be 1, 100, or 10000"
        )
    if config.emergency_exit_slippage_bps < config.normal_exit_slippage_bps:
        raise ConfigError("Emergency slippage must be >= normal exit slippage")
    if max(config.entry_slippage_bps, config.emergency_exit_slippage_bps) >= BPS:
        raise ConfigError("Slippage must be less than 10000 bps")
    if (
        config.reconcile_ms >= config.account_stale_ms
        or config.book_levels > config.max_book_levels
    ):
        raise ConfigError("Reconciliation must precede account staleness; book depth exceeds bound")
    if config.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("Invalid LOG_LEVEL")
    return config
