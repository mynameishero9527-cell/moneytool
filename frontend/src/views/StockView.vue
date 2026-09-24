<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import { stockHistoryOption } from "../charts/options";
import { pct, yi, num, signClass } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import MetaBadge from "../components/MetaBadge.vue";
import StageBadge from "../components/StageBadge.vue";
import { useEcharts } from "../composables/useEcharts";
import { useTradeDateStore } from "../stores/tradeDate";

const props = defineProps<{ code: string }>();
const store = useTradeDateStore();

const detail = useQuery({
  queryKey: computed(() => ["stock", props.code, store.date]),
  queryFn: () => api.stockDetail(props.code, store.scope),
});
const history = useQuery({
  queryKey: computed(() => ["stock", props.code, "history", store.date]),
  queryFn: () => api.stockHistory(props.code, 120, store.date),
});
const d = computed(() => detail.data.value?.data ?? null);
const chartEl = ref<HTMLElement | null>(null);
const option = computed(() => (history.data.value?.data.length ? stockHistoryOption(history.data.value.data) : null));
useEcharts(chartEl, option);

const HOLD_LABEL: Record<string, string> = { intact: "结构完好", review: "需复核", broken: "结构破坏" };
const keyFeatures = [
  ["main_ratio", "净占比", "pct"],
  ["main_mean_5d", "5 日均值", "yi"],
  ["inflow_days_5d", "5 日流入天数", "int"],
  ["retention_5d", "5 日留存", "pct"],
  ["ret_5d", "5 日涨跌", "pct"],
  ["ret_20d", "20 日涨跌", "pct"],
  ["range_pos_250d", "250 日位置", "pct"],
  ["turnover_vs_20d", "换手/20 日", "num"],
  ["super_share", "超大单占比", "pct"],
  ["drawdown_20d", "20 日回撤", "pct"],
] as const;
function fmt(kind: string, v: number | boolean | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (kind === "pct") return pct(v, 1, true);
  if (kind === "yi") return yi(v);
  if (kind === "int") return String(v);
  return num(v, 2);
}
</script>

<template>
  <div class="page">
    <EmptyState v-if="detail.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="detail.isError.value" :reason="String(detail.error.value)" />
    <template v-else-if="d">
      <h1 class="page-title">
        {{ d.security.name }}<span class="code">{{ d.security.code }}</span>
        <span v-if="d.security.board" class="tag">{{ d.security.board }}</span>
        <span v-if="d.security.is_st" class="tag outline-danger">ST</span>
        <MetaBadge :meta="detail.data.value?.meta" />
      </h1>

      <div class="grid grid-4">
        <div class="card stat">
          <span class="value" :class="signClass(d.bar?.pct_chg as number | null)">{{ num(d.bar?.close as number | null) }} <small>{{ pct(d.bar?.pct_chg as number | null, 2, true) }}</small></span>
          <span class="label">收盘 · 成交 {{ yi(d.bar?.amount as number | null) }} 亿 · 换手 {{ pct(d.bar?.turnover as number | null, 2) }}</span>
        </div>
        <div class="card stat">
          <span class="value" :class="signClass(d.flow?.net_main as number | null)">{{ yi(d.flow?.net_main as number | null) }} 亿</span>
          <span class="label">主力净流入 · 净占比 {{ pct(d.flow?.main_ratio as number | null, 2) }}<span v-if="d.flow && d.flow.reconciled === false" class="tag unreconciled" style="margin-left: 6px">未对账</span><span v-if="!d.flow" class="tag missing" style="margin-left: 6px">缺失</span></span>
        </div>
        <div class="card stat">
          <span class="value">{{ yi(d.flow?.net_super as number | null) }} / {{ yi(d.flow?.net_large as number | null) }}</span>
          <span class="label">超大单 / 大单 净流入 亿</span>
        </div>
        <div class="card stat">
          <span class="value">{{ d.hold_eval ? HOLD_LABEL[d.hold_eval.eval] ?? d.hold_eval.eval : "—" }}</span>
          <span class="label">持有结构评估（无结果时为 —，不代表任何结论）</span>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top: 12px">
        <div class="card">
          <div class="section-title" style="margin-top: 0">所属板块</div>
          <table class="dt">
            <tbody>
              <tr v-for="s in d.sectors" :key="s.sector_id" class="clickable" @click="$router.push({ name: 'sector', params: { id: s.sector_id }, query: $route.query })">
                <td style="width: 60px"><span class="tag">{{ s.level }}</span></td>
                <td>{{ s.name }}<span class="code">{{ s.sector_id }}</span></td>
                <td style="width: 160px"><StageBadge v-if="s.stage" :stage="s.stage" :days="s.days_in_stage" /><span v-else class="muted">无阶段</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="card">
          <div class="section-title" style="margin-top: 0">派生量</div>
          <table class="dt">
            <tbody>
              <tr v-for="[k, label, kind] in keyFeatures" :key="k">
                <td>{{ label }}</td>
                <td class="num">{{ fmt(kind, d.features?.[k]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="d.hints.length" class="card" style="margin-top: 12px">
        <div v-for="h in d.hints" :key="h.template_id">{{ h.text }}</div>
      </div>

      <div class="section-title">近 120 日：收盘价与主力净流入（未对账日柱体淡显；缺失日留空）</div>
      <div ref="chartEl" class="chart card" />

      <div v-if="d.marks.length" class="section-title">我的标记</div>
      <table v-if="d.marks.length" class="dt">
        <tbody>
          <tr v-for="m in d.marks" :key="m.marked_at">
            <td style="width: 180px" class="muted">{{ m.marked_at }}</td>
            <td style="width: 80px">{{ m.mark }}</td>
            <td>{{ m.note ?? "" }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.chart {
  height: 320px;
  padding: 0;
}
</style>
