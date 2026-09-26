<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { formatDuration, useSync } from "../composables/useSync";
import SyncBar from "./SyncBar.vue";

const { sync } = useSync();
const route = useRoute();
const visible = computed(() => {
  const s = sync.value;
  return !!s && s.live && !s.complete && route.name !== "status";
});
const lanes = computed(() => (sync.value ? [sync.value.lanes.bars, sync.value.lanes.flow] : []));
const paused = computed(() => lanes.value.find((l) => l.state === "paused" && l.done < l.total));
const summary = computed(() => {
  const s = sync.value;
  if (!s) return "";
  if (s.stage === "catchup") return `计算历史结果 ${s.catchup.done}/${s.catchup.total} 天`;
  if (s.usable) return "近期数据已齐，页面可正常使用；更早的日线在后台继续补";
  if (s.stage === "reference" || s.stage === "starting") return s.stage_label;
  return s.eta_seconds != null ? `预计剩余 ${formatDuration(s.eta_seconds)}` : "剩余时间估算中";
});
</script>

<template>
  <router-link v-if="visible && sync" class="sync-banner" :class="{ usable: sync.usable }" :to="{ name: 'status' }" title="查看同步详情">
    <span class="title">{{ sync.usable ? "已可使用" : "数据同步中" }}</span>
    <span v-for="lane in lanes" :key="lane.label" class="lane">
      <span class="name">{{ lane.label }}</span>
      <SyncBar :lane="lane" slim class="bar" />
      <span class="pct">{{ lane.percent.toFixed(1) }}%</span>
    </span>
    <span class="muted">{{ summary }}</span>
    <span v-if="paused" class="warn">{{ paused.label }}暂停中</span>
    <span class="more">详情 ›</span>
  </router-link>
</template>

<style scoped>
.sync-banner {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 5px 16px;
  font-size: 12px;
  background: #eff6ff;
  border-bottom: 1px solid #bfdbfe;
  color: var(--c-text);
  text-decoration: none;
}
.sync-banner:hover {
  background: #dbeafe;
}
.sync-banner.usable {
  background: #f8fafc;
  border-bottom-color: var(--c-border);
}
.title {
  font-weight: 600;
  color: #1e3a8a;
}
.lane {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.bar {
  width: 120px;
}
.pct {
  font-variant-numeric: tabular-nums;
  min-width: 42px;
}
.warn {
  color: #b45309;
}
.more {
  margin-left: auto;
  color: #1e3a8a;
}
</style>
