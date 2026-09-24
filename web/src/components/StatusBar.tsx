import { fmtAge, fmtUtc } from '../format';
import type { Meta } from '../types';

export function StatusBar({ meta, now, linkUp }: { meta: Meta; now: number; linkUp: boolean | null }) {
  const age = now - meta.generated_at;
  const live = meta.mode === 'LIVE';
  return (
    <header className="statusbar">
      <div className="brand">WHALESCAN <span className="dim">// POLYMARKET INTELLIGENCE</span></div>
      <div className="status-items">
        {live ? (
          <>
            <span className={`badge ${linkUp ? 'ok' : 'bad'}`}>● {linkUp ? 'LIVE' : 'LINK DOWN'}</span>
            <span>FEED LAT <b>{meta.feed_latency_ms == null ? '—' : `${meta.feed_latency_ms}MS`}</b></span>
            {Object.entries(meta.link ?? {}).map(([k, v]) => (
              <span key={k}>{k.toUpperCase()} <b className={v === 'connected' ? 'green' : 'amber'}>{v.toUpperCase()}</b></span>
            ))}
            <span>BOOKS <b>{meta.watching ?? 0}</b></span>
          </>
        ) : (
          <span className={`badge ${age > 8 * 3600 ? 'warn' : 'ok'}`}>
            ● {meta.mode} · {fmtUtc(meta.generated_at)} · {fmtAge(age)} AGO
          </span>
        )}
        <span>INSIDERS <b className="violet">{meta.counts.insiders ?? 0}</b></span>
        <span>SCANNED <b>{meta.counts.wallets_scanned}</b></span>
        <span>TESTABLE <b>{meta.counts.testable}</b></span>
        <span>SNIPERS <b className="green">{meta.counts.certified_wallets}</b></span>
        <span>SNIPER ALERTS <b className="amber">{meta.counts.signals - (meta.counts.insiders ?? 0)}</b></span>
        <span>FDR q=<b>{meta.params.bh_q}</b></span>
        {meta.errors.api > 0 && <span className="red">API ERR {meta.errors.api}</span>}
      </div>
    </header>
  );
}
