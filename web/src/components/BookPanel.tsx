import { useMemo } from 'react';
import { fmtAge, fmtPrice, fmtUsd } from '../format';
import type { Signal } from '../types';
import { Panel } from './Panel';
import { PriceChart } from './PriceChart';

export function BookPanel({ signal, now, followSize }: { signal: Signal | null; now: number; followSize: number }) {
  const history = useMemo(() => signal?.history ?? [], [signal]);
  if (!signal) {
    return (
      <Panel code="02" title="BOOK" className="area-book">
        <p className="empty">NO SIGNAL SELECTED</p>
      </Panel>
    );
  }
  const q = signal.quote;
  let cumulative = 0;
  const maxUsd = Math.max(1, ...(q?.levels ?? []).map(([p, s]) => p * s));
  return (
    <Panel code="02" title="BOOK" className="area-book" right={q ? `AS OF ${fmtAge(now - q.book_as_of)} AGO` : 'NO BOOK'}>
      <div className="question" style={{ marginBottom: 8 }}>{signal.question}</div>
      <PriceChart history={history} entryTs={signal.first_ts} entryPrice={signal.price} maxEntry={signal.max_entry} />
      <div className="stats">
        <div className="stat"><span>BID</span>{fmtPrice(q?.best_bid)}</div>
        <div className="stat"><span>ASK</span>{fmtPrice(q?.best_ask)}</div>
        <div className="stat"><span>MICRO</span>{fmtPrice(q?.microprice)}</div>
        <div className="stat"><span>FOLLOW {fmtUsd(followSize)}</span>{fmtPrice(q?.vwap)}</div>
      </div>
      <div className="ladder" aria-label="Ask ladder">
        {(q?.levels ?? []).map(([price, size]) => {
          const usd = price * size;
          const filled = cumulative < followSize;
          cumulative += usd;
          return (
            <div className="ladder-row" key={price}>
              <span className="red">{fmtPrice(price)}</span>
              <div className={`ladder-bar ${filled ? 'filled' : ''}`} style={{ width: `${(usd / maxUsd) * 100}%` }} />
              <span className="dim" style={{ textAlign: 'right' }}>{fmtUsd(usd)}</span>
            </div>
          );
        })}
      </div>
      <table style={{ marginTop: 12 }}>
        <tbody>
          {signal.checks.map((c) => (
            <tr key={c.code}>
              <td className={c.passed ? 'green' : 'red'}>{c.passed ? '■' : '□'} {c.code}</td>
              <td className="dim">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}
