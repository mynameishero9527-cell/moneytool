<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { SectorMember } from "../api/types";
import { sectorHistoryOption } from "../charts/options";
import { STAGE_LABEL, pct, yi, num, signClass } from "../charts/format";
import DataTable, { type Column } from "../components/DataTable.vue";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import MetaBadge from "../components/MetaBadge.vue";
import StageBadge from "../components/StageBadge.vue";
import { useEcharts } from "../composables/useEcharts";
import { useTradeDateStore } from "../stores/tradeDate";

const props = defineProps<{ id: string }>();
const store = useTradeDateStore();

const detail = useQuery({
  queryKey: computed(() => ["sector", props.id, store.date, store.segment]),
  queryFn: () => api.sectorDetail(props.id, store.scope),
});
const history = useQuery({
  queryKey: computed(() => ["sector", props.id, "history", store.date]),
  queryFn: () => api.sectorHistory(props.id, 120, store.date),
});

const d = computed(() => detail.data.value?.data ?? null);
const isIntraday = computed(() => detail.data.value?.meta.status === "intraday");
const chartEl = ref<HTMLElement | null>(null);
const option = computed(() => (history.data.value?.data.length ? sectorHistoryOption(history.data.value.data) : null));
useEcharts(chartEl, option);

type MRow = SectorMember & Record<string, unknown>;
const memberCols: Column<MRow>[] = [
  { key: "name", title: "个股", width: 160 },
  { key: "net_main", title: "主力净流入 亿", width: 110, num: true },
  { key: "main_ratio", title: "净占比", width: 80, num: true },
  { key: "pct_chg", title: "涨跌", width: 80, num: true },
  { key: "amount", title: "成交 亿", width: 90, num: true, hideBelow: 768 },
  { key: "m5", title: "5 日均值 亿", width: 100, num: true, sortValue: (r) => r.metrics["main_mean_5d"] as number | null, hideBelow: 768 },
  { key: "inflow5", title: "5 日流入天数", width: 100, num: true, sortValue: (r) => r.metrics["inflow_days_5d"] as number | null, hideBelow: 1280 },
  { key: "ret20", title: "20 日", width: 80, num: true, sortValue: (r) => r.metrics["ret_20d"] as number | null, hideBelow: 768 },
  { key: "pos", title: "250 日位置", width: 90, num: true, sortValue: (r) => r.metrics["range_pos_250d"] as number | null, hideBelow: 1280 },
  { key: "flags", title: "标记", width: 180 },
];

const featureRows = computed(() => {
  const f = d.value?.features ?? {};
  return Object.entries(f)
    .filter(([, v]) => typeof v === "number")
    .map(([k, v]) => [k, v as number] as const);
});
const showFeatures = ref(false);
</script>

