import { fmtAge, fmtUtc } from '../format';
import type { Meta } from '../types';

export function StatusBar({ meta, now }: { meta: Meta; now: number }) {
  const age = now - meta.generated_at;
  return (
    <header className="statusbar">
      <div className="brand">WHALESCAN <span className="dim">// POLYMARKET INTELLIGENCE</span></div>
      <div className="status-items">
        <span className={`badge ${age > 8 * 3600 ? 'warn' : 'ok'}`}>
          ● {meta.mode} · {fmtUtc(meta.generated_at)} · {fmtAge(age)} AGO
        </span>
        <span>SCANNED <b>{meta.counts.wallets_scanned}</b></span>
        <span>TESTABLE <b>{meta.counts.testable}</b></span>
        <span>CERTIFIED <b className="green">{meta.counts.certified_wallets}</b></span>
        <span>SIGNALS <b className="amber">{meta.counts.signals}</b></span>
        <span>FDR q=<b>{meta.params.bh_q}</b></span>
        {meta.errors.api > 0 && <span className="red">API ERR {meta.errors.api}</span>}
      </div>
    </header>
  );
}
