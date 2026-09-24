import { describe, expect, it } from 'vitest';
import { fmtAge, fmtCents, fmtP, fmtPrice, fmtUsd, shortWallet, signalsEmptyMessage } from './format';
import type { Meta } from './types';

const meta = (certified: number): Meta => ({
  generated_at: 0, version: '0.1.0', mode: 'SNAPSHOT',
  counts: { wallets_scanned: 10, wallets_complete: 10, tests: 20, testable: 5, certified_wallets: certified, signals: 0, contacts: 0 },
  params: { bh_q: 0.1, min_usdc: 5000, conviction_k: 2, follow_size_usdc: 1000, min_net_edge: 0.02, signal_lookback_h: 24 },
  errors: { api: 0 }, validation_generated_at: null,
});

describe('format', () => {
  it('formats money, prices and edges', () => {
    expect(fmtUsd(20_000)).toBe('$20.0K');
    expect(fmtUsd(2_500_000)).toBe('$2.50M');
    expect(fmtUsd(null)).toBe('—');
    expect(fmtPrice(0.4)).toBe('0.400');
    expect(fmtCents(0.042)).toBe('+4.2¢');
    expect(fmtCents(-0.01)).toBe('−1.0¢');
    expect(fmtP(0.00001)).toBe('1.0E-5');
    expect(fmtP(0.034)).toBe('0.034');
  });

  it('formats ages like a HUD', () => {
    expect(fmtAge(42)).toBe('42S');
    expect(fmtAge(125)).toBe('2M');
    expect(fmtAge(3 * 3600 + 12 * 60)).toBe('3H12M');
    expect(fmtAge(5 * 86400)).toBe('5D');
  });

  it('shortens wallets', () => {
    expect(shortWallet('0x5268527977f700f9bf9b6d5cd843859e4e70135d')).toBe('0x5268…135d');
  });

  it('explains an empty signal board', () => {
    expect(signalsEmptyMessage(meta(0))).toMatch(/NO CERTIFIED WHALES YET/);
    expect(signalsEmptyMessage(meta(3))).toBe('NO SIGNALS · 3 CERTIFIED WHALES UNDER WATCH');
    expect(signalsEmptyMessage(meta(1))).toBe('NO SIGNALS · 1 CERTIFIED WHALE UNDER WATCH');
  });
});


import { fmtDate, isSnapshotStale } from './format';

describe('dates and staleness', () => {
  it('never renders a missing date as 1970', () => {
    expect(fmtDate(null)).toBe('—');
    expect(fmtDate(0)).toBe('—');
    expect(fmtDate(86400)).toBe('1970-01-02');
  });

  it('flags a snapshot older than 8 hours as stale', () => {
    const m = meta(1);
    expect(isSnapshotStale({ ...m, generated_at: 0 }, 8 * 3600)).toBe(false);
    expect(isSnapshotStale({ ...m, generated_at: 0 }, 8 * 3600 + 1)).toBe(true);
  });
});
