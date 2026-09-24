import type { Meta } from './types';

type Num = number | null | undefined;

export const fmtUsd = (n: Num): string =>
  n == null ? '—' : n >= 1e6 ? `$${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `$${(n / 1e3).toFixed(1)}K` : `$${n.toFixed(0)}`;

export const fmtPrice = (p: Num): string => (p == null ? '—' : p.toFixed(3));

export const fmtCents = (e: Num): string =>
  e == null ? '—' : `${e >= 0 ? '+' : '−'}${Math.abs(e * 100).toFixed(1)}¢`;

export const fmtP = (p: Num): string =>
  p == null ? '—' : p < 1e-3 ? p.toExponential(1).toUpperCase() : p.toFixed(3);

export const fmtPct = (x: Num): string => (x == null ? '—' : `${(x * 100).toFixed(0)}%`);

export function fmtAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}S`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}M`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}H${String(m % 60).padStart(2, '0')}M`;
  return `${Math.floor(h / 24)}D`;
}

export const fmtUtc = (ts: number): string => `${new Date(ts * 1000).toISOString().slice(11, 16)}Z`;

export const fmtDate = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 10);

export const shortWallet = (w: string): string => `${w.slice(0, 6)}…${w.slice(-4)}`;

export function signalsEmptyMessage(meta: Meta): string {
  const c = meta.counts.certified_wallets;
  if (c === 0) return 'NO CERTIFIED WHALES YET — SCORING NEEDS MORE RESOLVED HISTORY';
  return `NO SIGNALS · ${c} CERTIFIED WHALE${c === 1 ? '' : 'S'} UNDER WATCH`;
}
