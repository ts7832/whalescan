import { describe, expect, it } from 'vitest';
import { applyMessage, tryLive } from './live';
import type { Signal, Snapshot } from './types';

const sig = (id: string, status: Signal['status'], extra: Partial<Signal> = {}): Signal => ({
  id, status, tier: status === 'SIGNAL' ? 'A' : null, category: 'POLITICS', wallet: '0xw', wallet_name: '', asset: 'yes',
  question: 'Q', market_slug: '', event_slug: '', end_ts: null, outcome: 'Yes', side: 'BUY', price: 0.4, usdc: 10000,
  shares: 25000, first_ts: 1, last_ts: 2, n_fills: 1, post_edge: 0.06, net_edge: 0.04, max_entry: 0.45, fee: 0,
  quote: null, consensus: [], checks: [], history: null, ...extra,
});

const base = (): Snapshot => ({
  meta: { generated_at: 0, version: '0.1.0', mode: 'LIVE', counts: { wallets_scanned: 0, wallets_complete: 0, tests: 0, testable: 0, certified_wallets: 1, signals: 0, contacts: 0 },
    params: { bh_q: 0.1, min_usdc: 5000, conviction_k: 2, follow_size_usdc: 1000, min_net_edge: 0.02, signal_lookback_h: 24 },
    errors: { api: 0 }, validation_generated_at: null },
  signals: [], contacts: [], whales: [], validation: null,
});

describe('applyMessage', () => {
  it('upserts contacts newest first without duplicates', () => {
    let s = applyMessage(base(), { type: 'contact', data: sig('a', 'REJECTED') });
    s = applyMessage(s, { type: 'contact', data: sig('b', 'REJECTED') });
    s = applyMessage(s, { type: 'contact', data: sig('a', 'EXIT') });
    expect(s.contacts.map((c) => `${c.id}:${c.status}`)).toEqual(['a:EXIT', 'b:REJECTED']);
  });

  it('promotes a contact to a signal and demotes it again when it closes', () => {
    let s = applyMessage(base(), { type: 'contact', data: sig('a', 'REJECTED') });
    s = applyMessage(s, { type: 'signal', data: sig('a', 'SIGNAL') });
    expect(s.signals.map((x) => x.id)).toEqual(['a']);
    expect(s.contacts).toEqual([]);
    s = applyMessage(s, { type: 'signal_update', data: sig('a', 'STALE') });
    expect(s.signals[0].status).toBe('STALE');
    s = applyMessage(s, { type: 'signal_update', data: sig('a', 'EXPIRED') });
    expect(s.signals).toEqual([]);
    expect(s.contacts[0].status).toBe('EXPIRED');
  });

  it('applies book updates to signals on that asset only', () => {
    let s = applyMessage(base(), { type: 'signal', data: sig('a', 'SIGNAL') });
    s = applyMessage(s, { type: 'signal', data: sig('b', 'SIGNAL', { asset: 'other' }) });
    const quote = { vwap: 0.43, complete: true, book_as_of: 9, best_bid: 0.42, best_ask: 0.43, microprice: 0.425, levels: [] as [number, number][] };
    s = applyMessage(s, { type: 'book', data: { asset: 'yes', quote } });
    expect(s.signals.find((x) => x.id === 'a')?.quote?.vwap).toBe(0.43);
    expect(s.signals.find((x) => x.id === 'b')?.quote).toBeNull();
  });

  it('replaces meta on status and everything on state', () => {
    const s = applyMessage(base(), { type: 'status', data: { ...base().meta, feed_latency_ms: 120 } });
    expect(s.meta.feed_latency_ms).toBe(120);
    const fresh = { ...base(), signals: [sig('z', 'SIGNAL')] };
    expect(applyMessage(s, { type: 'state', data: fresh }).signals.map((x) => x.id)).toEqual(['z']);
  });
});

describe('tryLive', () => {
  it('returns the live state when the station answers', async () => {
    const f = (async () => new Response(JSON.stringify(base()), { status: 200 })) as typeof fetch;
    expect((await tryLive(f))?.meta.mode).toBe('LIVE');
  });

  it('returns null on GitHub Pages (no /api)', async () => {
    const f = (async () => new Response('', { status: 404 })) as typeof fetch;
    expect(await tryLive(f)).toBeNull();
    const broken = (async () => { throw new TypeError('network'); }) as typeof fetch;
    expect(await tryLive(broken)).toBeNull();
  });
});

describe('insider alerts', () => {
  it('keeps INSIDER alerts on the board like signals', () => {
    let s = applyMessage(base(), { type: 'signal', data: sig('i', 'INSIDER', { kind: 'INSIDER' }) });
    expect(s.signals.map((x) => x.id)).toEqual(['i']);
    s = applyMessage(s, { type: 'signal_update', data: sig('i', 'STALE', { kind: 'INSIDER' }) });
    expect(s.signals[0].status).toBe('STALE');
  });
});
