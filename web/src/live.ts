import type { LiveMessage, Signal, Snapshot } from './types';

const MAX_CONTACTS = 300;
const OPEN: Signal['status'][] = ['INSIDER', 'SIGNAL', 'STALE'];

const without = (list: Signal[], id: string) => list.filter((x) => x.id !== id);
const upsertFront = (list: Signal[], s: Signal) => [s, ...without(list, s.id)].slice(0, MAX_CONTACTS);
const upsertInPlace = (list: Signal[], s: Signal) =>
  list.some((x) => x.id === s.id) ? list.map((x) => (x.id === s.id ? s : x)) : [...list, s];

/** Pure reducer: the next dashboard state after one message from the live station. */
export function applyMessage(snap: Snapshot, msg: LiveMessage): Snapshot {
  switch (msg.type) {
    case 'state':
      return msg.data;
    case 'status':
      return { ...snap, meta: msg.data };
    case 'contact':
      return { ...snap, contacts: upsertFront(snap.contacts, msg.data), signals: without(snap.signals, msg.data.id) };
    case 'signal':
    case 'signal_update':
      if (OPEN.includes(msg.data.status)) {
        return { ...snap, signals: upsertInPlace(snap.signals, msg.data), contacts: without(snap.contacts, msg.data.id) };
      }
      return { ...snap, signals: without(snap.signals, msg.data.id), contacts: upsertFront(snap.contacts, msg.data) };
    case 'book':
      return {
        ...snap,
        signals: snap.signals.map((s) => (s.asset === msg.data.asset ? { ...s, quote: msg.data.quote } : s)),
      };
  }
}

/** The station serves /api/state; GitHub Pages does not. Null means "use the static snapshot". */
export async function tryLive(fetcher: typeof fetch = fetch): Promise<Snapshot | null> {
  try {
    const r = await fetcher('./api/state', { cache: 'no-store' });
    if (!r.ok) return null;
    const snap = (await r.json()) as Snapshot;
    return snap?.meta?.mode === 'LIVE' ? snap : null;
  } catch {
    return null;
  }
}

/** Keep a WebSocket to the station open, reconnecting with capped exponential backoff. Returns a stop function. */
export function connectLive(onMessage: (m: LiveMessage) => void, onLink: (up: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let attempt = 0;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const open = () => {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    ws = new WebSocket(url);
    ws.onopen = () => { attempt = 0; onLink(true); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data) as LiveMessage);
    ws.onclose = () => {
      onLink(false);
      if (stopped) return;
      const delay = Math.min(30_000, 1000 * 2 ** attempt++) * (0.5 + Math.random());
      timer = setTimeout(open, delay);
    };
  };
  open();
  return () => { stopped = true; clearTimeout(timer); ws?.close(); };
}
