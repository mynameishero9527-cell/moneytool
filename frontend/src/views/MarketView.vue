<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { Stage } from "../api/types";
import { marketHistoryOption } from "../charts/options";
import { REGIME_LABEL, STAGE_COLOR, STAGE_LABEL, STAGE_ORDER, pct, yi } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import FlowSignalFeed from "../components/FlowSignalFeed.vue";
import MetaBadge from "../components/MetaBadge.vue";
import { useEcharts } from "../composables/useEcharts";
import { useTradeDateStore } from "../stores/tradeDate";

const store = useTradeDateStore();
const overview = useQuery({
  queryKey: computed(() => ["market", "overview", store.date, store.segment]),
  queryFn: () => api.marketOverview(store.scope),
});
const history = useQuery({
  queryKey: computed(() => ["market", "history", store.date]),
  queryFn: () => api.marketHistory(120, store.date),
});

const data = computed(() => overview.data.value?.data ?? null);
const chartEl = ref<HTMLElement | null>(null);
const option = computed(() => (history.data.value?.data.length ? marketHistoryOption(history.data.value.data) : null));
useEcharts(chartEl, option);

const l1Counts = computed(() => data.value?.stage_counts["L1"] ?? null);
const total = computed(() => (l1Counts.value ? Object.values(l1Counts.value).reduce((a, b) => a + b, 0) : 0));
const mainline = computed(() => {
  const m = data.value?.mainline;
  return Array.isArray(m) ? (m as { sector_id: string; name?: string; stage?: Stage; market_share?: number }[]) : [];
});
const showGate = ref(false);
const pressureLabel = (v: number | null) => (v === null ? "历史不足" : v >= 80 ? "过热" : v >= 60 ? "亢奋" : v >= 40 ? "中性" : v >= 20 ? "低迷" : "冰点");
</script>

<template>
  <div class="page">
    <h1 class="page-title">市场总览 <MetaBadge :meta="overview.data.value?.meta" /></h1>

    <EmptyState v-if="overview.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="!data" :reason="overview.data.value?.meta.reason ?? '该日无市场层结果'" />
    <template v-else>
      <div class="grid grid-4">
        <div class="card stat">
          <span class="value" :class="data.eqw_ret && data.eqw_ret > 0 ? 'up' : 'down'">{{ pct(data.eqw_ret, 2, true) }}</span>
          <span class="label">全市场等权涨跌（5 日 {{ pct(data.eqw_ret_5d, 2, true) }}）</span>
        </div>
        <div class="card stat">
          <span class="value" :class="data.net_main_all && data.net_main_all > 0 ? 'up' : 'down'">{{ yi(data.net_main_all) }} 亿</span>
          <span class="label">全市场主力净流入 · 成交 {{ yi(data.amount_all, 0) }} 亿</span>
        </div>
        <div class="card stat">
          <span class="value">{{ pct(data.breadth_all, 1) }}</span>
          <span class="label">上涨家数占比 · 涨停 {{ data.limit_up_count ?? "—" }}</span>
        </div>
        <div class="card stat">
          <span class="value">{{ data.market_pressure ?? "—" }} <small class="muted">{{ pressureLabel(data.market_pressure) }}</small></span>
          <span class="label">市场情绪压力指数（0–100，仅描述）</span>
        </div>
      </div>

      <div class="grid grid-3" style="margin-top: 12px">
        <div class="card">
          <div class="section-title" style="margin-top: 0">主线 · {{ REGIME_LABEL[data.regime] }}</div>
          <div v-if="!mainline.length" class="muted">无主线：当日无满足条件的一级行业</div>
          <div v-for="m in mainline" :key="m.sector_id" class="line">
            <router-link :to="{ name: 'sector', params: { id: m.sector_id }, query: $route.query }">{{ m.name ?? m.sector_id }}</router-link>
            <span v-if="m.stage" class="muted">{{ STAGE_LABEL[m.stage] }}</span>
            <span v-if="m.market_share != null" class="num muted">{{ pct(m.market_share) }}</span>
          </div>
        </div>
        <div class="card">
          <div class="section-title" style="margin-top: 0">一级行业阶段分布（{{ total }}）</div>
          <div v-if="l1Counts" class="dist">
            <div v-for="s in STAGE_ORDER" :key="s" class="dist-row">
              <span class="dist-label">{{ STAGE_LABEL[s] }}</span>
              <span class="bar"><span :style="{ width: total ? (l1Counts[s] / total) * 100 + '%' : '0', background: STAGE_COLOR[s] }" /></span>
              <span class="num">{{ l1Counts[s] }}</span>
            </div>
          </div>
          <div v-else class="muted">无板块阶段结果</div>
        </div>
        <div class="card">
          <div class="section-title" style="margin-top: 0">
            风控开关
            <span class="tag" :class="data.risk_gate ? 'outline-danger' : ''">{{ data.risk_gate ? "开启" : "关闭" }}</span>
          </div>
          <ul v-if="data.risk_gate_reasons?.length" class="reasons">
            <li v-for="r in data.risk_gate_reasons" :key="r">{{ r }}</li>
          </ul>
          <div v-else class="muted">未触发</div>
          <a href="#" class="muted" @click.prevent="showGate = !showGate">{{ showGate ? "收起证据" : "查看证据" }}</a>
          <EvidenceCard v-if="showGate" :evidence="(data.evidence as never)" :footer="`参数 ${data.param_version}`" />
        </div>
      </div>

      <div class="section-title">
        资金动向 <router-link class="muted more" :to="{ name: 'flow', query: $route.query }">资金周期 →</router-link>
      </div>
      <div class="card"><FlowSignalFeed :limit="10" :days="3" /></div>

      <div class="section-title">近 120 日：全市场主力净流入、等权累计收益、情绪压力（黄色区间为风控开启）</div>
      <div ref="chartEl" class="chart card" />
    </template>
  </div>
</template>

<style scoped>
.chart {
  height: 320px;
  padding: 0;
}
.line {
  display: flex;
  gap: 10px;
  padding: 2px 0;
}
.dist-row {
  display: grid;
  grid-template-columns: 72px 1fr 32px;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
}
.bar {
  height: 10px;
  background: #fff;
  border: 1px solid var(--c-border);
  border-radius: 3px;
  overflow: hidden;
  display: block;
}
.bar span {
  display: block;
  height: 100%;
}
.reasons {
  margin: 4px 0;
  padding-left: 18px;
}
</style>
