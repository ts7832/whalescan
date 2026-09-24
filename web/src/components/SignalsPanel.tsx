import type { RefObject } from 'react';
import { fmtAge, fmtCents, fmtPrice, fmtUsd, signalsEmptyMessage } from '../format';
import type { Meta, Signal } from '../types';
import { Panel } from './Panel';

interface Props {
  signals: Signal[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  meta: Meta;
  now: number;
  categories: string[];
  category: string;
  onCategory: (c: string) => void;
  filterRef: RefObject<HTMLSelectElement | null>;
}

export function SignalsPanel({ signals, selectedId, onSelect, meta, now, categories, category, onCategory, filterRef }: Props) {
  const filter = (
    <select ref={filterRef} value={category} onChange={(e) => onCategory(e.target.value)} aria-label="Category filter">
      {['ALL', ...categories].map((c) => <option key={c}>{c}</option>)}
    </select>
  );
  return (
    <Panel code="01" title="SIGNALS" className="area-signals" right={filter}>
      {signals.length === 0 ? (
        <p className="empty">{signalsEmptyMessage(meta)}</p>
      ) : (
        <ul className="signal-list">
          {signals.map((s) => (
            <li key={s.id}>
              <SignalCard s={s} selected={s.id === selectedId} onSelect={() => onSelect(s.id)} now={now} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function SignalCard({ s, selected, onSelect, now }: { s: Signal; selected: boolean; onSelect: () => void; now: number }) {
  const bookAge = s.quote ? now - s.quote.book_as_of : null;
  return (
    <button className={`signal tier-${s.tier} ${selected ? 'selected' : ''}`} onClick={onSelect}>
      <div className="row">
        <span className={`tier tier-${s.tier}`}>TIER {s.tier}</span>
        <span className="dim">{s.category}</span>
        <span className="dim right">{fmtAge(now - s.last_ts)} AGO</span>
      </div>
      <div className="question">{s.question}</div>
      <div className="row">
        <span>BUY <b>{s.outcome.toUpperCase()}</b></span>
        <span>@ {fmtPrice(s.price)}</span>
        <span>{fmtUsd(s.usdc)}</span>
      </div>
      <dl className="kv">
        <dt>NET EDGE</dt><dd className="green">{fmtCents(s.net_edge)}</dd>
        <dt>MAX ENTRY</dt><dd className="amber">{fmtPrice(s.max_entry)}</dd>
        <dt>FOLLOW</dt><dd>{fmtPrice(s.quote?.vwap)}</dd>
        <dt>WHALES</dt><dd>{s.consensus.length}</dd>
      </dl>
      {bookAge != null && bookAge > 3600 && <div className="stale">BOOK {fmtAge(bookAge)} OLD — RE-CHECK PRICE BEFORE ENTRY</div>}
    </button>
  );
}
