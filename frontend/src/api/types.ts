// 与后端 moneytool/api/schemas.py 对齐的手写类型；接口稳定后改为 `npm run gen:api` 生成。

export type DataStatus = "intraday" | "confirmed" | "unreconciled" | "missing" | "degraded";
export type Stage = "freeze" | "start" | "spread" | "climax" | "diverge" | "ebb";
export type SectorLevel = "L1" | "L2" | "concept";

export interface Meta {
  trade_date: string | null;
  segment: string | null;
  status: DataStatus;
  param_version: string | null;
  generated_at: string;
  reason: string | null;
}

export interface Envelope<T> {
  meta: Meta;
  data: T;
}

export interface Evidence {
  rule: string;
  metric: string;
  value: number | boolean | null;
  threshold: number | boolean | null;
  op: string;
  hit: boolean;
  note?: string | null;
}

export type EvidenceMap = Record<string, Evidence[] | unknown>;

export interface StatusData {
  latest_confirmed: string | null;
  today: string;
  is_trading_day: boolean;
  segments_captured: string[];
  risk_gate: boolean;
  data_status: string;
  degraded_reason: string | null;
  backfill: Record<string, Record<string, number>>;
  quality: QualityRow[];
  counts: Record<string, number>;
  param_version: string | null;
  version: string;
}

export interface QualityRow {
  source: string;
  endpoint: string;
  segment: string;
  status: string;
  reason: string | null;
  value: number | null;
  created_at: string;
}

export interface MarketOverview {
  trade_date: string;
  segment: string;
  param_version: string;
  eqw_ret: number | null;
  eqw_ret_5d: number | null;
  breadth_all: number | null;
  limit_up_count: number | null;
  limit_up_ratio_all: number | null;
  amount_all: number | null;
  net_main_all: number | null;
  regime: "clear" | "scattered" | "none";
  mainline: unknown;
  rotation: unknown;
  market_pressure: number | null;
  pressure_components: Record<string, number | null> | null;
  risk_gate: boolean;
  risk_gate_reasons: string[] | null;
  evidence: Record<string, unknown> | null;
  data_status: DataStatus;
  stage_counts: Record<string, Record<Stage, number>>;
  captured_segments: string[];
}

export interface MarketHistoryRow {
  trade_date: string;
  eqw_ret: number | null;
  eqw_ret_5d: number | null;
  breadth_all: number | null;
  limit_up_count: number | null;
  amount_all: number | null;
  net_main_all: number | null;
  regime: string;
  market_pressure: number | null;
  risk_gate: boolean;
  data_status: DataStatus;
}

export interface SectorMetrics {
  sector_net_main: number | null;
  sector_main_ratio: number | null;
  main_mean_5d: number | null;
  main_multiple_prev5: number | null;
  main_slope_5d: number | null;
  sector_pct_chg: number | null;
  ret_5d: number | null;
  ret_20d: number | null;
  market_share: number | null;
  breadth: number | null;
  limit_up_count: number | null;
  turnover_vs_20d: number | null;
  amount_tier: string | null;
  member_count: number | null;
  small_sample: boolean | null;
}

export interface SectorStageRow {
  sector_id: string;
  name: string;
  level: SectorLevel;
  parent_id: string | null;
  dominant_l1: string | null;
  trade_date: string;
  segment?: string;
  param_version: string;
  stage: Stage;
  half: string | null;
  candidate: Stage | null;
  suspected_to: Stage | null;
  abnormal_transition: boolean;
  carried_over: boolean;
  days_in_stage: number | null;
  entered_from: Stage | null;
  suppressed: unknown;
  pattern: string | null;
  is_pulse: boolean;
  small_sample: boolean;
  attribution: unknown;
  evidence: EvidenceMap | null;
  metrics: SectorMetrics;
}

export interface SectorMember {
  code: string;
  name: string | null;
  is_st: boolean | null;
  net_main: number | null;
  main_ratio: number | null;
  pct_chg: number | null;
  close: number | null;
  reconciled: boolean | null;
  amount: number | null;
  turnover: number | null;
  metrics: Record<string, number | boolean | null>;
}

export interface IndexRow {
  index_name: string;
  total: number | null;
  components: Record<string, number | null> | null;
  window_ok: boolean;
  degraded: boolean;
}

export interface HintRow {
  template_id: string;
  tier: string;
  text: string;
  links: unknown;
}

export interface SectorDetail {
  sector: { sector_id: string; name: string; level: SectorLevel; parent_id: string | null };
  stage: (SectorStageRow & { features?: never }) | null;
  features: Record<string, number | boolean | string | null> | null;
  children: (Pick<SectorStageRow, "sector_id" | "name" | "stage" | "days_in_stage"> & {
    metrics: SectorMetrics;
  })[];
  members: SectorMember[];
  indices: IndexRow[];
  hints: HintRow[];
}

export interface SectorHistoryRow {
  trade_date: string;
  stage: Stage;
  half: string | null;
  candidate: Stage | null;
  days_in_stage: number | null;
  is_pulse: boolean;
  abnormal_transition: boolean;
  small_sample: boolean;
  sector_net_main: number | null;
  sector_main_ratio: number | null;
  main_mean_5d: number | null;
  sector_pct_chg: number | null;
  ret_20d: number | null;
  breadth: number | null;
  market_share: number | null;
  turnover_vs_20d: number | null;
}

export interface StockDetail {
  security: {
    code: string;
    name: string;
    exchange: string;
    board: string | null;
    list_date: string | null;
    is_st: boolean;
  };
  sectors: {
    sector_id: string;
    name: string;
    level: SectorLevel;
    stage: Stage | null;
    days_in_stage: number | null;
  }[];
  bar: Record<string, number | boolean | null> | null;
  flow: Record<string, number | boolean | null> | null;
  features: Record<string, number | boolean | null> | null;
  roles: unknown[];
  indices: IndexRow[];
  hold_eval: { eval: string; changed_from: string | null; evidence: unknown } | null;
  hints: HintRow[];
  marks: { marked_at: string; mark: string; note: string | null }[];
}

export interface StockHistoryRow {
  trade_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  amount: number | null;
  pct_chg: number | null;
  net_main: number | null;
  main_ratio: number | null;
  reconciled: boolean | null;
}

export interface SearchResult {
  stocks: { code: string; name: string; kind: "stock" }[];
  sectors: { code: string; name: string; kind: SectorLevel }[];
}
