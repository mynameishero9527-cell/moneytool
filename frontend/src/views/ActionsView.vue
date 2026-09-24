<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api/client";
import type { ActionRow, ListType, TrackingRow } from "../api/types";
import { LIST_LABEL, POINT_LABEL, ROLE_LABEL, STAGE_LABEL, pct, yi, num, signClass } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import MetaBadge from "../components/MetaBadge.vue";
import StageBadge from "../components/StageBadge.vue";
import { useTradeDateStore } from "../stores/tradeDate";

type Tab = ListType | "tracking";
const TABS: Tab[] = ["buy", "lowbase", "point", "sell", "hold_watch", "buy_invalid", "tracking"];
const TAB_LABEL: Record<Tab, string> = { ...LIST_LABEL, tracking: "跟踪中" } as Record<Tab, string>;
const GATED: Tab[] = ["buy", "lowbase"];

const store = useTradeDateStore();
const route = useRoute();
const router = useRouter();

const tab = ref<Tab>(TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : "buy");
watch(tab, (t) => void router.replace({ query: { ...route.query, tab: t } }));
const basis = ref<"industry" | "concept">("industry");
const expanded = ref<string | null>(null);

const status = useQuery({ queryKey: ["status"], queryFn: api.status });
const list = useQuery({
  queryKey: computed(() => ["actions", tab.value, store.date, store.segment]),
  queryFn: () => api.actions(tab.value as ListType, store.scope),
  enabled: computed(() => tab.value !== "tracking"),
});
const tracking = useQuery({
  queryKey: ["tracking"],
  queryFn: () => api.tracking(true),
  enabled: computed(() => tab.value === "tracking"),
});

const isConcept = (r: ActionRow) => r.sector_level === "concept" || r.basis_level === "concept";
const rowsAll = computed(() => list.data.value?.data ?? []);
const counts = computed(() => ({
  industry: rowsAll.value.filter((r) => !isConcept(r)).length,
  concept: rowsAll.value.filter(isConcept).length,
}));
const groups = computed(() => {
  const rows = rowsAll.value.filter((r) => (basis.value === "concept" ? isConcept(r) : !isConcept(r)));
  const map = new Map<string, { sector_id: string; name: string; stage: ActionRow["sector_stage"]; days: number | null; half: string | null; rows: ActionRow[] }>();
  for (const r of rows) {
    const g = map.get(r.sector_id) ?? {
      sector_id: r.sector_id,
      name: r.sector_name ?? r.sector_id,
      stage: r.sector_stage,
      days: r.days_in_stage,
      half: r.sector_half,
      rows: [],
    };
    g.rows.push(r);
    map.set(r.sector_id, g);
  }
  return [...map.values()];
});
const rowKey = (r: ActionRow) => `${r.code}|${r.sector_id}|${r.point_type ?? ""}`;
const gateOn = computed(() => GATED.includes(tab.value) && (status.data.value?.data.risk_gate ?? false));

