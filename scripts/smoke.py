"""Fail loudly if any Polymarket endpoint is blocked from this machine (used on GitHub runners)."""

import json
import sys
import urllib.error
import urllib.request

UA = "whalescan/0.1 (+https://github.com/whalescan)"
URLS = [
    "https://data-api.polymarket.com/trades?limit=1",
    "https://data-api.polymarket.com/closed-positions?user=0x5268527977f700f9bf9b6d5cd843859e4e70135d&limit=1&sortBy=TIMESTAMP",
    "https://data-api.polymarket.com/v1/leaderboard?limit=1",
    "https://gamma-api.polymarket.com/markets?limit=1",
    "https://clob.polymarket.com/markets?limit=1",
]


def main() -> int:
    failed = 0
    for url in URLS:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.load(r)
                print(f"OK      {r.status} {url}")
        except urllib.error.HTTPError as e:
            failed += 1
            print(f"BLOCKED {e.code} {e.headers.get('server')} {url}")
        except Exception as e:  # noqa: BLE001 — report anything else as a failure too
            failed += 1
            print(f"ERROR   {e!r} {url}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
