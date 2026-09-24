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
  components: {
    items: { key: string; name: string; value: number | null; max: number; note: string }[];
    tier: string | null;
  } | null;
  window_ok: boolean;
  degraded: boolean;
}

export interface HintRow {
  template_id: string;
  tier: string;
  text: string;
  links: HintSentence[] | null;
}

export type Role = "core" | "follow" | "avoid" | "other";
export type HoldEval = "intact" | "review" | "broken";
export type ListType = "buy" | "buy_invalid" | "sell" | "hold_watch" | "point" | "lowbase";

export interface HintSentence {
  text: string;
  links: string[];
}

export interface SectorRoleRow {
  code: string;
  name: string | null;
  role: Role;
  tags: string[] | null;
  score: number | null;
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
  roles: SectorRoleRow[];
}

export interface ActionRow {
  code: string;
  name: string | null;
  sector_id: string;
  sector_name: string | null;
  sector_level: SectorLevel | null;
  sector_stage: Stage | null;
  sector_half: string | null;
  days_in_stage: number | null;
  trade_date: string;
  list_type: ListType;
  basis_level: string;
  point_type: string | null;
  tags: string[] | null;
  evidence: EvidenceMap | null;
  invalidation: unknown;
  score: number | null;
  score_tier: string | null;
  risk_total: number | null;
  retention_5d: number | null;
  close: number | null;
  pct_chg: number | null;
  net_main: number | null;
  main_ratio: number | null;
}

export interface TrackingRow {
  list_type: string;
  code: string;
  name?: string | null;
  sector_id: string;
  sector_name: string | null;
  entered_date: string;
  entered_price: number | null;
  entered_stage: Stage | null;
  exited_date: string | null;
  exit_reason: string | null;
  last_close?: number | null;
  ret_since?: number | null;
  excess_vs_eqw?: number | null;
  excess_vs_sector?: number | null;
  current_role?: Role | null;
  current_actions?: string[] | null;
}

export interface WatchRow {
  code: string;
  group_id: string;
  added_at: string;
  name: string | null;
  kind: "stock" | "sector";
  net_main: number | null;
  main_ratio: number | null;
  pct_chg: number | null;
  close: number | null;
  hold_eval: HoldEval | null;
  hold_text: string | null;
}

export interface StatGroup {
  list_type: string;
  point_type: string;
  horizon: number;
  risk_gate: boolean | null;
  n: number;
  mean_ret: number | null;
  median_ret: number | null;
  win_rate: number | null;
  mean_excess_sector: number | null;
  mean_excess_eqw: number | null;
  mean_max_drawdown: number | null;
  enough: boolean;
}

export interface StatsData {
  min_samples: number;
  horizons: Record<string, number[]>;
  groups: StatGroup[];
}

export interface BriefRow {
  trade_date: string;
  kind: "close" | "premarket";
  markdown?: string;
  generated_at: string;
}

export interface EvidenceGroup {
  hit: boolean;
  items: Evidence[];
}

export interface StockProfile {
  basis_sector: string | null;
  basis_sector_name: string | null;
  basis_level: SectorLevel | null;
  identity: {
    concepts: string[];
    is_concept_stock: boolean;
    amount_tier: string | null;
    indices: string[];
    board: string | null;
    float_mv_tier: string | null;
  } | null;
  exclusions: Record<"control" | "crash" | "untradable", EvidenceGroup> | null;
  tradable: boolean;
  score: number | null;
  score_tier: string | null;
  score_components: ScoreComponents | null;
}

export interface ScoreComponents {
  items: { key: string; name: string; value: number | null; max: number }[];
  penalty: number | null;
  penalty_reasons: string[];
}

export interface StockRoleRow {
  sector_id: string;
  sector_name: string | null;
  level: SectorLevel | null;
  role: Role;
  tags: string[] | null;
  score: number | null;
  score_components: ScoreComponents | null;
  evidence: EvidenceMap | null;
}

export interface StockActionRow {
  sector_id: string;
  sector_name: string | null;
  list_type: ListType;
  point_type: string | null;
  basis_level: string;
  tags: string[] | null;
  evidence: EvidenceMap | null;
  invalidation: unknown;
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
  roles: StockRoleRow[];
  indices: IndexRow[];
  hold_eval: {
    eval: HoldEval;
    changed_from: HoldEval | null;
    evidence: { text?: string; items?: string[] } | null;
  } | null;
  hints: HintRow[];
  profile: StockProfile | null;
  actions: StockActionRow[];
  tracking: TrackingRow[];
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
