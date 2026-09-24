"""The live station's brain (Plan 2, spec §4.8): pure state, no network.

Trades and book updates go in; dashboard messages come out:
  {"type": "contact" | "signal" | "signal_update", "data": <evaluation_json>}
The same gate as snapshot mode decides everything; this class adds time: merging fills as they arrive,
re-evaluating when books move, STALE when the live price runs past a signal's max entry, and expiry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from whalescan.classify import Blocklist
from whalescan.config import Config
from whalescan.gate import Evaluation, GateContext, PositionEvent, ScoreBook, _merge, evaluate
from whalescan.models import Market, Trade
from whalescan.snapshot import evaluation_json
from whalescan.stream.books import LiveBooks

Message = dict[str, Any]


class LiveState:
    def __init__(self, cfg: Config, *, scores: pd.DataFrame, markets: Mapping[str, Market] | None = None,
                 names: Mapping[str, str] | None = None, books: LiveBooks | None = None, max_watch: int = 200) -> None:
        self.cfg = cfg
        self.g = cfg.gate
        self.blocklist = Blocklist(cfg.blocklist)
        self.scores = ScoreBook(scores, self.g.fallback_max_cat_positions)
        self.markets: dict[str, Market] = dict(markets or {})
        self.names: dict[str, str] = dict(names or {})
        self.books = books or LiveBooks()
        self.max_watch = max_watch
        self._open_groups: dict[tuple[str, str, str], list[Trade]] = {}
        self._seen: set[tuple[str, str, str, str]] = set()
        self.events: dict[str, PositionEvent] = {}
        self._emitted: dict[str, tuple[str, float]] = {}  # id -> (status, usdc) last sent
        self.signals: dict[str, Message] = {}
        self.contacts: dict[str, Message] = {}

    # ---------------------------------------------------------------- inputs

    def on_trade(self, t: Trade, now: int) -> list[Message]:
        fill = (t.tx_hash, t.wallet, t.asset, t.side)
        if fill in self._seen:
            return []
        self._seen.add(fill)
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
        return self._reevaluate([ev], now)

    def on_book(self, asset: str, now: int) -> list[Message]:
        return self._reevaluate([e for e in self.events.values() if e.asset == asset], now)

    def set_markets(self, markets: Mapping[str, Market], now: int) -> list[Message]:
        self.markets.update(markets)
        return self._reevaluate([e for e in self.events.values() if e.condition_id in markets], now)

    def set_scores(self, scores: pd.DataFrame, now: int) -> list[Message]:
        self.scores = ScoreBook(scores, self.g.fallback_max_cat_positions)
        return self._reevaluate(list(self.events.values()), now)

    def expire(self, now: int) -> list[Message]:
        horizon = now - int(self.g.signal_lookback_h * 3600)
        msgs: list[Message] = []
        for eid, ev in list(self.events.items()):
            if ev.last_ts >= horizon:
                continue
            del self.events[eid]
            self._emitted.pop(eid, None)
            self.contacts.pop(eid, None)
            if eid in self.signals:
                data = dict(self.signals.pop(eid), status="EXPIRED")
                msgs.append({"type": "signal_update", "data": data})
        window_start = now - self.g.aggregation_window_s
        self._open_groups = {k: g for k, g in self._open_groups.items() if g[0].ts >= window_start}
        if len(self._seen) > 500_000:
            self._seen.clear()  # bounded memory; a duplicate after this is merely re-merged once
        return msgs

    # ---------------------------------------------------------------- queries

    def watch_set(self, now: int) -> list[str]:
        """Assets whose books matter: recent BUYs by certified wallets, plus open signals. Most recent first."""
        horizon = now - int(self.g.signal_lookback_h * 3600)
        recent = sorted((e for e in self.events.values()
                         if e.side == "BUY" and e.last_ts >= horizon
                         and self.scores.certified(e.wallet, self._category(e))),
                        key=lambda e: -e.last_ts)
        out: list[str] = []
        for asset in [self.events[i].asset for i in self.signals if i in self.events] + [e.asset for e in recent]:
            if asset not in out:
                out.append(asset)
        return sorted(out, key=lambda a: -max(e.last_ts for e in self.events.values() if e.asset == a))[:self.max_watch]

    def missing_markets(self) -> set[str]:
        return {e.condition_id for e in self.events.values()} - self.markets.keys()

    def state(self) -> dict[str, list[Message]]:
        contacts = sorted(self.contacts.values(), key=lambda c: -c["last_ts"])[:self.g.max_contacts]
        signals = sorted(self.signals.values(), key=lambda s: (s["tier"] or "Z", -(s["net_edge"] or 0.0)))
        return {"signals": signals, "contacts": contacts}

    # ---------------------------------------------------------------- internals

    def _category(self, ev: PositionEvent) -> str:
        return GateContext(self.g, self.cfg.categories, self.blocklist, self.scores, self.markets, (), None).category(
            ev.condition_id)

    def _evaluate(self, ev: PositionEvent, now: int) -> Evaluation:
        ctx = GateContext(cfg=self.g, categories=self.cfg.categories, blocklist=self.blocklist, scores=self.scores,
                          markets=self.markets, events=list(self.events.values()), now=now)
        return evaluate(ev, ctx, self.books.quote(ev.asset, self.g.follow_size_usdc))

    def _reevaluate(self, events: list[PositionEvent], now: int) -> list[Message]:
        msgs: list[Message] = []
        for ev in events:
            e = self._evaluate(ev, now)
            data = evaluation_json(e, self.markets.get(ev.condition_id), self.names, None)
            is_open = ev.id in self.signals
            if e.status == "SIGNAL":
                kind = "signal_update" if is_open else "signal"
                self.signals[ev.id] = data
                self.contacts.pop(ev.id, None)
            elif is_open:
                failed = e.failed()
                if e.status == "REJECTED" and len(failed) == 1 and failed[0].code == "G6":
                    data = dict(data, status="STALE")  # still valid, but no longer worth the entry price
                    self.signals[ev.id] = data
                else:
                    del self.signals[ev.id]
                kind = "signal_update"
            else:
                kind = "contact"
                self.contacts[ev.id] = data
            fingerprint = (data["status"], round(data["usdc"], 2))
            if self._emitted.get(ev.id) != fingerprint:
                self._emitted[ev.id] = fingerprint
                msgs.append({"type": kind, "data": data})
        return msgs
