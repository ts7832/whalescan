"""The live station's brain (Plan 2, spec §4.8): pure state, no network.

Trades and book updates go in; dashboard messages come out:
  {"type": "contact" | "signal" | "signal_update", "data": <evaluation_json + "book">}
The same gate as snapshot mode decides everything; this class adds time: merging fills as they arrive,
re-evaluating when books move or related trades land, STALE when the live price runs past a signal's
max entry, RESYNC when its book is temporarily unknown, and expiry.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

from whalescan.classify import Blocklist, category_for_tags
from whalescan.config import Config
from whalescan.gate import Evaluation, GateContext, PositionEvent, ScoreBook, _merge, evaluate
from whalescan.insider import account_age_days, evaluate_insider, is_insider_candidate
from whalescan.models import Market, Trade, WalletProfile
from whalescan.snapshot import evaluation_json
from whalescan.stream.books import LiveBooks

Message = dict[str, Any]
OPEN = ("SIGNAL", "INSIDER")  # statuses shown on the signal board


class LiveState:
    def __init__(self, cfg: Config, *, scores: pd.DataFrame, markets: Mapping[str, Market] | None = None,
                 names: Mapping[str, str] | None = None, books: LiveBooks | None = None, max_watch: int = 200) -> None:
        self.cfg = cfg
        self.g = cfg.gate
        self.blocklist = Blocklist(cfg.blocklist)
        self.markets: dict[str, Market] = dict(markets or {})
        self.names: dict[str, str] = dict(names or {})
        self.books = books or LiveBooks()
        self.max_watch = max_watch
        self._set_scores(scores)
        self._open_groups: dict[tuple[str, str, str], list[Trade]] = {}
        self._seen: dict[tuple[str, str, str, str, float, float], int] = {}  # fill -> trade ts (evicted after the lookback)
        self.events: dict[str, PositionEvent] = {}
        self._by_asset: dict[str, set[str]] = defaultdict(set)
        self._by_cid: dict[str, set[str]] = defaultdict(set)
        self._emitted: dict[str, tuple[Any, ...]] = {}
        self._book_sig: dict[str, tuple[Any, ...] | None] = {}
        self.signals: dict[str, Message] = {}
        self.contacts: dict[str, Message] = {}
        self.profiles: dict[str, WalletProfile] = {}

    # ---------------------------------------------------------------- inputs

    def on_trade(self, t: Trade, now: int) -> list[Message]:
        fill = (t.tx_hash, t.wallet, t.asset, t.side, t.price, t.size)  # a sweep = several fills, one tx
        if fill in self._seen:
            return []
        self._seen[fill] = t.ts
        key = (t.wallet, t.asset, t.side)
        group = self._open_groups.get(key)
        if group and t.ts - group[0].ts <= self.g.aggregation_window_s:
            group.append(t)
        else:
            group = [t]
            self._open_groups[key] = group
        ev = _merge(group)
        if ev.usdc < self.g.min_usdc:  # below the gate floor: never a contact or a signal
            return []
        self.events[ev.id] = ev
        self._by_asset[ev.asset].add(ev.id)
        self._by_cid[ev.condition_id].add(ev.id)
        # A new position can change its neighbours' verdicts (G7 conflict, consensus tier): re-check the market.
        return self._reevaluate(self._ids(self._by_cid[ev.condition_id]), now)

    def on_book(self, asset: str, now: int) -> list[Message]:
        ids = self._by_asset.get(asset)
        if not ids:
            return []
        q = self.books.quote(asset, self.g.follow_size_usdc)
        sig = None if q is None else (q.best_bid, q.best_ask, q.vwap, q.complete)
        if asset in self._book_sig and self._book_sig[asset] == sig:
            return []  # nothing a decision depends on moved
        self._book_sig[asset] = sig
        return self._reevaluate(self._ids(ids), now)

    def set_markets(self, markets: Mapping[str, Market], now: int) -> list[Message]:
        self.markets.update(markets)
        return self._reevaluate(self._ids(i for c in markets for i in self._by_cid.get(c, ())), now)

    def set_profiles(self, profiles: Mapping[str, WalletProfile], now: int) -> list[Message]:
        self.profiles.update(profiles)
        return self._reevaluate([e for e in self.events.values() if e.wallet in profiles], now)

    def set_scores(self, scores: pd.DataFrame, now: int) -> list[Message]:
        self._set_scores(scores)
        return self._reevaluate(list(self.events.values()), now)

    def expire(self, now: int) -> list[Message]:
        horizon = now - int(self.g.signal_lookback_h * 3600)
        msgs: list[Message] = []
        for eid, ev in list(self.events.items()):
            if ev.last_ts >= horizon:
                continue
            del self.events[eid]
            self._by_asset[ev.asset].discard(eid)
            self._by_cid[ev.condition_id].discard(eid)
            self._emitted.pop(eid, None)
            self.contacts.pop(eid, None)
            if eid in self.signals:
                data = dict(self.signals.pop(eid), status="EXPIRED")
                msgs.append({"type": "signal_update", "data": data})
        window_start = now - self.g.aggregation_window_s
        self._open_groups = {k: g for k, g in self._open_groups.items() if g[0].ts >= window_start}
        self._seen = {k: ts for k, ts in self._seen.items() if ts >= horizon}
        return msgs

    # ---------------------------------------------------------------- queries

    @property
    def certified_wallets(self) -> set[str]:
        return self._certified

    def watch_set(self, now: int) -> list[str]:
        """Books that matter, most important first: open signals, then recent BUYs by certified wallets
        in markets still trading. Signal assets are never evicted by the cap in favour of newer events."""
        horizon = now - int(self.g.signal_lookback_h * 3600)

        def latest(asset: str) -> int:
            return max((self.events[i].last_ts for i in self._by_asset.get(asset, ())), default=0)

        signal_assets = sorted({self.events[i].asset for i in self.signals if i in self.events}, key=lambda a: -latest(a))
        def fresh_or_unknown(e: PositionEvent) -> bool:
            age = account_age_days(e, self.profiles.get(e.wallet))
            return e.wallet not in self.profiles or (age is not None and age <= self.cfg.insider.max_age_days)

        recent = sorted((e for e in self.events.values()
                         if e.side == "BUY" and e.last_ts >= horizon and not self._closed(e)
                         and (self.scores.certified(e.wallet, self._category(e))
                              or (self._insider_candidate(e) and fresh_or_unknown(e)))),
                        key=lambda e: -e.last_ts)
        out: list[str] = []
        for asset in signal_assets + [e.asset for e in recent]:
            if asset not in out:
                out.append(asset)
        return out[:self.max_watch]

    def insider_count(self) -> int:
        return sum(1 for s in self.signals.values() if s.get("kind") == "INSIDER")

    def signal_assets(self) -> set[str]:
        return {self.events[i].asset for i in self.signals if i in self.events}

    def _insider_candidate(self, ev: PositionEvent) -> bool:
        return (ev.wallet not in self._certified and ev.condition_id in self.markets  # category must be known
                and is_insider_candidate(ev, self._category(ev), self.cfg.insider) and not self._closed(ev))

    def missing_profiles(self, now: int | None = None) -> set[str]:
        """Candidate wallets whose profile is missing or expired (unknown ages are retried sooner)."""
        ic = self.cfg.insider
        out = set()
        for w in {e.wallet for e in self.events.values() if self._insider_candidate(e)}:
            p = self.profiles.get(w)
            if p is None:
                out.add(w)
            elif now is not None:
                ttl = ic.profile_ttl_h if p.created_ts is not None else ic.unknown_profile_ttl_h
                if now - p.fetched_at > ttl * 3600:
                    out.add(w)
        return out

    def missing_markets(self) -> set[str]:
        return {e.condition_id for e in self.events.values()} - self.markets.keys()

    def state(self) -> dict[str, list[Message]]:
        contacts = sorted(self.contacts.values(), key=lambda c: -c["last_ts"])[:self.g.max_contacts]
        signals = sorted(self.signals.values(), key=lambda s: (s.get("kind") != "INSIDER", s["tier"] or "Z",
                                                               -(s["net_edge"] or 0.0), -s["usdc"]))
        return {"signals": signals, "contacts": contacts}

    # ---------------------------------------------------------------- internals

    def _set_scores(self, scores: pd.DataFrame) -> None:
        self.scores = ScoreBook.for_config(scores, self.cfg)
        self._certified = self.scores.certified_wallets()

    def _ids(self, ids: Iterable[str]) -> list[PositionEvent]:
        return [self.events[i] for i in list(ids) if i in self.events]

    def _closed(self, ev: PositionEvent) -> bool:
        m = self.markets.get(ev.condition_id)
        return m is not None and m.closed

    def _category(self, ev: PositionEvent) -> str:
        m = self.markets.get(ev.condition_id)
        return category_for_tags(m.tags, self.cfg.categories) if m else "OTHER"

    def _evaluate(self, ev: PositionEvent, now: int) -> Evaluation:
        # G7 and consensus only ever look at the same market, so the market's own events suffice.
        ctx = GateContext(cfg=self.g, categories=self.cfg.categories, blocklist=self.blocklist, scores=self.scores,
                          markets=self.markets, events=self._ids(self._by_cid[ev.condition_id]), now=now)
        quote = self.books.quote(ev.asset, self.g.follow_size_usdc)
        e = evaluate(ev, ctx, quote)
        if e.status != "SIGNAL" and self._insider_candidate(ev) and ev.wallet in self.profiles:
            # Not a proven sniper, but maybe an insider: judge it by the account instead of its track record.
            return evaluate_insider(ev, self.profiles[ev.wallet], self.markets.get(ev.condition_id), e.category,
                                    self.cfg.insider, self.blocklist, quote, now)
        return e

    def _reevaluate(self, events: list[PositionEvent], now: int) -> list[Message]:
        msgs: list[Message] = []
        for ev in events:
            e = self._evaluate(ev, now)
            data = dict(evaluation_json(e, self.markets.get(ev.condition_id), self.names, None), book="OK")
            is_open = ev.id in self.signals
            if e.status in OPEN:
                kind = "signal_update" if is_open else "signal"
                self.signals[ev.id] = data
                self.contacts.pop(ev.id, None)
            elif is_open:
                kind = "signal_update"
                failed = e.failed()
                only_g6 = e.status == "REJECTED" and len(failed) == 1 and failed[0].code in ("G6", "I6")
                if only_g6 and failed[0].detail.startswith(("FOLLOW", "PRICE MOVED")):
                    data = dict(data, status="STALE")  # still valid, but no longer worth the entry price
                    self.signals[ev.id] = data
                elif only_g6:
                    # NO BOOK / BOOK TOO THIN: we don't currently know the price; keep the verdict, flag the book
                    data = dict(self.signals[ev.id], book="RESYNC")
                    self.signals[ev.id] = data
                else:
                    del self.signals[ev.id]  # it failed a real gate: it goes back to the contact log
                    self.contacts[ev.id] = data
            else:
                kind = "contact"
                self.contacts[ev.id] = data
            fingerprint = (data["status"], round(data["usdc"], 2), data["tier"], round(data["net_edge"] or 0.0, 3),
                           round(data["max_entry"] or 0.0, 3), data["book"])
            if self._emitted.get(ev.id) != fingerprint:
                self._emitted[ev.id] = fingerprint
                msgs.append({"type": kind, "data": data})
        return msgs
