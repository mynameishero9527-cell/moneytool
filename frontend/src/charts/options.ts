import type { ChartOption } from "../composables/useEcharts";
import type { MarketHistoryRow, SectorHistoryRow, Stage, StockHistoryRow } from "../api/types";
import { STAGE_COLOR, STAGE_LABEL, yi } from "./format";

const AXIS = { axisLabel: { fontSize: 11, color: "#6B7280" }, axisLine: { lineStyle: { color: "#E5E7EB" } } };
const GRID = { left: 56, right: 56, top: 28, bottom: 28 };

/** 连续同阶段日期合并为 markArea 色带 */
function stageAreas(rows: { trade_date: string; stage: Stage }[]) {
  const areas: [Record<string, unknown>, Record<string, unknown>][] = [];
  let start = 0;
  for (let i = 1; i <= rows.length; i++) {
    if (i === rows.length || rows[i]!.stage !== rows[start]!.stage) {
      const st = rows[start]!.stage;
      areas.push([
        { xAxis: rows[start]!.trade_date, itemStyle: { color: STAGE_COLOR[st], opacity: 0.16 }, name: STAGE_LABEL[st] },
        { xAxis: rows[i - 1]!.trade_date },
      ]);
      start = i;
    }
  }
  return areas;
}

function barColor(v: number | null) {
  return v !== null && v < 0 ? "#1F9D55" : "#D9363E";
}

/** 板块详情主图：主力净流入柱 + 5 日均值线 + 阶段色带 */
export function sectorHistoryOption(rows: SectorHistoryRow[]): ChartOption {
  const x = rows.map((r) => r.trade_date);
  return {
    tooltip: {
      trigger: "axis",
      formatter: (params: unknown) => {
        const ps = params as { dataIndex: number }[];
        const r = rows[ps[0]?.dataIndex ?? 0];
        if (!r) return "";
        return [
          `<b>${r.trade_date}</b>　${STAGE_LABEL[r.stage]}${r.days_in_stage ? ` 第${r.days_in_stage}日` : ""}`,
          `主力净流入 ${yi(r.sector_net_main)} 亿`,
          `5 日均值 ${yi(r.main_mean_5d)} 亿`,
          `市场份额 ${r.market_share === null ? "—" : (r.market_share * 100).toFixed(1) + "%"}`,
          `涨跌 ${r.sector_pct_chg === null ? "—" : (r.sector_pct_chg * 100).toFixed(2) + "%"}`,
        ].join("<br/>");
      },
    },
    grid: GRID,
    xAxis: { type: "category", data: x, ...AXIS },
    yAxis: [
      { type: "value", name: "亿", ...AXIS, splitLine: { lineStyle: { color: "#F3F4F6" } } },
      { type: "value", name: "份额", ...AXIS, splitLine: { show: false }, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%`, fontSize: 11 } },
    ],
    series: [
      {
        type: "bar",
        name: "主力净流入",
        data: rows.map((r) => ({
          value: r.sector_net_main === null ? null : r.sector_net_main / 1e8,
          itemStyle: { color: barColor(r.sector_net_main) },
        })),
        markArea: { silent: true, data: stageAreas(rows) },
      },
      {
        type: "line",
        name: "5 日均值",
        data: rows.map((r) => (r.main_mean_5d === null ? null : r.main_mean_5d / 1e8)),
        smooth: false,
        showSymbol: false,
        lineStyle: { color: "#111827", width: 1.5 },
      },
      {
        type: "line",
        name: "市场份额",
        yAxisIndex: 1,
        data: rows.map((r) => r.market_share),
        showSymbol: false,
        lineStyle: { color: "#6B7280", width: 1, type: "dashed" },
      },
    ],
  };
}

/** 市场序列：等权收益累计 + 全市场净流入柱 + 风控开关区间 */
export function marketHistoryOption(rows: MarketHistoryRow[]): ChartOption {
  let cum = 1;
  const eqw = rows.map((r) => {
    cum *= 1 + (r.eqw_ret ?? 0);
    return +(cum - 1).toFixed(4);
  });
  const gateAreas: [Record<string, unknown>, Record<string, unknown>][] = [];
  let s = -1;
  rows.forEach((r, i) => {
    if (r.risk_gate && s < 0) s = i;
    if ((!r.risk_gate || i === rows.length - 1) && s >= 0) {
      gateAreas.push([{ xAxis: rows[s]!.trade_date, itemStyle: { color: "#FEF3C7" } }, { xAxis: r.trade_date }]);
      s = -1;
    }
  });
  return {
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: GRID,
    xAxis: { type: "category", data: rows.map((r) => r.trade_date), ...AXIS },
    yAxis: [
      { type: "value", name: "净流入 亿", ...AXIS, splitLine: { lineStyle: { color: "#F3F4F6" } } },
      { type: "value", name: "等权累计", ...AXIS, splitLine: { show: false }, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%`, fontSize: 11 } },
    ],
    series: [
      {
        type: "bar",
        name: "全市场主力净流入",
        data: rows.map((r) => ({ value: r.net_main_all === null ? null : r.net_main_all / 1e8, itemStyle: { color: barColor(r.net_main_all) } })),
        markArea: { silent: true, data: gateAreas },
      },
      { type: "line", name: "等权累计收益", yAxisIndex: 1, data: eqw, showSymbol: false, lineStyle: { color: "#111827", width: 1.5 } },
      { type: "line", name: "情绪压力", yAxisIndex: 1, data: rows.map((r) => (r.market_pressure === null ? null : r.market_pressure / 100)), showSymbol: false, lineStyle: { color: "#A855F7", width: 1, type: "dotted" } },
    ],
  };
}

/** 个股：收盘线 + 主力净流入柱（缺失日留空，不填补） */
export function stockHistoryOption(rows: StockHistoryRow[]): ChartOption {
  return {
    tooltip: { trigger: "axis" },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: GRID,
    xAxis: { type: "category", data: rows.map((r) => r.trade_date), ...AXIS },
    yAxis: [
      { type: "value", name: "亿", ...AXIS, splitLine: { lineStyle: { color: "#F3F4F6" } } },
      { type: "value", name: "价", scale: true, ...AXIS, splitLine: { show: false } },
    ],
    series: [
      {
        type: "bar",
        name: "主力净流入",
        data: rows.map((r) => ({ value: r.net_main === null ? null : r.net_main / 1e8, itemStyle: { color: barColor(r.net_main), opacity: r.reconciled ? 1 : 0.55 } })),
      },
      { type: "line", name: "收盘", yAxisIndex: 1, data: rows.map((r) => r.close), showSymbol: false, lineStyle: { color: "#111827", width: 1.5 } },
    ],
  };
}
