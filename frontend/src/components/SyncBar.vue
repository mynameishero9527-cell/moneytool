<script setup lang="ts">
import { computed } from "vue";
import type { SyncLane } from "../api/types";
import { inflight } from "../composables/useSync";

const props = defineProps<{ lane: SyncLane; slim?: boolean }>();
const pct = (n: number) => (props.lane.total ? Math.min(100, (n / props.lane.total) * 100) : 0);
const doneWidth = computed(() => pct(props.lane.done));
const flightWidth = computed(() => pct(inflight(props.lane)));
const tone = computed(() => {
  if (props.lane.total > 0 && props.lane.done >= props.lane.total) return "complete";
  return props.lane.state === "paused" ? "paused" : "active";
});
</script>

<template>
  <div
    class="sync-bar"
    :class="[tone, { slim }]"
    role="progressbar"
    :aria-valuenow="lane.percent"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-label="`${lane.label}进度 ${lane.percent}%`"
  >
    <span class="done" :style="{ width: doneWidth + '%' }" />
    <span class="flight" :style="{ width: flightWidth + '%' }" />
  </div>
</template>

<style scoped>
.sync-bar {
  display: flex;
  height: 10px;
  border-radius: 5px;
  background: #e5e7eb;
  overflow: hidden;
}
.sync-bar.slim {
  height: 6px;
  border-radius: 3px;
}
.done {
  background: var(--c-progress);
  transition: width 0.6s ease;
}
.flight {
  background: var(--c-progress-soft);
  transition: width 0.6s ease;
}
.active .flight {
  background-image: repeating-linear-gradient(135deg, var(--c-progress-soft) 0 6px, #dbeafe 6px 12px);
  background-size: 24px 24px;
  animation: slide 1s linear infinite;
}
.paused .done {
  background: var(--c-intraday);
}
.complete .done {
  background: var(--c-progress-done);
}
@keyframes slide {
  from {
    background-position: 0 0;
  }
  to {
    background-position: 24px 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .active .flight {
    animation: none;
  }
}
</style>
