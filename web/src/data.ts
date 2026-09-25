import type { Meta, Signal, Snapshot, Validation, Whale } from './types';

/** Where the 15-minute sweep publishes fresh alerts (the repo's `data` branch). Set at build time. */
const ALERTS_URL: string | undefined = import.meta.env.VITE_ALERTS_URL;

async function getJson<T>(url: string, fetcher: typeof fetch): Promise<T | null> {
  const r = await fetcher(url, { cache: 'no-store' });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** Fresh copy from the alerts URL when it answers, else the copy deployed with the site. */
async function freshest<T>(name: string, base: string, alertsBase: string | undefined, fetcher: typeof fetch): Promise<T | null> {
  if (alertsBase) {
    try {
      const fresh = await getJson<T>(`${alertsBase}/${name}`, fetcher);
      if (fresh !== null) return fresh;
    } catch {
      // unreachable or broken: fall back to the site's own copy below
    }
  }
  return getJson<T>(`${base}/${name}`, fetcher);
}

export async function loadSnapshot(base = './data', fetcher: typeof fetch = fetch, alertsBase = ALERTS_URL): Promise<Snapshot> {
  const [meta, signals, contacts, whales, validation] = await Promise.all([
    freshest<Meta>('meta.json', base, alertsBase, fetcher),
    freshest<Signal[]>('signals.json', base, alertsBase, fetcher),
    freshest<Signal[]>('contacts.json', base, alertsBase, fetcher),
    getJson<Whale[]>(`${base}/whales.json`, fetcher),
    getJson<Validation>(`${base}/validation.json`, fetcher),
  ]);
  if (!meta) throw new Error('NO SNAPSHOT DATA — RUN `whalescan batch` FIRST');
  return { meta, signals: signals ?? [], contacts: contacts ?? [], whales: whales ?? [], validation };
}
