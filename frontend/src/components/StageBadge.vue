<script setup lang="ts">
import { computed } from "vue";
import type { Stage } from "../api/types";
import { STAGE_COLOR, STAGE_LABEL } from "../charts/format";

const props = defineProps<{
  stage: Stage;
  half?: string | null;
  suspectedTo?: Stage | null;
  abnormal?: boolean;
  intraday?: boolean;
  days?: number | null;
}>();

const color = computed(() => STAGE_COLOR[props.stage]);
const label = computed(() => {
  const base = STAGE_LABEL[props.stage];
  if (props.stage === "spread" && props.half) return `${base}·${props.half === "first" ? "前半" : "后半"}`;
  return base;
});
</script>

<template>
  <span class="badge" :style="{ '--sc': color }">
    <span class="main">{{ label }}<span v-if="days" class="days">{{ days }}d</span></span>
    <span v-if="suspectedTo" class="suffix">疑似→{{ STAGE_LABEL[suspectedTo] }}</span>
    <span v-if="abnormal" class="suffix warn">异常迁移</span>
    <span v-if="intraday" class="suffix intraday">盘中</span>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.main {
  border-radius: 4px;
  padding: 0 6px;
  line-height: 18px;
  font-size: 12px;
  background: color-mix(in srgb, var(--sc) 18%, transparent);
  color: color-mix(in srgb, var(--sc) 70%, #000);
}
.days {
  margin-left: 4px;
  opacity: 0.75;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.suffix {
  font-size: 11px;
  color: var(--c-text-2);
  border: 1px solid var(--c-border);
  border-radius: 3px;
  padding: 0 4px;
  line-height: 16px;
}
.suffix.warn {
  border-color: var(--c-up);
  color: var(--c-up);
}
.suffix.intraday {
  border-color: var(--c-intraday);
  color: var(--c-intraday);
}
</style>
