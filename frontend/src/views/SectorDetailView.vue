<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { Evidence, Role, SectorMember } from "../api/types";
import { sectorHistoryOption } from "../charts/options";
import { ROLE_LABEL, STAGE_LABEL, pct, yi, num, signClass } from "../charts/format";
import DataTable, { type Column } from "../components/DataTable.vue";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import HintBlock from "../components/HintBlock.vue";
import IndexGauge from "../components/IndexGauge.vue";
import MetaBadge from "../components/MetaBadge.vue";
import SectorHorizonPanel from "../components/SectorHorizonPanel.vue";
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

const qc = useQueryClient();
const watchlist = useQuery({ queryKey: ["watchlist", null], queryFn: () => api.watchlist(null) });
const inWatch = computed(() => (watchlist.data.value?.data ?? []).some((w) => w.code === props.id));
const toggleWatch = useMutation({
  mutationFn: () => (inWatch.value ? api.removeWatch(props.id) : api.addWatch(props.id)),
  onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
});

interface Attribution {
  summary: string;
  hype?: { score: number; items: Evidence[] };
  seasonal?: { score: number | null; note: string };
  external?: { score: number | null; note: string };
}
const ROLE_ORDER: Role[] = ["core", "follow", "avoid"];
const roleGroups = computed(() =>
  ROLE_ORDER.map((role) => ({ role, rows: (d.value?.roles ?? []).filter((r) => r.role === role) })).filter(
    (g) => g.rows.length,
  ),
);
const attribution = computed(() => {
  const a = d.value?.stage?.attribution;
  return a && typeof a === "object" && "summary" in a ? (a as Attribution) : null;
});
const showAttr = ref(false);
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
        <span class="spacer" />
        <button class="btn" :disabled="toggleWatch.isPending.value" @click="toggleWatch.mutate()">{{ inWatch ? "移出自选" : "加入自选" }}</button>
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

      <HintBlock :hints="d.hints" style="margin-top: 12px" />

      <div v-if="attribution" class="card attribution" style="margin-top: 12px">
        <b>资金归因</b>
        <span>{{ attribution.summary }}</span>
        <span v-if="attribution.hype" class="muted">概念炒作倾向 {{ attribution.hype.score }}/3</span>
        <span class="muted">季节性：{{ attribution.seasonal?.note ?? "—" }} · 外部：{{ attribution.external?.note ?? "—" }}</span>
        <a v-if="attribution.hype" href="#" class="muted" @click.prevent="showAttr = !showAttr">{{ showAttr ? "收起" : "依据" }}</a>
      </div>
      <EvidenceCard v-if="showAttr && attribution?.hype" :evidence="{ hype: attribution.hype.items }" />

      <div class="section-title">多周期资金与趋势倾向（当天 / 5 日 / 20 日 / 40 日 / 60 日 / 120 日 / 250 日）</div>
      <SectorHorizonPanel :id="id" />

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
          <IndexGauge v-for="ix in d.indices" :key="ix.index_name" :index="ix" style="margin-bottom: 6px" />
        </div>
      </div>

      <template v-if="roleGroups.length">
        <div class="section-title">成分角色</div>
        <div class="grid grid-3">
          <div v-for="g in roleGroups" :key="g.role" class="card">
            <div class="role-head"><span class="tag" :class="'role-' + g.role">{{ ROLE_LABEL[g.role] }}</span><span class="muted">{{ g.rows.length }} 只</span></div>
            <div
              v-for="r in g.rows.slice(0, 12)"
              :key="r.code"
              class="role-row clickable"
              @click="$router.push({ name: 'stock', params: { code: r.code }, query: $route.query })"
            >
              <span>{{ r.name ?? r.code }}<span class="code">{{ r.code }}</span></span>
              <span class="tags"><span v-for="t in r.tags ?? []" :key="t" class="tag">{{ t }}</span></span>
              <span class="num muted">{{ r.score ?? "" }}</span>
            </div>
            <div v-if="g.rows.length > 12" class="muted small">另 {{ g.rows.length - 12 }} 只见下方成分表</div>
          </div>
        </div>
      </template>

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
.spacer {
  flex: 1;
}
.btn {
  font: inherit;
  font-size: 12px;
  padding: 3px 10px;
  border: 1px solid var(--c-border);
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
}
.attribution {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.role-head {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}
.role-row {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 2px 0;
}
.role-row .tags {
  flex: 1;
  overflow: hidden;
  white-space: nowrap;
}
.small {
  font-size: 12px;
}
.role-core {
  background: #dbeafe;
  color: #1e3a8a;
}
.role-avoid {
  background: #e5e7eb;
  color: #374151;
}
</style>
