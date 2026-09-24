import { fmtCents, fmtDate, fmtPct } from '../format';
import type { Validation } from '../types';
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

export function ValidationPanel({ validation }: { validation: Validation | null }) {
  if (!validation) {
    return (
      <Panel code="05" title="VALIDATION" className="area-validation">
        <p className="empty">VALIDATION NOT RUN YET</p>
      </Panel>
    );
  }
  const cls = validation.verdict === 'EDGE CONFIRMED' ? 'ok' : validation.verdict === 'EDGE NOT CONFIRMED' ? 'bad' : 'wait';
  const rows = [
    ...(validation.groups.INSIDER ? [['INSIDER (FRESH)', validation.groups.INSIDER] as const] : []),
    ['SNIPER TIER A', validation.groups.A], ['SNIPER TIER B', validation.groups.B],
    ['ALL SNIPER', validation.groups.SIGNALS], ['BASELINE ≥ FLOOR', validation.groups.BASELINE],
  ] as const;
  const insiderCls = validation.insider_verdict === 'EDGE CONFIRMED' ? 'ok' : validation.insider_verdict === 'EDGE NOT CONFIRMED' ? 'bad' : 'wait';
  return (
    <Panel code="05" title="VALIDATION" className="area-validation" right={`WALK-FORWARD · ${fmtDate(validation.generated_at)}`}>
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
    </Panel>
  );
}
