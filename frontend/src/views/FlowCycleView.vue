<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api/client";
import type { FlowHorizonItem, HorizonCell, HorizonKey, TrendDirection } from "../api/types";
import { CONSISTENCY_LABEL, DIRECTION_LABEL, money, num, pct, rankHeat, signClass } from "../charts/format";
import DataTable, { type Column } from "../components/DataTable.vue";
import EmptyState from "../components/EmptyState.vue";
import FlowSignalFeed from "../components/FlowSignalFeed.vue";
import MetaBadge from "../components/MetaBadge.vue";
import TrendBadge from "../components/TrendBadge.vue";
import { useTradeDateStore } from "../stores/tradeDate";

const store = useTradeDateStore();
const route = useRoute();
const router = useRouter();

type Metric = "flow_rank_pct" | "main_ratio" | "net_main" | "ret";
const METRICS: { key: Metric; label: string }[] = [
  { key: "flow_rank_pct", label: "资金同级分位" },
  { key: "main_ratio", label: "主力占比" },
  { key: "net_main", label: "主力净流入" },
  { key: "ret", label: "涨跌幅" },
];

const level = ref<"L1" | "L2" | "concept">("L1");
const metric = ref<Metric>("flow_rank_pct");
const direction = ref<TrendDirection | null>(null);
const hideSmall = ref(true);

const q = useQuery({
  queryKey: computed(() => ["flow-horizons", store.date, store.segment, level.value]),
  queryFn: () => api.flowHorizons(store.scope, level.value),
  refetchInterval: () => (store.isReplay ? false : 60_000),
});
const data = computed(() => q.data.value?.data);
const horizons = computed(() => data.value?.horizons ?? []);
const rows = computed(() =>
  (data.value?.items ?? []).filter(
    (r) => (!direction.value || r.direction === direction.value) && (!hideSmall.value || !r.small_sample),
  ),
);
const counts = computed(() => {
  const c: Record<string, number> = { up: 0, flat: 0, down: 0 };
  for (const r of data.value?.items ?? []) if (r.direction) c[r.direction] = (c[r.direction] ?? 0) + 1;
  return c;
});

const bt = useQuery({
  queryKey: computed(() => ["flow-backtest", store.date]),
  queryFn: () => api.flowBacktest(store.date),
});
const btRows = computed(() => (bt.data.value?.data.items ?? []).filter((r) => r.level === level.value));

function cellValue(c: HorizonCell | undefined): number | null {
  return c ? (c[metric.value] ?? null) : null;
}
function cellText(c: HorizonCell | undefined): string {
  const v = cellValue(c);
  switch (metric.value) {
    case "flow_rank_pct":
      return num(v, 0);
    case "main_ratio":
      return pct(v, 2, true);
    case "net_main":
      return money(v);
    case "ret":
      return pct(v, 1, true);
  }
  return "—";
}
function cellHeat(c: HorizonCell | undefined): string {
  if (!c) return "transparent";
  return rankHeat(metric.value === "ret" ? c.ret_rank_pct : c.flow_rank_pct);
}
function cellTitle(c: HorizonCell | undefined, label: string): string {
  if (!c) return `${label}：历史不足`;
  return [
    `${label}`,
    `主力净流入 ${money(c.net_main)}（占成交 ${pct(c.main_ratio, 2, true)}）`,
    `资金同级分位 ${num(c.flow_rank_pct, 0)}`,
    `涨跌 ${pct(c.ret, 1, true)}（同级分位 ${num(c.ret_rank_pct, 0)}）`,
    c.inflow_days_ratio === null ? "" : `净流入天数占比 ${pct(c.inflow_days_ratio, 0)}`,
  ]
    .filter(Boolean)
    .join("\n");
}

type Row = FlowHorizonItem & Record<string, unknown>;
const columns = computed<Column<Row>[]>(() => [
  { key: "name", title: "板块", width: 150 },
  { key: "trend", title: "趋势倾向", width: 130, sortValue: (r) => r.score },
  { key: "consistency", title: "短中长期", width: 100, hideBelow: 768 },
  ...horizons.value.map((h) => ({
    key: `h_${h.key}`,
    title: `${h.label}`,
    width: 86,
    num: true,
    sortValue: (r: Row) => cellValue(r.h[h.key as HorizonKey]),
  })),
]);

