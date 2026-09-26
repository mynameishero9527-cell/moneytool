<script setup lang="ts">
import { computed } from "vue";
import { DIRECTION_LABEL, STRENGTH_LABEL } from "../charts/format";

const props = defineProps<{
  direction: string | null | undefined;
  score: number | null | undefined;
  strength?: string | null;
  compact?: boolean;
}>();

const cls = computed(() => (props.direction === "up" ? "up" : props.direction === "down" ? "down" : "neutral"));
const arrow = computed(() => (props.direction === "up" ? "↗" : props.direction === "down" ? "↘" : "→"));
</script>

<template>
  <span v-if="direction" class="trend" :class="cls" :title="`趋势倾向得分 ${score ?? '—'}（−100…100）`">
    {{ arrow }} {{ DIRECTION_LABEL[direction] }}<template v-if="!compact && strength && direction !== 'flat'">·{{ STRENGTH_LABEL[strength] }}</template>
    <b class="num">{{ score === null || score === undefined ? "—" : score.toFixed(0) }}</b>
  </span>
  <span v-else class="muted">—</span>
</template>

<style scoped>
.trend {
  display: inline-flex;
  gap: 4px;
  align-items: baseline;
  white-space: nowrap;
}
.trend b {
  font-weight: 600;
}
</style>
