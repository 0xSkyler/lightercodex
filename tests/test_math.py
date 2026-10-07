import pytest

from scalper.config import D
from scalper.metrics import TradeCycle
from scalper.orderbook import BookError, Market, OrderBook
from scalper.pnl import Position, estimate_close
from scalper.signals import SignalEngine


def make_book(bids, asks):
    b = OrderBook()
    b.update(
        {
            "bids": [{"price": p, "size": q} for p, q in bids],
            "asks": [{"price": p, "size": q} for p, q in asks],
            "offset": 1,
        },
        1_000_000_000,
        snapshot=True,
    )
    return b


def test_vwap_entire_depth_and_short_liquidity():
    b = make_book([("101", "1"), ("100", "1")], [("102", "1"), ("103", "1")])
    assert b.sweep(D(2), buy=False).vwap == D("100.5")
    assert b.sweep(D(2), buy=True).vwap == D("102.5")
    partial = b.sweep(D(3), buy=False)
    assert partial.filled == 2 and not partial.complete


@pytest.mark.parametrize("side,entry", [(1, "100"), (-1, "103")])
def test_close_both_sides_exact_fees(side, entry):
    b = make_book([("101", "1"), ("100", "1")], [("102", "1"), ("103", "1")])
    p = Position(D(side * 2), D(entry))
    result = estimate_close(p, b, taker_fee=D("0.001"), min_profit_usd=D("0.01"))
    assert result.gross_pnl == 1
    expected_fees = (D(entry) + result.close_vwap) * 2 * D("0.001")
    assert result.fees == expected_fees
    assert result.expected_net_pnl == 1 - expected_fees
    assert result.profitable


def test_green_requires_full_size_not_profitable_top_level():
    b = make_book([("101", "0.01"), ("99", "10")], [("102", "10")])
    result = estimate_close(Position(D(1), D(100)), b, taker_fee=D(0), min_profit_usd=D("0.01"))
    assert result.expected_net_pnl < 0 and not result.profitable


def test_fees_spread_and_buffer_can_erase_green():
    b = make_book([("100.05", "2")], [("100.06", "2")])
    result = estimate_close(
        Position(D(1), D(100)),
        b,
        taker_fee=D("0.001"),
        min_profit_usd=D("0.01"),
        safety_buffer_usd=D("0.01"),
    )
    assert result.gross_pnl > 0 and result.expected_net_pnl < 0 and not result.profitable


def test_threshold_is_strict_and_bps_threshold_applies():
    b = make_book([("100.01", "2")], [("100.02", "2")])
    p = Position(D(1), D(100))
    r = estimate_close(p, b, taker_fee=D(0), min_profit_usd=D("0.01"))
    assert not r.profitable
    r = estimate_close(p, b, taker_fee=D(0), min_profit_usd=D("0.001"), min_profit_bps=D(2))
    assert not r.profitable


def test_partial_liquidity_is_never_green_and_limit_applies():
    b = make_book([("101", "1"), ("100", "1")], [("102", "2")])
    r = estimate_close(
        Position(D(2), D(99)), b, taker_fee=D(0), min_profit_usd=D("0.01"), limit=D("100.5")
    )
    assert r.available_liquidity == 1 and not r.complete and not r.profitable


def test_book_updates_replace_sizes_remove_zero_and_reject_duplicates(book):
    assert book.update({"bids": [{"price": "100", "size": "2"}], "asks": [], "offset": 2}, 2)
    assert book.bids[D(100)] == 2
    assert not book.update({"bids": [{"price": "100", "size": "99"}], "asks": [], "offset": 2}, 3)
    assert book.bids[D(100)] == 2
    book.update(
        {
            "bids": [{"price": "100", "size": "0"}, {"price": "99", "size": "1"}],
            "asks": [],
            "offset": 3,
        },
        4,
    )
    assert book.bid == 99