function open(r: Row) {
  void router.push({ name: "sector", params: { id: r.sector_id }, query: route.query });
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">资金周期 <MetaBadge :meta="q.data.value?.meta" /></h1>

    <div v-if="data && Object.keys(data.market).length" class="market card">
      <span class="muted">全市场主力净流入 / 等权涨跌</span>
      <span v-for="h in horizons" :key="h.key" class="mk">
        <span class="muted">{{ h.label }}</span>
        <b :class="signClass(data.market[h.key]?.net_main)">{{ money(data.market[h.key]?.net_main) }}</b>
        <span :class="signClass(data.market[h.key]?.ret)">{{ pct(data.market[h.key]?.ret, 1, true) }}</span>
      </span>
    </div>

    <div class="toolbar">
      <button v-for="l in ['L1', 'L2', 'concept'] as const" :key="l" :class="{ active: level === l }" @click="level = l">
        {{ l === "L1" ? "一级行业" : l === "L2" ? "二级行业" : "概念" }}
      </button>
      <span class="muted">│ 显示</span>
      <button v-for="m in METRICS" :key="m.key" :class="{ active: metric === m.key }" @click="metric = m.key">{{ m.label }}</button>
      <span class="muted">│</span>
      <button :class="{ active: direction === null }" @click="direction = null">全部</button>
      <button v-for="dir in ['up', 'flat', 'down'] as const" :key="dir" :class="{ active: direction === dir }" @click="direction = dir">
        {{ DIRECTION_LABEL[dir] }} {{ counts[dir] ?? 0 }}
      </button>
      <label class="muted"><input v-model="hideSmall" type="checkbox" /> 隐藏小样本</label>
    </div>

    <div class="layout">
      <div class="main">
        <EmptyState v-if="q.isLoading.value" reason="加载中…" />
        <EmptyState
          v-else-if="!rows.length"
          :reason="q.data.value?.meta.reason ?? (level === 'concept' ? '无概念板块结果：概念成分需夜间任务同步' : '暂无多周期结果：收盘计算后生成')"
        />
        <DataTable
          v-else
          :columns="columns"
          :rows="(rows as Row[])"
          :row-key="(r) => r.sector_id"
          :default-sort="{ key: 'trend', desc: true }"
          clickable
          @row-click="open"
        >
          <template #name="{ row }">
            <a href="#" @click.stop.prevent="open(row)">{{ row.name }}</a>
            <span v-if="row.small_sample" class="tag">小样本</span>
          </template>
          <template #trend="{ row }">
            <TrendBadge :direction="row.direction" :score="row.score" :strength="row.strength" />
          </template>
          <template #consistency="{ row }">
            <span class="muted">{{ CONSISTENCY_LABEL[row.consistency ?? "insufficient"] }}</span>
          </template>
          <template v-for="h in horizons" :key="h.key" #[`h_${h.key}`]="{ row }">
            <span
              class="cell"
              :class="metric === 'flow_rank_pct' ? '' : signClass(cellValue(row.h[h.key]))"
              :style="{ background: cellHeat(row.h[h.key]) }"
              :title="cellTitle(row.h[h.key], h.label)"
              >{{ cellText(row.h[h.key]) }}</span
            >
          </template>
        </DataTable>
        <div class="muted footnote">
          底色为同级板块分位：越红表示该周期主力占比（显示涨跌幅时为涨跌幅）在同级中越靠前，越绿越靠后。
          趋势倾向 = 短期 50% + 中期 30% + 长期 20%，每个周期按资金分位 50%、涨跌分位 30%、净流入天数 20% 合成；
          ≥ 20 为上升倾向，≤ −20 为下降倾向。{{ data?.note }}
        </div>

        <div class="section-title">趋势倾向历史统计（{{ level === "L1" ? "一级行业" : level === "L2" ? "二级行业" : "概念" }}）</div>
        <EmptyState v-if="!btRows.length" reason="历史样本不足" />
        <table v-else class="dt">
          <thead>
            <tr>
              <th>倾向</th><th class="num">之后</th><th class="num">样本</th><th class="num">上涨占比</th>
              <th class="num">平均涨跌</th><th class="num">相对同级超额</th><th class="num">跑赢同级占比</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in btRows" :key="`${r.direction}-${r.fwd_days}`">
              <td :class="r.direction === 'up' ? 'up' : r.direction === 'down' ? 'down' : 'neutral'">{{ DIRECTION_LABEL[r.direction] }}</td>
              <td class="num">{{ r.fwd_days }} 日</td>
              <td class="num">{{ r.samples }}</td>
              <td class="num">{{ pct(r.up_ratio, 0) }}</td>
              <td class="num" :class="signClass(r.avg_ret)">{{ pct(r.avg_ret, 2, true) }}</td>
              <td class="num" :class="signClass(r.avg_excess)">{{ pct(r.avg_excess, 2, true) }}</td>
              <td class="num">{{ pct(r.beat_ratio, 0) }}</td>
            </tr>
          </tbody>
        </table>
        <div class="muted footnote">
          用同一算法回算近一年每个交易日的倾向，与其后 5 / 20 个交易日板块实际涨跌对照；只用前瞻窗口已走完的样本，相邻日样本重叠。
        </div>
      </div>
      <aside class="side">
        <div class="section-title" style="margin-top: 0">资金动向</div>
        <FlowSignalFeed :limit="60" :days="5" filters />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.market {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  align-items: baseline;
  margin-bottom: 10px;
  font-size: var(--fs-table);
}
.mk {
  display: inline-flex;
  gap: 6px;
  align-items: baseline;
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 16px;
}
@media (max-width: 1280px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
.cell {
  display: block;
  margin: -4px -6px;
  padding: 4px 6px;
  border-radius: 3px;
  font-variant-numeric: tabular-nums;
}
.footnote {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
}
.side :deep(.toolbar) {
  flex-wrap: wrap;
}
</style>
