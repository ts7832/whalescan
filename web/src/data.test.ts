import { describe, expect, it } from 'vitest';
import { loadSnapshot } from './data';

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
