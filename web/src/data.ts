import type { Meta, Signal, Snapshot, Validation, Whale } from './types';

async function getJson<T>(url: string, fetcher: typeof fetch): Promise<T | null> {
  const r = await fetcher(url, { cache: 'no-store' });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export async function loadSnapshot(base = './data', fetcher: typeof fetch = fetch): Promise<Snapshot> {
  const [meta, signals, contacts, whales, validation] = await Promise.all([
    getJson<Meta>(`${base}/meta.json`, fetcher),
    getJson<Signal[]>(`${base}/signals.json`, fetcher),
    getJson<Signal[]>(`${base}/contacts.json`, fetcher),
    getJson<Whale[]>(`${base}/whales.json`, fetcher),
    getJson<Validation>(`${base}/validation.json`, fetcher),
  ]);
  if (!meta) throw new Error('NO SNAPSHOT DATA — RUN `whalescan batch` FIRST');
  return { meta, signals: signals ?? [], contacts: contacts ?? [], whales: whales ?? [], validation };
}
