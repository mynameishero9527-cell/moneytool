<script setup lang="ts">
import { computed } from "vue";
import type { IndexRow } from "../api/types";
import { INDEX_LABEL } from "../charts/format";

const props = defineProps<{ index: IndexRow }>();

const TIER_COLOR = ["var(--tier-1)", "var(--tier-2)", "var(--tier-3)", "var(--tier-4)", "var(--tier-5)"];
function color(v: number | null | undefined): string {
  if (v === null || v === undefined) return "var(--c-missing)";
  return TIER_COLOR[Math.max(0, Math.min(4, Math.floor(v / 20)))] ?? "var(--c-missing)";
}

const items = computed(() => props.index.components?.items ?? []);
const status = computed(() => {
  if (props.index.degraded) return "降级";
  if (!props.index.window_ok) return "部分分项缺失";
  return null;
});
</script>

<template>
  <div class="gauge card">
    <div class="head">
      <b>{{ INDEX_LABEL[index.index_name] ?? index.index_name }}</b>
      <span class="total" :style="{ color: color(index.total) }">{{ index.total ?? "—" }}</span>
      <span v-if="index.components?.tier" class="tag">{{ index.components.tier }}</span>
      <span v-if="status" class="tag missing">{{ status }}</span>
    </div>
    <div class="bar"><div class="fill" :style="{ width: (index.total ?? 0) + '%', background: color(index.total) }" /></div>
    <table>
      <tbody>
        <tr v-for="it in items" :key="it.key" :class="{ miss: it.value === null }">
          <td class="name">{{ it.name }}</td>
          <td class="mini">
            <div class="bar small"><div class="fill" :style="{ width: ((it.value ?? 0) / it.max) * 100 + '%', background: color(it.value === null ? null : (it.value / it.max) * 100) }" /></div>
          </td>
          <td class="num">{{ it.value ?? "—" }}<span class="muted">/{{ it.max }}</span></td>
          <td class="muted note">{{ it.note }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.gauge {
  font-size: 12px;
}
.head {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.total {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.bar {
  height: 6px;
  background: var(--c-border);
  border-radius: 3px;
  margin: 4px 0 6px;
  overflow: hidden;
}
.bar.small {
  height: 4px;
  margin: 0;
}
.fill {
  height: 100%;
}
table {
  width: 100%;
  border-collapse: collapse;
}
td {
  padding: 1px 4px;
  white-space: nowrap;
}
td.name {
  width: 110px;
}
td.mini {
  width: 80px;
}
td.num {
  text-align: right;
  width: 56px;
  font-variant-numeric: tabular-nums;
}
td.note {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 160px;
}
tr.miss td {
  color: var(--c-text-2);
}
</style>
