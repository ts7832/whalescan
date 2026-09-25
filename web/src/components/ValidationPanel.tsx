import { useState } from 'react';
import { fmtCents, fmtDate, fmtPct, fmtUsd, fmtUtc, shortWallet } from '../format';
import type { LedgerSummary, Validation } from '../types';
import { Panel } from './Panel';

function Calibration({ bins }: { bins: Validation['calibration'] }) {
  const s = 200;
  const pts = bins.filter((b) => b.n > 0 && b.predicted != null && b.realized != null);
  return (
    <svg className="calibration" viewBox={`0 0 ${s} ${s}`} role="img" aria-label="Calibration: predicted vs realized win rate">
      <rect x="0" y="0" width={s} height={s} fill="none" stroke="#1C242C" />
      <line x1="0" y1={s} x2={s} y2="0" stroke="#6B7785" strokeDasharray="4 4" />
      {pts.map((b) => (
        <circle key={b.lo} cx={b.predicted! * s} cy={s - b.realized! * s} r={3 + Math.min(8, Math.sqrt(b.n))} fill="#4FC3F7" fillOpacity="0.7" />
      ))}
      <text x="4" y="12" fill="#6B7785" fontSize="9">REALIZED</text>
      <text x={s - 4} y={s - 4} fill="#6B7785" fontSize="9" textAnchor="end">PREDICTED</text>
    </svg>
  );
}

