export type Status = 'INSIDER' | 'SIGNAL' | 'STALE' | 'REJECTED' | 'EXIT' | 'CONFLICT' | 'EXPIRED';

export interface Check { code: string; passed: boolean; detail: string }

export interface Quote {
  vwap: number | null;
  complete: boolean;
  book_as_of: number;
  best_bid: number | null;
  best_ask: number | null;
  microprice: number | null;
  levels: [number, number][];
}

export interface Signal {
  id: string;
  /** INSIDER = fresh-account big bet (primary signal); SKILL = proven sniper */
  kind?: 'INSIDER' | 'SKILL';
  status: Status;
  tier: 'A' | 'B' | null;
  category: string;
  wallet: string;
  wallet_name: string;
  asset: string;
  question: string;
  market_slug: string;
  event_slug: string;
  end_ts: number | null;
  outcome: string;
  side: 'BUY' | 'SELL';
  price: number;
  usdc: number;
  shares: number;
  first_ts: number;
  last_ts: number;
  n_fills: number;
  post_edge: number | null;
  net_edge: number | null;
  max_entry: number | null;
  fee: number | null;
  quote: Quote | null;
  consensus: string[];
  checks: Check[];
  history: { t: number; p: number }[] | null;
  /** live mode: RESYNC while the book is re-fetched and the price is unconfirmed */
  book?: 'OK' | 'RESYNC';
}

export interface CategoryScore {
  category: string;
  n: number;
  n_eff: number;
  edge: number;
  post_edge: number;
  p_value: number;
  bh_pass: boolean;
  certified: boolean;
}

export interface Whale {
  wallet: string;
  name: string;
  certified: boolean;
  median_stake: number | null;
  flags: string;
  best_category: string | null;
  best_post_edge: number | null;
  categories: CategoryScore[];
  recent: { title: string; outcome: string; price: number; won: boolean; stake: number; closed_ts: number }[];
}

export interface GroupStats { n: number; mean_ret: number | null; hit_rate: number | null; t_stat: number | null; wallets?: number; wallet_t?: number | null }

export interface Validation {
  generated_at: number;
  verdict: 'INSUFFICIENT DATA' | 'EDGE CONFIRMED' | 'EDGE NOT CONFIRMED';
  folds: { cutoff: number; end: number; signals: number; mean_ret: number | null }[];
  groups: { A: GroupStats; B: GroupStats; SIGNALS: GroupStats; BASELINE: GroupStats; INSIDER?: GroupStats };
  insider_verdict?: 'INSUFFICIENT DATA' | 'EDGE CONFIRMED' | 'EDGE NOT CONFIRMED';
  calibration: { lo: number; hi: number; n: number; predicted: number | null; realized: number | null }[];
  params: { fold_days: number; min_history_days: number; slippage: number; n_sims: number };
  caveats: string[];
}

export interface Meta {
  generated_at: number;
  version: string;
  mode: 'SNAPSHOT' | 'LIVE';
  counts: {
    wallets_scanned: number; wallets_complete: number; tests: number; testable: number;
    certified_wallets: number; signals: number; contacts: number; insiders?: number;
  };
  params: { bh_q: number; min_usdc: number; conviction_k: number; follow_size_usdc: number; min_net_edge: number; signal_lookback_h: number };
  errors: { api: number; ws_dropped?: number };
  validation_generated_at: number | null;
  link?: Record<string, string>;
  feed_latency_ms?: number | null;
  watching?: number;
}

export interface Snapshot { meta: Meta; signals: Signal[]; contacts: Signal[]; whales: Whale[]; validation: Validation | null }

// --- Track Record ledger (data.ts loadLedgerSummary; the `ledger` branch's summary.json) ---

export type LedgerKind = 'INSIDER' | 'SNIPER' | 'NEAR_MISS';

export interface LedgerKindStats {
  calls: number;
  open: number;
  settled: number;
  hit_rate: number | null;
  mean_return: number | null;
  day7_mean_return: number | null;
  day28_mean_return: number | null;
}

export interface LedgerRecentCall {
  id: string;
  kind: LedgerKind;
  call_ts: number;
  wallet: string;
  question: string;
  category: string;
  tier: string | null;
  entry_cost: number | null;
  status: 'OPEN' | 'WIN' | 'LOSS';
  latest_return: number | null;
  missed_rule: string | null;
}

export interface LedgerFeatureStat {
  n_winners: number;
  n_losers: number;
  winners_median: number | null;
  losers_median: number | null;
  winners_mean: number | null;
  losers_mean: number | null;
  p_value: number | null;
}

export interface LedgerBucket {
  lo: number;
  hi: number;
  n: number;
  win_rate: number | null;
  mean_return: number | null;
}

export interface LedgerModel {
  status: 'OK' | 'INSUFFICIENT DATA';
  auc: number | null;
  coefficients: Record<string, number> | null;
  n: number;
}

export interface LedgerAnalysis {
  status: 'OK' | 'INSUFFICIENT DATA';
  n_scored: number;
  features: Record<string, LedgerFeatureStat>;
  buckets: Record<string, LedgerBucket[]>;
  model: LedgerModel;
}

export interface LedgerSummary {
  generated_at: number;
  totals: { calls: number; open: number; settled: number };
  by_kind: Record<LedgerKind, LedgerKindStats>;
  recent: LedgerRecentCall[];
  analysis: LedgerAnalysis;
}

export type LiveMessage =
  | { type: 'state'; data: Snapshot }
  | { type: 'status'; data: Meta }
  | { type: 'contact' | 'signal' | 'signal_update'; data: Signal }
  | { type: 'book'; data: { asset: string; quote: Quote } };
