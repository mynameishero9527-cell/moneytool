import type { Stage } from "../api/types";

export const STAGE_LABEL: Record<Stage, string> = {
  freeze: "冰点",
  start: "启动",
  spread: "扩散加速",
  climax: "高潮拥挤",
  diverge: "分歧背离",
  ebb: "退潮",
};

export const STAGE_ORDER: Stage[] = ["freeze", "start", "spread", "climax", "diverge", "ebb"];

export const STAGE_COLOR: Record<Stage, string> = {
  freeze: "#93C5FD",
  start: "#FCD34D",
  spread: "#FB923C",
  climax: "#DC2626",
  diverge: "#A855F7",
  ebb: "#64748B",
};

export const STATUS_LABEL: Record<string, string> = {
  intraday: "盘中",
  confirmed: "已确认",
  unreconciled: "未对账",
  missing: "缺失",
  degraded: "价格模式",
};

export const SEGMENT_LABEL: Record<string, string> = {
  auction: "集合竞价",
  "0930_1030": "09:30–10:30",
  "1030_1130": "10:30–11:30",
  "1300_1400": "13:00–14:00",
  "1400_1430": "14:00–14:30",
  "1430_1500": "14:30–15:00",
  close: "收盘",
};

export const REGIME_LABEL: Record<string, string> = {
  clear: "主线清晰",
  scattered: "主线分散",
  none: "无主线",
};

export const ROLE_LABEL: Record<string, string> = {
  core: "核心",
  follow: "跟随",
  avoid: "规避",
  other: "一般成分",
};

export const HOLD_LABEL: Record<string, string> = { intact: "完好", review: "需复核", broken: "已破坏" };

export const LIST_LABEL: Record<string, string> = {
  buy: "买入关注",
  buy_invalid: "买入关注失效",
  sell: "卖出关注",
  hold_watch: "持有观察",
  point: "买卖点",
  lowbase: "低位企稳",
  bought: "已标记买入",
};

export const POINT_LABEL: Record<string, string> = {
  start_confirm: "启动确认",
  pullback_hold: "回踩不破",
  breakout: "放量突破",
  low_base: "低位企稳",
  climax: "阶段性高潮",
  diverge: "背离",
  broken: "结构破坏",
  ebb: "周期退潮",
  expire: "跟踪期满",
};

export const INDEX_LABEL: Record<string, string> = {
  sector_sentiment: "板块情绪指数",
  sector_risk: "板块风险指数",
  stock_risk: "个股风险指数",
  retail_pressure: "散户压力指数",
  market_pressure: "市场情绪压力指数",
};

export const SECTOR_TIER_LABEL: Record<string, string> = { focus: "可关注", watch: "观察", avoid: "不参与" };

/** 金额 → 亿，2 位 */
export function yi(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (v / 1e8).toFixed(digits);
}

/** 比例 0–1 → 百分比 1 位 */
export function pct(v: number | null | undefined, digits = 1, sign = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = (v * 100).toFixed(digits);
  return (sign && v > 0 ? "+" : "") + s + "%";
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "neutral";
  return v > 0 ? "up" : "down";
}