function WalkForwardBody({ validation }: { validation: Validation | null }) {
  if (!validation) return <p className="empty">VALIDATION NOT RUN YET</p>;
  const cls = validation.verdict === 'EDGE CONFIRMED' ? 'ok' : validation.verdict === 'EDGE NOT CONFIRMED' ? 'bad' : 'wait';
  const rows = [
    ...(validation.groups.INSIDER ? [['INSIDER (FRESH)', validation.groups.INSIDER] as const] : []),
    ['SNIPER TIER A', validation.groups.A], ['SNIPER TIER B', validation.groups.B],
    ['ALL SNIPER', validation.groups.SIGNALS], ['BASELINE ≥ FLOOR', validation.groups.BASELINE],
  ] as const;
  const insiderCls = validation.insider_verdict === 'EDGE CONFIRMED' ? 'ok' : validation.insider_verdict === 'EDGE NOT CONFIRMED' ? 'bad' : 'wait';
  return (
    <>
      <div className="panel-right" style={{ marginBottom: 8 }}>WALK-FORWARD · {fmtDate(validation.generated_at)}</div>
      {validation.insider_verdict && <div className={`verdict ${insiderCls}`}>INSIDERS: {validation.insider_verdict}</div>}
      <div className={`verdict ${cls}`}>SNIPERS: {validation.verdict}</div>
      <table>
        <thead>
          <tr><th>GROUP</th><th className="num">N</th><th className="num" title="Mean return per share bought, in cents">RET/SH</th><th className="num">HIT</th><th className="num">T</th></tr>
        </thead>
        <tbody>
          {rows.map(([label, g]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="num">{g.n}{g.wallets != null ? ` / ${g.wallets}W` : ''}</td>
              <td className={`num ${g.mean_ret != null && g.mean_ret > 0 ? 'green' : 'red'}`}>{fmtCents(g.mean_ret)}</td>
              <td className="num">{fmtPct(g.hit_rate)}</td>
              <td className="num">{(g.wallet_t ?? g.t_stat) == null ? '—' : (g.wallet_t ?? g.t_stat)!.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Calibration bins={validation.calibration} />
      <ul className="caveats">
        {validation.caveats.map((c) => <li key={c}>{c}</li>)}
      </ul>
    </>
  );
}

const KIND_LABEL = { INSIDER: 'INSIDER', SNIPER: 'SNIPER', NEAR_MISS: 'NEAR MISS' } as const;

function TrackRecordBody({ ledger }: { ledger: LedgerSummary | null }) {
  if (!ledger) return <p className="empty">TRACK RECORD NOT PUBLISHED YET</p>;
  const kinds = (['INSIDER', 'SNIPER', 'NEAR_MISS'] as const).map((k) => [k, ledger.by_kind[k]] as const);
  return (
    <>
      <div className="panel-right" style={{ marginBottom: 8 }}>{fmtDate(ledger.generated_at)} · {ledger.totals.calls} CALLS · {ledger.totals.open} OPEN</div>
      <table>
        <thead>
          <tr><th>KIND</th><th className="num">N</th><th className="num">OPEN</th><th className="num">HIT</th>
            <th className="num">RET</th><th className="num">D7</th><th className="num">D28</th></tr>
        </thead>
        <tbody>
          {kinds.map(([k, s]) => (
            <tr key={k}>
              <td>{KIND_LABEL[k]}</td>
              <td className="num">{s.calls}</td>
              <td className="num">{s.open}</td>
              <td className="num">{fmtPct(s.hit_rate)}</td>
              <td className={`num ${s.mean_return != null && s.mean_return > 0 ? 'green' : s.mean_return != null ? 'red' : ''}`}>{fmtCents(s.mean_return)}</td>
              <td className="num">{fmtCents(s.day7_mean_return)}</td>
              <td className="num">{fmtCents(s.day28_mean_return)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="dim" style={{ fontSize: 10, letterSpacing: '0.08em', margin: '16px 0 4px' }}>RECENT CALLS</h3>
      <table>
        <thead>
          <tr><th>AGE</th><th>KIND</th><th>MARKET</th><th>WALLET</th><th className="num">ENTRY</th><th>STATUS</th><th className="num">RETURN</th></tr>
        </thead>
        <tbody>
          {ledger.recent.slice(0, 20).map((c) => (
            <tr key={c.id}>
              <td className="dim">{fmtUtc(c.call_ts)}</td>
              <td>{KIND_LABEL[c.kind]}{c.missed_rule ? ` · ${c.missed_rule}` : ''}</td>
              <td className="truncate" title={c.question}>{c.question}</td>
              <td>{shortWallet(c.wallet)}</td>
              <td className="num">{fmtUsd(c.entry_cost != null ? c.entry_cost * 1000 : null)}</td>
              <td className={c.status === 'WIN' ? 'green' : c.status === 'LOSS' ? 'red' : ''}>{c.status}</td>
              <td className={`num ${c.latest_return != null && c.latest_return > 0 ? 'green' : c.latest_return != null ? 'red' : ''}`}>{fmtCents(c.latest_return)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {ledger.analysis.status === 'OK' ? (
        <>
          <h3 className="dim" style={{ fontSize: 10, letterSpacing: '0.08em', margin: '16px 0 4px' }}>
            WINNERS VS LOSERS ({ledger.analysis.n_scored} SCORED)
          </h3>
          <table>
            <thead>
              <tr><th>FEATURE</th><th className="num">WIN MED</th><th className="num">LOSS MED</th><th className="num">P</th></tr>
            </thead>
            <tbody>
              {Object.entries(ledger.analysis.features).map(([f, s]) => (
                <tr key={f}>
                  <td>{f.toUpperCase()}</td>
                  <td className="num">{s.winners_median?.toFixed(2) ?? '—'}</td>
                  <td className="num">{s.losers_median?.toFixed(2) ?? '—'}</td>
                  <td className={`num ${s.p_value != null && s.p_value < 0.05 ? 'green' : ''}`}>{s.p_value?.toFixed(3) ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {ledger.analysis.model.status === 'OK' && (
            <div className="stat" style={{ marginTop: 8, display: 'inline-block' }}>
              <span>MODEL AUC ({ledger.analysis.model.n} SCORED)</span>{ledger.analysis.model.auc?.toFixed(3) ?? '—'}
            </div>
          )}
        </>
      ) : (
        <p className="empty">
          NEEDS {ledger.analysis.n_scored < 30 ? '≥ 30' : '≥ 200'} SETTLED CALLS FOR ANALYSIS ({ledger.analysis.n_scored} SO FAR)
        </p>
      )}
    </>
  );
}

export function ValidationPanel({ validation, ledger }: { validation: Validation | null; ledger?: LedgerSummary | null }) {
  const [tab, setTab] = useState<'VALIDATION' | 'TRACK RECORD'>('TRACK RECORD');
  const switcher = (
    <div role="tablist" aria-label="Analysis view">
      {(['TRACK RECORD', 'VALIDATION'] as const).map((t) => (
        <button key={t} role="tab" aria-selected={tab === t} className={`tab-btn ${tab === t ? 'active' : ''}`}
                onClick={() => setTab(t)}>
          {t}
        </button>
      ))}
    </div>
  );
  return (
    <Panel code="05" title={tab} className="area-validation" right={switcher}>
      {tab === 'VALIDATION' ? <WalkForwardBody validation={validation} /> : <TrackRecordBody ledger={ledger ?? null} />}
    </Panel>
  );
}
