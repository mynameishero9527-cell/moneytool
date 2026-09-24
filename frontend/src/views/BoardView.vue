<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api/client";
import type { SectorStageRow, Stage } from "../api/types";
import { STAGE_LABEL, STAGE_ORDER, pct, yi, num, signClass } from "../charts/format";
import DataTable, { type Column } from "../components/DataTable.vue";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import MetaBadge from "../components/MetaBadge.vue";
import StageBadge from "../components/StageBadge.vue";
import { useTradeDateStore } from "../stores/tradeDate";

const store = useTradeDateStore();
const route = useRoute();
const router = useRouter();

const level = ref<"L1" | "L2" | "concept">("L1");
const stageFilter = ref<Stage | null>(null);

const q = useQuery({
  queryKey: computed(() => ["sectors", store.date, store.segment, level.value, stageFilter.value]),
  queryFn: () => api.sectors(store.scope, level.value, stageFilter.value),
});
const rows = computed(() => q.data.value?.data ?? []);
const isIntraday = computed(() => q.data.value?.meta.status === "intraday");

type Row = SectorStageRow & Record<string, unknown>;
const columns: Column<Row>[] = [
  { key: "stage", title: "阶段", width: 170, sortValue: (r) => STAGE_ORDER.indexOf(r.stage) },
  { key: "name", title: "板块", width: 150 },
  { key: "net", title: "主力净流入 亿", width: 110, num: true, sortValue: (r) => r.metrics.sector_net_main },
  { key: "share", title: "市场份额", width: 90, num: true, sortValue: (r) => r.metrics.market_share },
  { key: "ratio", title: "净占比", width: 80, num: true, sortValue: (r) => r.metrics.sector_main_ratio },
  { key: "mult", title: "对前 5 日倍数", width: 100, num: true, sortValue: (r) => r.metrics.main_multiple_prev5, hideBelow: 768 },
  { key: "slope", title: "5 日斜率", width: 90, num: true, sortValue: (r) => r.metrics.main_slope_5d, hideBelow: 1280 },
  { key: "chg", title: "涨跌", width: 80, num: true, sortValue: (r) => r.metrics.sector_pct_chg },
  { key: "ret20", title: "20 日", width: 80, num: true, sortValue: (r) => r.metrics.ret_20d, hideBelow: 768 },
  { key: "breadth", title: "上涨占比", width: 80, num: true, sortValue: (r) => r.metrics.breadth, hideBelow: 768 },
  { key: "turnover", title: "换手/20 日", width: 90, num: true, sortValue: (r) => r.metrics.turnover_vs_20d, hideBelow: 1280 },
  { key: "pattern", title: "形态", width: 100, hideBelow: 1280 },
  { key: "flags", title: "标记", width: 160, hideBelow: 768 },
];

const expanded = ref<string | null>(null);
function open(r: Row) {
  void router.push({ name: "sector", params: { id: r.sector_id }, query: route.query });
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">板块看板 <MetaBadge :meta="q.data.value?.meta" /></h1>
    <div class="toolbar">
      <button v-for="l in ['L1', 'L2', 'concept'] as const" :key="l" :class="{ active: level === l }" @click="level = l">
        {{ l === "L1" ? "一级行业" : l === "L2" ? "二级行业" : "概念" }}
      </button>
      <span class="muted">│</span>
      <button :class="{ active: stageFilter === null }" @click="stageFilter = null">全部阶段</button>
      <button v-for="s in STAGE_ORDER" :key="s" :class="{ active: stageFilter === s }" @click="stageFilter = s">{{ STAGE_LABEL[s] }}</button>
    </div>

    <EmptyState v-if="q.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="!rows.length" :reason="q.data.value?.meta.reason ?? (level === 'concept' ? '无概念板块结果：概念成分需夜间任务同步' : '无板块结果')" />
    <DataTable
      v-else
      :columns="columns"
      :rows="(rows as Row[])"
      :row-key="(r) => r.sector_id"
      clickable
      @row-click="(r) => (expanded = expanded === r.sector_id ? null : r.sector_id)"
    >
      <template #stage="{ row }">
        <StageBadge
          :stage="row.stage"
          :half="row.half"
          :suspected-to="row.suspected_to"
          :abnormal="row.abnormal_transition"
          :intraday="isIntraday"
          :days="row.days_in_stage"
        />
      </template>
      <template #name="{ row }">
        <a href="#" @click.stop.prevent="open(row)">{{ row.name }}</a><span class="code">{{ row.sector_id }}</span>
      </template>
      <template #net="{ row }"><span :class="signClass(row.metrics.sector_net_main)">{{ yi(row.metrics.sector_net_main) }}</span></template>
      <template #share="{ row }">{{ pct(row.metrics.market_share) }}</template>
      <template #ratio="{ row }"><span :class="signClass(row.metrics.sector_main_ratio)">{{ pct(row.metrics.sector_main_ratio, 2) }}</span></template>
      <template #mult="{ row }">{{ num(row.metrics.main_multiple_prev5, 1) }}</template>
      <template #slope="{ row }">{{ row.metrics.main_slope_5d == null ? "—" : yi(row.metrics.main_slope_5d, 3) }}</template>
      <template #chg="{ row }"><span :class="signClass(row.metrics.sector_pct_chg)">{{ pct(row.metrics.sector_pct_chg, 2, true) }}</span></template>
      <template #ret20="{ row }"><span :class="signClass(row.metrics.ret_20d)">{{ pct(row.metrics.ret_20d, 1, true) }}</span></template>
      <template #breadth="{ row }">{{ pct(row.metrics.breadth, 0) }}</template>
      <template #turnover="{ row }">{{ num(row.metrics.turnover_vs_20d, 2) }}</template>
      <template #pattern="{ row }">{{ row.pattern ?? "—" }}</template>
      <template #flags="{ row }">
        <span v-if="row.is_pulse" class="tag">脉冲</span>
        <span v-if="row.small_sample" class="tag">小样本</span>
        <span v-if="row.carried_over" class="tag">沿用</span>
        <span v-if="row.metrics.amount_tier" class="tag">{{ row.metrics.amount_tier }}</span>
      </template>
    </DataTable>

    <template v-if="expanded">
      <div class="section-title">证据：{{ rows.find((r) => r.sector_id === expanded)?.name }}</div>
      <EvidenceCard
        :evidence="rows.find((r) => r.sector_id === expanded)?.evidence"
        :footer="`阶段 ${STAGE_LABEL[rows.find((r) => r.sector_id === expanded)!.stage]} · 停留 ${rows.find((r) => r.sector_id === expanded)?.days_in_stage ?? '—'} 日 · 参数 ${q.data.value?.meta.param_version ?? ''}`"
      />
    </template>
  </div>
</template>
