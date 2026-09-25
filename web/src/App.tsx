import { useEffect, useMemo, useRef, useState } from 'react';
import { BookPanel } from './components/BookPanel';
import { ContactsPanel } from './components/ContactsPanel';
import { DossierPanel } from './components/DossierPanel';
import { SignalsPanel } from './components/SignalsPanel';
import { StatusBar } from './components/StatusBar';
import { ValidationPanel } from './components/ValidationPanel';
import { loadLedgerSummary, loadSnapshot } from './data';
import { applyMessage, connectLive, tryLive } from './live';
import { pickWallet } from './select';
import type { LedgerSummary, Snapshot } from './types';

const nowSeconds = () => Math.floor(Date.now() / 1000);
const LEDGER_REFRESH_MS = 60_000; // the ledger only changes once per sweep round (~15 min); no need to poll faster

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [ledger, setLedger] = useState<LedgerSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [walletPick, setWalletPick] = useState<string | null>(null);
  const [category, setCategory] = useState('ALL');
  const [now, setNow] = useState(nowSeconds);
  const [linkUp, setLinkUp] = useState<boolean | null>(null);
  const filterRef = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    let stopLive: (() => void) | undefined;
    (async () => {
      const live = await tryLive();
      if (live) {
        setSnap(live);
        stopLive = connectLive((m) => setSnap((s) => (s ? applyMessage(s, m) : s)), setLinkUp);
      } else {
        setSnap(await loadSnapshot());
      }
    })().catch((e: unknown) => setError(String(e instanceof Error ? e.message : e)));
    const t = setInterval(() => setNow(nowSeconds()), 1000);
    return () => { clearInterval(t); stopLive?.(); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const refresh = () => { loadLedgerSummary().then((s) => { if (!cancelled) setLedger(s); }); };
    refresh();
    const t = setInterval(refresh, LEDGER_REFRESH_MS);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  const categories = useMemo(() => [...new Set((snap?.signals ?? []).map((s) => s.category))].sort(), [snap]);
  const signals = useMemo(
    () => (snap?.signals ?? []).filter((s) => category === 'ALL' || s.category === category),
    [snap, category],
  );
  const selected = signals.find((s) => s.id === selectedId) ?? signals[0] ?? null;
  const wallet = pickWallet(walletPick, selected?.wallet ?? null, snap?.whales ?? []);
  const whale = snap?.whales.find((w) => w.wallet === wallet) ?? null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLSelectElement) return;
      const i = selected ? signals.indexOf(selected) : -1;
      if (e.key === 'j' && i + 1 < signals.length) { setSelectedId(signals[i + 1].id); setWalletPick(null); }
      else if (e.key === 'k' && i > 0) { setSelectedId(signals[i - 1].id); setWalletPick(null); }
      else if (e.key === 'Enter' && selected) setWalletPick(selected.wallet);
      else if (e.key === '/') { e.preventDefault(); filterRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [signals, selected]);

  if (error) return <div className="boot"><div className="red">{error}</div></div>;
  if (!snap) return <div className="boot">ACQUIRING SNAPSHOT…</div>;

  return (
    <>
      <StatusBar meta={snap.meta} now={now} linkUp={linkUp} />
      <main className="grid">
        <SignalsPanel
          signals={signals} selectedId={selected?.id ?? null}
          onSelect={(id) => { setSelectedId(id); setWalletPick(null); }}
          meta={snap.meta} now={now} categories={categories} category={category} onCategory={setCategory} filterRef={filterRef}
        />
        <BookPanel signal={selected} now={now} followSize={snap.meta.params.follow_size_usdc} />
        <DossierPanel whale={whale} wallet={wallet} />
        <ContactsPanel contacts={snap.contacts} now={now} onWallet={setWalletPick} />
        <ValidationPanel validation={snap.validation} ledger={ledger} />
      </main>
    </>
  );
}