function goStock(code: string) {
  void router.push({ name: "stock", params: { code }, query: store.isReplay ? { date: store.date } : {} });
}
function goSector(id: string) {
  void router.push({ name: "sector", params: { id }, query: store.isReplay ? { date: store.date } : {} });
}
function trackActions(t: TrackingRow): string {
  return (t.current_actions ?? []).map((a) => LIST_LABEL[a] ?? a).join("、") || "—";
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">
      行动名单
      <MetaBadge :meta="tab === 'tracking' ? tracking.data.value?.meta : list.data.value?.meta" />
    </h1>
    <div class="toolbar">
      <button v-for="t in TABS" :key="t" :class="{ active: tab === t }" @click="tab = t">{{ TAB_LABEL[t] }}</button>
    </div>

    <div v-if="gateOn" class="gate-banner inline">市场风控开启：本名单暂停新增，已列出的仅供对照。</div>

    <template v-if="tab === 'tracking'">
      <EmptyState v-if="tracking.isLoading.value" reason="加载中…" />
      <EmptyState v-else-if="!tracking.data.value?.data.length" reason="暂无跟踪中的条目" />
      <table v-else class="dt">
        <thead>
          <tr>
            <th>个股</th>
            <th>来源</th>
            <th>依据板块</th>
            <th>入选日</th>
            <th class="num">入选价</th>
            <th class="num">至今</th>
            <th class="num">超额 vs 全 A 等权</th>
            <th class="num">超额 vs 板块</th>
            <th>当前角色</th>
            <th>当前动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in tracking.data.value.data" :key="`${t.list_type}|${t.code}|${t.sector_id}|${t.entered_date}`" class="clickable" @click="goStock(t.code)">
            <td>{{ t.name ?? "—" }}<span class="code">{{ t.code }}</span></td>
            <td>{{ LIST_LABEL[t.list_type] ?? t.list_type }}</td>
            <td>{{ t.sector_name ?? t.sector_id }}<span v-if="t.entered_stage" class="muted"> · 入选时{{ STAGE_LABEL[t.entered_stage] }}</span></td>
            <td>{{ t.entered_date }}</td>
            <td class="num">{{ num(t.entered_price) }}</td>
            <td class="num" :class="signClass(t.ret_since)">{{ pct(t.ret_since, 1, true) }}</td>
            <td class="num" :class="signClass(t.excess_vs_eqw)">{{ pct(t.excess_vs_eqw, 1, true) }}</td>
            <td class="num" :class="signClass(t.excess_vs_sector)">{{ pct(t.excess_vs_sector, 1, true) }}</td>
            <td>{{ t.current_role ? ROLE_LABEL[t.current_role] : "—" }}</td>
            <td>{{ trackActions(t) }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-else>
      <div class="toolbar">
        <button :class="{ active: basis === 'industry' }" @click="basis = 'industry'">按行业（{{ counts.industry }}）</button>
        <button :class="{ active: basis === 'concept' }" @click="basis = 'concept'">按概念（{{ counts.concept }}）</button>
        <span class="muted small">组内按板块阶段 → 资金留存排列，带「高风险」标签的排在组内最后；不设全市场名次。</span>
      </div>
      <EmptyState v-if="list.isLoading.value" reason="加载中…" />
      <EmptyState v-else-if="list.isError.value" :reason="String(list.error.value)" />
      <EmptyState v-else-if="!groups.length" :reason="list.data.value?.meta.reason ?? '该日本名单为空'" />
      <div v-for="g in groups" :key="g.sector_id" class="group">
        <div class="group-head clickable" @click="goSector(g.sector_id)">
          <StageBadge v-if="g.stage" :stage="g.stage" :half="g.half" :days="g.days" />
          <b>{{ g.name }}</b><span class="code">{{ g.sector_id }}</span>
          <span class="muted">{{ g.rows.length }} 只</span>
        </div>
        <table class="dt">
          <colgroup>
            <col style="width: 160px" />
            <col />
            <col style="width: 90px" />
            <col style="width: 70px" />
            <col style="width: 100px" />
            <col style="width: 80px" />
            <col style="width: 80px" />
            <col style="width: 60px" />
          </colgroup>
          <thead>
            <tr>
              <th>个股</th>
              <th>标签</th>
              <th class="num">结构分</th>
              <th class="num">风险</th>
              <th class="num">主力净流入 亿</th>
              <th class="num">涨跌</th>
              <th class="num">5 日留存 亿</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <template v-for="r in g.rows" :key="rowKey(r)">
              <tr class="clickable" @click="goStock(r.code)">
                <td>{{ r.name ?? "—" }}<span class="code">{{ r.code }}</span></td>
                <td>
                  <span v-if="r.point_type" class="tag">{{ POINT_LABEL[r.point_type] ?? r.point_type }}</span>
                  <span v-for="t in r.tags ?? []" :key="t" class="tag" :class="{ 'outline-danger': t.includes('风险') }">{{ t }}</span>
                </td>
                <td class="num">{{ r.score ?? "—" }}<span v-if="r.score_tier" class="muted"> {{ r.score_tier }}</span></td>
                <td class="num">{{ r.risk_total ?? "—" }}</td>
                <td class="num" :class="signClass(r.net_main)">{{ yi(r.net_main) }}</td>
                <td class="num" :class="signClass(r.pct_chg)">{{ pct(r.pct_chg, 2, true) }}</td>
                <td class="num" :class="signClass(r.retention_5d)">{{ yi(r.retention_5d) }}</td>
                <td><a href="#" @click.prevent.stop="expanded = expanded === rowKey(r) ? null : rowKey(r)">{{ expanded === rowKey(r) ? "收起" : "依据" }}</a></td>
              </tr>
              <tr v-if="expanded === rowKey(r)">
                <td colspan="8"><EvidenceCard :evidence="r.evidence" /></td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.group {
  margin-top: 12px;
}
.group-head {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
}
.small {
  font-size: 12px;
}
.inline {
  margin: 8px 0;
  border-radius: 4px;
}
</style>
