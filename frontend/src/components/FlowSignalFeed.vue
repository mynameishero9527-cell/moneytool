<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "../api/client";
import type { SignalKind } from "../api/types";
import { SEGMENT_LABEL } from "../charts/format";
import { useTradeDateStore } from "../stores/tradeDate";
import EmptyState from "./EmptyState.vue";

const props = withDefaults(
  defineProps<{ limit?: number; days?: number; sectorId?: string | null; filters?: boolean }>(),
  { limit: 30, days: 5, sectorId: null, filters: false },
);

const store = useTradeDateStore();
const route = useRoute();
const kind = ref<SignalKind | null>(null);

const q = useQuery({
  queryKey: computed(() => ["flow-signals", props.days, props.limit, props.sectorId, kind.value]),
  queryFn: () => api.flowSignals({ days: props.days, limit: props.limit, sector_id: props.sectorId, kind: kind.value }),
  refetchInterval: () => (store.isReplay ? false : 60_000),
});
const items = computed(() => q.data.value?.data.items ?? []);
const kinds = computed(() => Object.entries(q.data.value?.data.kinds ?? {}) as [SignalKind, string][]);

function tone(k: string): string {
  return k.endsWith("_in") || k === "turn_up" ? "up" : "down";
}
</script>

<template>
  <div class="feed">
    <div v-if="filters" class="toolbar">
      <button :class="{ active: kind === null }" @click="kind = null">全部</button>
      <button v-for="[k, zh] in kinds" :key="k" :class="{ active: kind === k }" @click="kind = k">{{ zh }}</button>
    </div>
    <EmptyState v-if="q.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="!items.length" reason="近期没有资金动向信号：信号在收盘计算、盘中每段计算后生成" />
    <ul v-else class="list">
      <li v-for="s in items" :key="`${s.trade_date}-${s.sector_id}-${s.kind}`">
        <span class="when muted">{{ s.trade_date.slice(5) }}<template v-if="s.segment !== 'close'"> · {{ SEGMENT_LABEL[s.segment] ?? s.segment }}</template></span>
        <span class="tag kind" :class="tone(s.kind)">{{ s.kind_zh }}</span>
        <router-link v-if="!sectorId" :to="{ name: 'sector', params: { id: s.sector_id }, query: route.query }" class="text">{{ s.text }}</router-link>
        <span v-else class="text">{{ s.text }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.list li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 0;
  border-bottom: 1px solid var(--c-border);
  font-size: var(--fs-table);
}
.when {
  flex: 0 0 auto;
  min-width: 42px;
  white-space: nowrap;
}
.kind {
  flex: 0 0 auto;
}
.kind.up {
  color: var(--c-up);
  border-color: var(--c-up);
}
.kind.down {
  color: var(--c-down);
  border-color: var(--c-down);
}
.text {
  color: var(--c-text);
}
</style>