def test_gap_crossed_book_and_unsynchronized_delta_fail_closed():
    b = OrderBook()
    with pytest.raises(BookError, match="before snapshot"):
        b.update({"offset": 1, "bids": [], "asks": []}, 0)
    b.update(
        {
            "bids": [{"price": "100", "size": "1"}],
            "asks": [{"price": "101", "size": "1"}],
            "offset": 1,
            "nonce": 5,
        },
        1,
        snapshot=True,
    )
    assert b.update({"bids": [], "asks": [], "offset": 10, "begin_nonce": 5, "nonce": 6}, 2)
    with pytest.raises(BookError, match="nonce gap"):
        b.update({"bids": [], "asks": [], "offset": 11, "begin_nonce": 7, "nonce": 8}, 3)
    assert not b.valid
    with pytest.raises(BookError, match="crossed"):
        b.update(
            {
                "bids": [{"price": "101", "size": "1"}],
                "asks": [{"price": "100", "size": "1"}],
                "offset": 1,
            },
            1,
            snapshot=True,
        )


def test_spread_and_freshness(book):
    assert book.spread_bps == D("0.01") / D("100.005") * 10000
    assert book.fresh(book.received_ns + 500_000_000, 500)
    assert not book.fresh(book.received_ns + 500_000_001, 500)
    book.valid = False
    assert not book.fresh(book.received_ns, 500)


def test_native_rounding_stays_inside_slippage_limit(market):
    assert market.size(D("0.123459")) == D("0.12345")
    assert market.price_units(D("100.019"), buy=True) == 10001
    assert market.price_units(D("100.019"), buy=False) == 10002
    with pytest.raises(BookError):
        market.price_units(D("1e30"), buy=True)


def test_market_discovery_never_assumes_btc_index():
    row = dict(
        symbol="BTC",
        market_type="perp",
        status="active",
        market_id=99,
        supported_size_decimals=5,
        supported_price_decimals=1,
        min_base_amount="0.0001",
        min_quote_amount="10",
        min_initial_margin_fraction=200,
        order_quote_limit="10000",
    )
    assert Market.discover([row]).index == 99
    with pytest.raises(BookError):
        Market.discover([row, row])


def test_signal_symmetry_warmup_and_bounded_history():
    results = []
    for direction in (1, -1):
        b = make_book(
            [("100", "9" if direction == 1 else "1")], [("100.01", "1" if direction == 1 else "9")]
        )
        engine = SignalEngine(1, (1, 1, 0, 0, 1, 0), 0.5)
        engine.quote(b, 1_000_000_000)
        assert engine.calculate(b, 1_000_000_000).direction == 0
        engine.quote(b, 2_100_000_000)
        engine.trade(1, 1, buyer_aggressive=direction == 1, now_ns=2_100_000_000)
        engine.trade(1, 100, buyer_aggressive=direction != 1, now_ns=2_100_000_000)
        result = engine.calculate(b, 2_100_000_000)
        assert result.direction == direction and len(engine.trades) == 1
        results.append(result.score)
        engine.quote(b, 13_000_000_000)
        assert not engine.trades
    assert results[0] == pytest.approx(-results[1])


def test_partial_fill_accounting_counts_only_confirmed_complete_exits():
    cycle = TradeCycle(1, 25)
    cycle.fill(
        entry=True, size=D("0.4"), price=D(100), fee_tick=1000, fee_scale=1000000, now_ns=100
    )
    cycle.fill(
        entry=True, size=D("0.6"), price=D(101), fee_tick=1000, fee_scale=1000000, now_ns=200
    )
    cycle.fill(entry=False, size=D(1), price=D(102), fee_tick=1000, fee_scale=1000000, now_ns=300)
    result = cycle.finish(400)
    assert result["average_entry"] == "100.6"
    assert D(result["gross_pnl"]) == D("1.4")
    assert D(result["fees"]) == D("0.2026")
    assert D(result["net_realized_pnl"]) == D("1.1974")
    assert result["accounting_complete"]
    cycle.fees_known = False
    assert cycle.finish(400)["net_realized_pnl"] is None
    cycle.fees_known = True
    cycle.recovered = True
    assert not cycle.finish(400)["accounting_complete"]
