import { describe, expect, it } from 'vitest';
import { loadLedgerSummary, loadSnapshot } from './data';

function fakeFetch(files: Record<string, unknown>): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const name = String(input).split('/').pop() ?? '';
    return name in files
      ? new Response(JSON.stringify(files[name]), { status: 200 })
      : new Response('', { status: 404 });
  }) as typeof fetch;
}

describe('loadSnapshot', () => {
  it('treats validation as optional', async () => {
    const snap = await loadSnapshot('./data', fakeFetch({
      'meta.json': { generated_at: 1 }, 'signals.json': [], 'contacts.json': [], 'whales.json': [],
    }));
    expect(snap.validation).toBeNull();
    expect(snap.meta.generated_at).toBe(1);
  });

  it('fails clearly when no snapshot exists', async () => {
    await expect(loadSnapshot('./data', fakeFetch({}))).rejects.toThrow(/NO SNAPSHOT DATA/);
  });

  it('surfaces server errors', async () => {
    const broken = (async () => new Response('', { status: 500 })) as typeof fetch;
    await expect(loadSnapshot('./data', broken)).rejects.toThrow(/HTTP 500/);
  });
});

describe('alerts from the data branch', () => {
  const files = (prefix: string, names: Record<string, unknown>): typeof fetch =>
    (async (input: RequestInfo | URL) => {
      const url = String(input);
      const name = url.split('/').pop() ?? '';
      return url.startsWith(prefix) && name in names
        ? new Response(JSON.stringify(names[name]), { status: 200 })
        : new Response('', { status: 404 });
    }) as typeof fetch;

  it('takes meta/signals/contacts from the alerts URL and dossiers/validation from the site', async () => {
    const site = { 'meta.json': { generated_at: 1 }, 'signals.json': [], 'contacts.json': [], 'whales.json': [{ wallet: 'w' }] };
    const alerts = { 'meta.json': { generated_at: 99 }, 'signals.json': [{ id: 'fresh' }], 'contacts.json': [] };
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).startsWith('https://raw.test/') ? files('https://raw.test/', alerts)(input, init) : files('./data', site)(input, init)) as typeof fetch;
    const snap = await loadSnapshot('./data', fetcher, 'https://raw.test/snapshot');
    expect(snap.meta.generated_at).toBe(99);
    expect(snap.signals.map((s) => s.id)).toEqual(['fresh']);
    expect(snap.whales.length).toBe(1);
  });

  it('falls back to the site copy when the alerts URL is unreachable', async () => {
    const site = { 'meta.json': { generated_at: 1 }, 'signals.json': [], 'contacts.json': [], 'whales.json': [] };
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith('https://raw.test/')) throw new TypeError('offline');
      return files('./data', site)(input, init);
    }) as typeof fetch;
    expect((await loadSnapshot('./data', fetcher, 'https://raw.test/snapshot')).meta.generated_at).toBe(1);
  });
});

describe('loadLedgerSummary', () => {
  const fetcher = (async (input: RequestInfo | URL) =>
    String(input) === 'https://raw.test/ledger/summary.json'
      ? new Response(JSON.stringify({ generated_at: 5 }), { status: 200 })
      : new Response('', { status: 404 })) as typeof fetch;

  it('returns the summary when the ledger URL is configured and answers', async () => {
    const summary = await loadLedgerSummary(fetcher, 'https://raw.test/ledger');
    expect(summary?.generated_at).toBe(5);
  });

  it('returns null when no ledger URL is configured', async () => {
    expect(await loadLedgerSummary(fetcher, undefined)).toBeNull();
  });

  it('returns null (not throw) when the ledger has never been published yet', async () => {
    const notFound = (async () => new Response('', { status: 404 })) as typeof fetch;
    expect(await loadLedgerSummary(notFound, 'https://raw.test/ledger')).toBeNull();
  });

  it('returns null (not throw) when the fetch itself fails', async () => {
    const broken = (async () => { throw new TypeError('offline'); }) as typeof fetch;
    expect(await loadLedgerSummary(broken, 'https://raw.test/ledger')).toBeNull();
  });
});
