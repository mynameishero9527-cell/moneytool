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