<template>
  <div class="page">
    <EmptyState v-if="detail.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="detail.isError.value" :reason="String(detail.error.value)" />
    <template v-else-if="d">
      <h1 class="page-title">
        {{ d.sector.name }}<span class="code">{{ d.sector.sector_id }}</span>
        <span class="tag">{{ d.sector.level }}</span>
        <router-link v-if="d.sector.parent_id" class="muted" :to="{ name: 'sector', params: { id: d.sector.parent_id }, query: $route.query }">↑ 所属一级</router-link>
        <MetaBadge :meta="detail.data.value?.meta" />
      </h1>

      <div v-if="d.stage" class="grid grid-4">
        <div class="card stat">
          <StageBadge
            :stage="d.stage.stage"
            :half="d.stage.half"
            :suspected-to="d.stage.suspected_to"
            :abnormal="d.stage.abnormal_transition"
            :intraday="isIntraday"
          />
          <span class="label">停留 {{ d.stage.days_in_stage ?? "—" }} 日<span v-if="d.stage.entered_from"> · 自 {{ STAGE_LABEL[d.stage.entered_from] }}</span><span v-if="d.stage.candidate && d.stage.candidate !== d.stage.stage"> · 候选 {{ STAGE_LABEL[d.stage.candidate] }}（防抖中）</span></span>
        </div>
        <div class="card stat">
          <span class="value" :class="signClass(d.stage.metrics.sector_net_main)">{{ yi(d.stage.metrics.sector_net_main) }} 亿</span>
          <span class="label">主力净流入 · 净占比 {{ pct(d.stage.metrics.sector_main_ratio, 2) }} · 对前 5 日 {{ num(d.stage.metrics.main_multiple_prev5, 1) }}×</span>
        </div>
        <div class="card stat">
          <span class="value">{{ pct(d.stage.metrics.market_share) }}</span>
          <span class="label">市场份额（一级正流入合计为分母）</span>
        </div>
        <div class="card stat">
          <span class="value" :class="signClass(d.stage.metrics.sector_pct_chg)">{{ pct(d.stage.metrics.sector_pct_chg, 2, true) }}</span>
          <span class="label">涨跌 · 20 日 {{ pct(d.stage.metrics.ret_20d, 1, true) }} · 上涨占比 {{ pct(d.stage.metrics.breadth, 0) }} · 涨停 {{ d.stage.metrics.limit_up_count ?? "—" }}</span>
        </div>
      </div>
      <EmptyState v-else :reason="detail.data.value?.meta.reason ?? '该日无该板块阶段结果'" />

      <div v-if="d.hints.length" class="card" style="margin-top: 12px">
        <div v-for="h in d.hints" :key="h.template_id" class="hint">{{ h.text }}</div>
      </div>

      <div class="section-title">近 120 日：主力净流入、5 日均值、市场份额；底色为阶段</div>
      <div ref="chartEl" class="chart card" />

      <div class="grid grid-2" style="margin-top: 12px">
        <div>
          <div class="section-title">判定证据</div>
          <EvidenceCard
            :evidence="d.stage?.evidence"
            :footer="d.stage ? `阶段 ${STAGE_LABEL[d.stage.stage]} · 停留 ${d.stage.days_in_stage ?? '—'} 日 · 参数 ${d.stage.param_version}` : ''"
          />
          <a href="#" class="muted" @click.prevent="showFeatures = !showFeatures">{{ showFeatures ? "收起" : "全部派生量" }}</a>
          <table v-if="showFeatures" class="dt" style="margin-top: 6px">
            <tbody>
              <tr v-for="[k, v] in featureRows" :key="k">
                <td style="font-family: ui-monospace, monospace">{{ k }}</td>
                <td class="num">{{ Math.abs(v) >= 1e6 ? yi(v) + " 亿" : num(v, 4) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="d.children.length">
          <div class="section-title">二级分布</div>
          <table class="dt">
            <tbody>
              <tr v-for="c in d.children" :key="c.sector_id" class="clickable" @click="$router.push({ name: 'sector', params: { id: c.sector_id }, query: $route.query })">
                <td style="width: 160px"><StageBadge :stage="c.stage" :days="c.days_in_stage" /></td>
                <td>{{ c.name }}</td>
                <td class="num" :class="signClass(c.metrics.sector_net_main)">{{ yi(c.metrics.sector_net_main) }}</td>
                <td class="num">{{ pct(c.metrics.market_share) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="d.indices.length">
          <div class="section-title">指数（描述性，不影响判定）</div>
          <div v-for="ix in d.indices" :key="ix.index_name" class="card" style="margin-bottom: 6px">
            <b>{{ ix.index_name }}</b>
            <span class="num" style="margin-left: 8px">{{ ix.window_ok && !ix.degraded ? ix.total : ix.degraded ? "降级" : "历史不足" }}</span>
          </div>
        </div>
      </div>

      <div class="section-title">成分个股（{{ d.members.length }}）</div>
      <DataTable
        :columns="memberCols"
        :rows="(d.members as MRow[])"
        :row-key="(r) => r.code"
        :default-sort="{ key: 'net_main', desc: true }"
        clickable
        @row-click="(r) => $router.push({ name: 'stock', params: { code: r.code }, query: $route.query })"
      >
        <template #name="{ row }">{{ row.name ?? "—" }}<span class="code">{{ row.code }}</span><span v-if="row.is_st" class="tag">ST</span></template>
        <template #net_main="{ row }"><span :class="signClass(row.net_main)">{{ yi(row.net_main) }}</span><span v-if="row.reconciled === false" class="tag unreconciled" title="未对账">·</span></template>
        <template #main_ratio="{ row }"><span :class="signClass(row.main_ratio)">{{ pct(row.main_ratio, 2) }}</span></template>
        <template #pct_chg="{ row }"><span :class="signClass(row.pct_chg)">{{ pct(row.pct_chg, 2, true) }}</span></template>
        <template #amount="{ row }">{{ yi(row.amount) }}</template>
        <template #m5="{ row }">{{ yi(row.metrics["main_mean_5d"] as number | null) }}</template>
        <template #inflow5="{ row }">{{ row.metrics["inflow_days_5d"] ?? "—" }}</template>
        <template #ret20="{ row }"><span :class="signClass(row.metrics['ret_20d'] as number | null)">{{ pct(row.metrics["ret_20d"] as number | null, 1, true) }}</span></template>
        <template #pos="{ row }">{{ pct(row.metrics["range_pos_250d"] as number | null, 0) }}</template>
        <template #flags="{ row }">
          <span v-if="row.metrics['is_limit_up']" class="tag">涨停</span>
          <span v-if="row.metrics['is_one_word']" class="tag">一字</span>
          <span v-if="row.metrics['is_consecutive_limit']" class="tag outline-danger">连板</span>
        </template>
      </DataTable>
    </template>
  </div>
</template>

<style scoped>
.chart {
  height: 320px;
  padding: 0;
}
.hint {
  padding: 2px 0;
}
</style>
