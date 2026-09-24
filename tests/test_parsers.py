import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whalescan.models import Market, resolved_winner
from whalescan.parsers import (
    ParseError,
    iso_to_ts,
    parse_book,
    parse_closed_position,
    parse_leaderboard,
    parse_many,
    parse_market,
    parse_open_position,
    parse_price_history,
    parse_trade,
)

REAL = Path(__file__).parent / "fixtures" / "real"

TRADE = {
    "proxyWallet": "0x23C8A4C266D10BA5846837EAC391FEA89ED6F293", "side": "BUY",
    "asset": "267958", "conditionId": "0x3B49", "size": 23340.21, "price": 0.5514416194,
    "timestamp": 1790249874, "title": "Dota 2: NAVI vs LGD", "slug": "dota2-navi-lgd",
    "eventSlug": "dota2-navi-lgd-2026-09-24", "outcome": "Natus Vincere", "outcomeIndex": 0,
    "transactionHash": "0x4435",
}

MARKET = {
    "conditionId": "0x8B7F", "question": "Spread: KC (-5.5)", "slug": "nfl-ind-kc-spread",
    "endDate": "2026-09-21T00:00:00Z", "closed": True, "closedTime": "2026-09-21 04:30:03+00",
    "outcomePrices": "[\"0\", \"1\"]", "clobTokenIds": "[\"111\", \"222\"]", "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    "volumeNum": 1685012.1, "events": [{"slug": "nfl-ind-kc-2026-09-21"}],
    "tags": [{"label": "Sports"}, {"label": "NFL"}],
}


def test_trade_normalises_wallet_and_condition():
    t = parse_trade(TRADE)
    assert t.wallet == "0x23c8a4c266d10ba5846837eac391fea89ed6f293"
    assert t.condition_id == "0x3b49"
    assert t.usdc == pytest.approx(23340.21 * 0.5514416194)
    assert t.fee is None


def test_trade_missing_field_raises_with_payload():
    bad = dict(TRADE)
    del bad["price"]
    with pytest.raises(ParseError) as exc:
        parse_trade(bad)
    assert exc.value.payload is bad


def test_trade_rejects_unknown_side():
    with pytest.raises(ParseError):
        parse_trade(dict(TRADE, side="HOLD"))


def test_market_fields_and_resolution():
    m = parse_market(MARKET)
    assert m.condition_id == "0x8b7f"
    assert m.event_slug == "nfl-ind-kc-2026-09-21"
    assert m.token_ids == ("111", "222")
    assert m.tags == ("Sports", "NFL")
    assert m.winner_index() == 1
    assert m.closed_ts == int(datetime(2026, 9, 21, 4, 30, 3, tzinfo=UTC).timestamp())
    assert m.end_ts == int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())


def test_fee_matches_live_fill():
    m = parse_market(MARKET)
    # verified live fill: BUY 2048 shares @ 0.63, rate 0.05, exponent 1 -> fee 23.86944 USDC
    assert 2048 * m.fee_per_share(0.63) == pytest.approx(23.86944, abs=1e-5)
    assert parse_market(dict(MARKET, feesEnabled=False)).fee_per_share(0.63) == 0.0


@pytest.mark.parametrize(
    ("closed", "prices", "expected"),
    [
        (True, [0.0, 1.0], 1),
        (True, [1.0, 0.0], 0),
        (True, [0.5, 0.5], None),        # fractional resolution is discarded
        (False, [0.0005, 0.9995], None),  # still trading
        (True, [], None),
    ],
)
def test_resolved_winner(closed, prices, expected):
    assert resolved_winner(closed, prices) == expected


def test_iso_formats():
    assert iso_to_ts(None) is None
    assert iso_to_ts("") is None
    assert iso_to_ts("2026-09-21") == int(datetime(2026, 9, 21, tzinfo=UTC).timestamp())


def test_closed_position_book_leaderboard_history():
    cp = parse_closed_position({
        "proxyWallet": "0xAB", "asset": "1", "conditionId": "0xC", "avgPrice": 0.49, "totalBought": 100.0,
        "realizedPnl": 51.0, "curPrice": 1, "outcome": "IND", "outcomeIndex": 1, "title": "t",
        "eventSlug": "e", "timestamp": 1789965015,
    })
    assert cp.wallet == "0xab" and cp.outcome_index == 1
    book = parse_book({"asset_id": "9", "timestamp": "1790250466788", "bids": [{"price": "0.01", "size": "155.15"}], "asks": []})
    assert book.ts_ms == 1790250466788 and book.bids == ((0.01, 155.15),)
    lb = parse_leaderboard({"rank": "1", "proxyWallet": "0xAA", "userName": "x", "vol": 10.0, "pnl": 2.0})
    assert lb.rank == 1 and lb.wallet == "0xaa"
    hist = parse_price_history({"history": [{"t": 1, "p": 0.5}]})
    assert hist[0].price == 0.5


def test_parse_many_counts_skips():
    items, skipped = parse_many([TRADE, {"junk": 1}], parse_trade)
    assert len(items) == 1 and skipped == 1


@pytest.mark.parametrize(
    ("name", "fn"),
    [
        ("trades_large", parse_trade),
        ("closed_positions", parse_closed_position),
        ("leaderboard", parse_leaderboard),
        ("markets_closed", parse_market),
        ("markets_open", parse_market),
    ],
)
def test_every_real_row_parses(name, fn):
    rows = json.loads((REAL / f"{name}.json").read_text())
    assert rows, f"{name}.json is empty"
    for row in rows:
        fn(row)


def test_real_book_and_history_parse():
    assert parse_book(json.loads((REAL / "book.json").read_text())).asset
    assert parse_price_history(json.loads((REAL / "prices_history.json").read_text()))


def test_real_closed_markets_resolve_to_a_winner():
    rows = json.loads((REAL / "markets_closed.json").read_text())
    winners = [parse_market(r).winner_index() for r in rows]
    assert any(w is not None for w in winners)


def test_market_dataclass_is_hashable():
    assert hash(parse_market(MARKET)) == hash(parse_market(MARKET))
    assert isinstance(parse_market(MARKET), Market)


def test_unredeemed_open_position_parses_as_resolved_history_row():
    row = {"proxyWallet": "0xD38B", "asset": "9", "conditionId": "0xAB", "avgPrice": 0.55, "totalBought": 410900.73,
           "realizedPnl": 0, "curPrice": 0, "outcome": "Packers", "outcomeIndex": 0, "title": "Packers vs. Bears",
           "eventSlug": "nfl-gb-chi", "redeemable": True, "size": 410900.73}
    p = parse_open_position(row)
    assert (p.wallet, p.condition_id, p.cur_price, p.total_bought) == ("0xd38b", "0xab", 0.0, 410900.73)
    assert p.ts == 0  # no close time exists; 0 keeps it out of incremental max(ts)
