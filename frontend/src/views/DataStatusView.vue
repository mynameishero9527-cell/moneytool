<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { api } from "../api/client";
import { SEGMENT_LABEL, STATUS_LABEL } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import SyncPanel from "../components/SyncPanel.vue";
import { useTradeDateStore } from "../stores/tradeDate";

const store = useTradeDateStore();
const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 30_000 });
const quality = useQuery({
  queryKey: computed(() => ["quality", store.date]),
  queryFn: () => api.quality(store.date),
  enabled: computed(() => store.isReplay),
});
const s = computed(() => status.data.value?.data ?? null);
const qualityRows = computed(() =>
  store.isReplay ? ((quality.data.value?.data ?? []) as { source: string; endpoint: string; segment: string; status: string; reason: string | null; created_at: string }[]) : (s.value?.quality ?? []),
);
const ALL_SEGMENTS = ["auction", "0930_1030", "1030_1130", "1300_1400", "1400_1430", "1430_1500", "close"];
</script>

<template>
  <div class="page">
    <h1 class="page-title">数据状态 <span v-if="s" class="tag" :class="s.data_status">{{ STATUS_LABEL[s.data_status] ?? s.data_status }}</span></h1>
    <EmptyState v-if="status.isLoading.value" reason="加载中…" />
    <template v-else-if="s">
      <div class="grid grid-4">
        <div class="card stat"><span class="value">{{ s.latest_confirmed ?? "—" }}</span><span class="label">最近已确认交易日</span></div>
        <div class="card stat"><span class="value">{{ s.today }}</span><span class="label">今日 · {{ s.is_trading_day ? "交易日" : "非交易日" }}</span></div>
        <div class="card stat"><span class="value">{{ s.param_version ?? "—" }}</span><span class="label">参数版本 · 程序 v{{ s.version }}</span></div>
        <div class="card stat"><span class="value">{{ s.counts["confirmed_days"] ?? 0 }}</span><span class="label">已确认天数 · 证券 {{ s.counts["security"] }} · 板块 {{ s.counts["sector"] }}</span></div>
      </div>

      <div class="section-title">今日分段采集</div>
      <div class="segments">
        <span v-for="seg in ALL_SEGMENTS" :key="seg" class="tag" :class="{ intraday: s.segments_captured.includes(seg), missing: !s.segments_captured.includes(seg) }">
          {{ SEGMENT_LABEL[seg] ?? seg }}
        </span>
      </div>

      <div class="section-title">数据同步 <span class="muted small">已入库：日线 {{ s.counts["bar_days"] }} 个交易日 · 资金流 {{ s.counts["flow_days"] }} 个交易日</span></div>
      <SyncPanel />

      <div class="section-title">质量记录{{ store.isReplay ? `（${store.date}）` : "（今日）" }}</div>
      <EmptyState v-if="!qualityRows.length" reason="无质量事件记录" />
      <table v-else class="dt">
        <thead>
          <tr><th style="width: 100px">来源</th><th style="width: 160px">接口</th><th style="width: 110px">分段</th><th style="width: 110px">状态</th><th>原因</th><th style="width: 180px">时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in qualityRows" :key="i">
            <td>{{ r.source }}</td><td>{{ r.endpoint }}</td><td>{{ r.segment ? SEGMENT_LABEL[r.segment] ?? r.segment : "—" }}</td>
            <td><span class="tag" :class="r.status">{{ r.status }}</span></td>
            <td :title="r.reason ?? ''">{{ r.reason ?? "" }}</td><td class="muted">{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.segments {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.small {
  font-size: 12px;
  font-weight: 400;
}
</style>
