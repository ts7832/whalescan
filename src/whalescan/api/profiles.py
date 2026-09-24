"""Wallet profiles: account age and breadth, the raw material of the insider detector."""

from __future__ import annotations

from typing import Any

from whalescan.models import WalletProfile


async def fetch_profile(gamma: Any, data: Any, wallet: str, *, now: int) -> WalletProfile:
    w = wallet.lower()
    return WalletProfile(w, await gamma.created_ts(w), await data.markets_traded(w), now)
