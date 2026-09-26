<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { BacktestStat } from "../api/types";
import { CONSISTENCY_LABEL, DIRECTION_LABEL, money, num, pct, rankHeat, signClass } from "../charts/format";
import { trendHistoryOption } from "../charts/options";
import { useEcharts } from "../composables/useEcharts";
import { useTradeDateStore } from "../stores/tradeDate";
import EmptyState from "./EmptyState.vue";
import FlowSignalFeed from "./FlowSignalFeed.vue";
import TrendBadge from "./TrendBadge.vue";

const props = defineProps<{ id: string }>();
const store = useTradeDateStore();

const q = useQuery({
  queryKey: computed(() => ["flow-sector", props.id, store.date, store.segment]),
  queryFn: () => api.flowSector(props.id, store.scope),
  refetchInterval: () => (store.isReplay ? false : 60_000),
});
const d = computed(() => q.data.value?.data);
const trend = computed(() => d.value?.trend ?? null);

const terms = computed(() => [
  { key: "short", label: "短期（5 日 + 资金加速度）", v: trend.value?.short_score ?? null },
  { key: "mid", label: "中期（20 / 40 日）", v: trend.value?.mid_score ?? null },
  { key: "long", label: "长期（60 / 120 / 250 日）", v: trend.value?.long_score ?? null },
]);

function barStyle(v: number | null) {
  if (v === null) return { width: "0" };
  const w = Math.min(Math.abs(v), 100) / 2;
  return v >= 0
    ? { left: "50%", width: `${w}%`, background: "var(--c-up)" }
    : { left: `${50 - w}%`, width: `${w}%`, background: "var(--c-down)" };
}

const bt = computed(() => {
  const b = trend.value?.backtest ?? {};
  return (["5d", "20d"] as const)
    .map((k) => ({ k, s: b[k] as BacktestStat | undefined }))
    .filter((x): x is { k: "5d" | "20d"; s: BacktestStat } => !!x.s);
});

const chartEl = ref<HTMLElement | null>(null);
const option = computed(() => (d.value && d.value.history.length > 1 ? trendHistoryOption(d.value.history) : null));
useEcharts(chartEl, option);
</script>

<template>
  <div class="hz">
    <EmptyState v-if="q.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="!d || !trend" reason="暂无多周期结果：收盘计算或盘中分段计算后生成" />
    <template v-else>
      <div class="grid grid-3">
        <div class="card">
          <div class="muted small">趋势倾向</div>
          <div class="headline">
            <TrendBadge :direction="trend.direction" :score="trend.score" :strength="trend.strength" />
          </div>
          <div class="muted small">{{ CONSISTENCY_LABEL[trend.consistency ?? "insufficient"] }}</div>
          <div v-for="t in terms" :key="t.key" class="term">
            <span class="small">{{ t.label }}</span>
            <span class="bar"><i :style="barStyle(t.v)" /><em /></span>
            <span class="num small" :class="signClass(t.v)">{{ t.v === null ? "—" : t.v.toFixed(0) }}</span>
          </div>
        </div>
        <div class="card">
          <div class="muted small">依据</div>
          <ul class="reasons">
            <li v-for="r in trend.reasons" :key="r">{{ r }}</li>
          </ul>
        </div>
        <div class="card">
          <div class="muted small">同级板块历史上处于「{{ DIRECTION_LABEL[trend.direction ?? "flat"] }}」时，其后表现</div>
          <table v-if="bt.length" class="dt mini">
            <thead>
              <tr><th>之后</th><th class="num">样本</th><th class="num">上涨占比</th><th class="num">平均涨跌</th><th class="num">跑赢同级</th></tr>
            </thead>
            <tbody>
              <tr v-for="x in bt" :key="x.k">
                <td>{{ x.k === "5d" ? "5 日" : "20 日" }}</td>
                <td class="num">{{ x.s.samples }}</td>
                <td class="num">{{ pct(x.s.up_ratio, 0) }}</td>
                <td class="num" :class="signClass(x.s.avg_ret)">{{ pct(x.s.avg_ret, 2, true) }}</td>
                <td class="num">{{ pct(x.s.beat_ratio, 0) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="muted small">历史样本不足</div>
          <div class="muted small note">{{ d.note }}</div>
        </div>
      </div>

      <table class="dt periods">
        <thead>
          <tr>
            <th>周期</th>
            <th class="num">主力净流入</th>
            <th class="num">占成交</th>
            <th class="num">资金同级分位</th>
            <th class="num">涨跌</th>
            <th class="num">涨跌同级分位</th>
            <th class="num">净流入天数</th>
            <th class="num">全市场同期净流入</th>
            <th class="num">全市场同期涨跌</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in d.horizons" :key="h.key">
            <td>{{ h.label }} <span class="muted small">{{ h.hint }}</span></td>
            <td class="num" :class="signClass(d.h[h.key]?.net_main)">{{ money(d.h[h.key]?.net_main) }}</td>
            <td class="num" :class="signClass(d.h[h.key]?.main_ratio)">{{ pct(d.h[h.key]?.main_ratio, 2, true) }}</td>
            <td class="num" :style="{ background: rankHeat(d.h[h.key]?.flow_rank_pct) }">{{ num(d.h[h.key]?.flow_rank_pct, 0) }}</td>
            <td class="num" :class="signClass(d.h[h.key]?.ret)">{{ pct(d.h[h.key]?.ret, 1, true) }}</td>
            <td class="num" :style="{ background: rankHeat(d.h[h.key]?.ret_rank_pct) }">{{ num(d.h[h.key]?.ret_rank_pct, 0) }}</td>
            <td class="num">{{ h.key === "1d" ? "—" : pct(d.h[h.key]?.inflow_days_ratio, 0) }}</td>
            <td class="num" :class="signClass(d.market[h.key]?.net_main)">{{ money(d.market[h.key]?.net_main) }}</td>
            <td class="num" :class="signClass(d.market[h.key]?.ret)">{{ pct(d.market[h.key]?.ret, 1, true) }}</td>
          </tr>
        </tbody>
      </table>

      <div class="grid grid-2" style="margin-top: 12px">
        <div>
          <div class="muted small">趋势倾向得分走势（收盘值）</div>
          <div v-if="option" ref="chartEl" class="chart card" />
          <EmptyState v-else reason="得分历史不足两日" />
        </div>
        <div>
          <div class="muted small">本板块资金动向</div>
          <FlowSignalFeed :sector-id="id" :days="60" :limit="12" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.small {
  font-size: 12px;
}
.headline {
  font-size: 18px;
  margin: 4px 0;
}
.term {
  display: grid;
  grid-template-columns: 150px 1fr 36px;
  gap: 8px;
  align-items: center;
  margin-top: 6px;
}
.bar {
  position: relative;
  height: 8px;
  background: #f3f4f6;
  border-radius: 4px;
  overflow: hidden;
}
.bar i {
  position: absolute;
  top: 0;
  bottom: 0;
}
.bar em {
  position: absolute;
  left: 50%;
  top: -2px;
  bottom: -2px;
  width: 1px;
  background: #9ca3af;
}
.reasons {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: var(--fs-table);
  line-height: 1.7;
}
.mini {
  margin-top: 6px;
}
.note {
  margin-top: 6px;
  line-height: 1.5;
}
.periods {
  margin-top: 12px;
}
.chart {
  height: 220px;
}
</style>
