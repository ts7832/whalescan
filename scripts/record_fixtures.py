"""Record small real API payloads into tests/fixtures/real/ for parser tests. Run manually; results are committed."""

import json
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real"
UA = {"User-Agent": "whalescan/0.1 (+https://github.com/whalescan)"}
WALLET = "0x5268527977f700f9bf9b6d5cd843859e4e70135d"


def get(client: httpx.Client, url: str, params: list[tuple[str, str]]) -> object:
    r = client.get(url, params=params)
    r.raise_for_status()
    return r.json()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=UA, timeout=30) as c:
        trades = get(c, "https://data-api.polymarket.com/trades",
                     [("limit", "20"), ("takerOnly", "false"), ("filterType", "CASH"), ("filterAmount", "5000")])
        closed = get(c, "https://data-api.polymarket.com/closed-positions",
                     [("user", WALLET), ("limit", "50"), ("sortBy", "TIMESTAMP"), ("sortDirection", "DESC")])
        board = get(c, "https://data-api.polymarket.com/v1/leaderboard",
                    [("timePeriod", "ALL"), ("orderBy", "PNL"), ("limit", "20")])
        cids = sorted({row["conditionId"] for row in closed})[:20]
        m_closed = get(c, "https://gamma-api.polymarket.com/markets",
                       [("condition_ids", x) for x in cids] + [("closed", "true"), ("include_tag", "true"), ("limit", "100")])
        m_open = get(c, "https://gamma-api.polymarket.com/markets",
                     [("closed", "false"), ("include_tag", "true"), ("limit", "10"), ("order", "volume24hr"), ("ascending", "false")])
        token = json.loads(m_open[0]["clobTokenIds"])[0]
        book = get(c, "https://clob.polymarket.com/book", [("token_id", token)])
        hist = get(c, "https://clob.polymarket.com/prices-history", [("market", token), ("interval", "1w"), ("fidelity", "60")])
    for name, data in {
        "trades_large": trades, "closed_positions": closed, "leaderboard": board,
        "markets_closed": m_closed, "markets_open": m_open, "book": book, "prices_history": hist,
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1))
        print(f"wrote {name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
