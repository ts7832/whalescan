import { fmtAge, fmtPrice, fmtUsd, shortWallet } from '../format';
import type { Signal } from '../types';
import { Panel } from './Panel';

export function ContactsPanel({ contacts, now, onWallet }: { contacts: Signal[]; now: number; onWallet: (w: string) => void }) {
  return (
    <Panel code="04" title="CONTACTS" className="area-contacts" right={`${contacts.length} ≥ GATE FLOOR`}>
      {contacts.length === 0 ? (
        <p className="empty">NO LARGE POSITION EVENTS IN WINDOW</p>
      ) : (
        <table>
          <thead>
            <tr><th>AGE</th><th>WALLET</th><th>MARKET</th><th>SIDE</th><th className="num">SIZE</th><th className="num">PX</th><th>STATUS</th></tr>
          </thead>
          <tbody>
            {contacts.map((c) => {
              const fail = c.checks.find((k) => !k.passed);
              return (
                <tr key={c.id}>
                  <td className="dim">{fmtAge(now - c.last_ts)}</td>
                  <td><button className="link" onClick={() => onWallet(c.wallet)}>{c.wallet_name || shortWallet(c.wallet)}</button></td>
                  <td className="truncate" title={c.question}>{c.question}</td>
                  <td>{c.side} {c.outcome.toUpperCase()}</td>
                  <td className="num">{fmtUsd(c.usdc)}</td>
                  <td className="num">{fmtPrice(c.price)}</td>
                  <td className="gate-fail">
                    <span className={c.status === 'CONFLICT' ? 'red' : c.status === 'EXIT' ? 'cyan' : ''}>{c.status}</span>
                    {fail && c.status === 'REJECTED' && <> · {fail.code} {fail.detail}</>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
