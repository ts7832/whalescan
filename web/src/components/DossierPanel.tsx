import { fmtCents, fmtDate, fmtP, fmtPrice, fmtUsd, shortWallet } from '../format';
import type { Whale } from '../types';
import { Panel } from './Panel';

export function DossierPanel({ whale, wallet }: { whale: Whale | null; wallet: string | null }) {
  if (!whale) {
    return (
      <Panel code="03" title="DOSSIER" className="area-dossier">
        <p className="empty">{wallet ? `NO DOSSIER FOR ${shortWallet(wallet)} — NOT IN SCORED SET` : 'SELECT A SIGNAL OR CONTACT'}</p>
      </Panel>
    );
  }
  const status = whale.certified ? <span className="green">CERTIFIED</span> : <span className="amber">WATCHLIST</span>;
  return (
    <Panel code="03" title="DOSSIER" className="area-dossier" right={status}>
      <div className="row">
        <b>{whale.name || shortWallet(whale.wallet)}</b>{' '}
        <a className="link" href={`https://polymarket.com/profile/${whale.wallet}`} target="_blank" rel="noreferrer">{shortWallet(whale.wallet)} ↗</a>
      </div>
      <div className="stats">
        <div className="stat"><span>BEST CAT</span>{whale.best_category ?? '—'}</div>
        <div className="stat"><span>POST EDGE</span>{fmtCents(whale.best_post_edge)}</div>
        <div className="stat"><span>MEDIAN BET</span>{fmtUsd(whale.median_stake)}</div>
        <div className="stat"><span>FLAGS</span>{whale.flags || 'NONE'}</div>
      </div>
      <table>
        <thead>
          <tr><th>CATEGORY</th><th className="num">N</th><th className="num">EDGE</th><th className="num">POST</th><th className="num">P</th><th>FDR</th></tr>
        </thead>
        <tbody>
          {whale.categories.map((c) => (
            <tr key={c.category}>
              <td>{c.category}</td>
              <td className="num">{c.n}</td>
              <td className="num">{fmtCents(c.edge)}</td>
              <td className="num">{fmtCents(c.post_edge)}</td>
              <td className="num">{fmtP(c.p_value)}</td>
              <td className={c.certified ? 'green' : c.bh_pass ? 'amber' : 'dim'}>{c.certified ? 'CERT' : c.bh_pass ? 'PASS' : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className="dim" style={{ fontSize: 10, letterSpacing: '0.08em', margin: '16px 0 4px' }}>RECENT RESOLVED</h3>
      <table>
        <tbody>
          {whale.recent.map((r, i) => (
            <tr key={i}>
              <td className={r.won ? 'green' : 'red'}>{r.won ? 'W' : 'L'}</td>
              <td className="truncate" title={r.title}>{r.title}</td>
              <td>{r.outcome.toUpperCase()}</td>
              <td className="num">{fmtPrice(r.price)}</td>
              <td className="num">{fmtUsd(r.stake)}</td>
              <td className="dim">{fmtDate(r.closed_ts)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
