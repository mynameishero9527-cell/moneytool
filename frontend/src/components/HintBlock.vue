<script setup lang="ts">
import type { HintRow } from "../api/types";
import { HOLD_LABEL, SECTOR_TIER_LABEL } from "../charts/format";

defineProps<{ hints: HintRow[] }>();

const TIER_CLASS: Record<string, string> = {
  focus: "tier-focus",
  intact: "tier-focus",
  watch: "tier-watch",
  review: "tier-watch",
  avoid: "tier-avoid",
  broken: "tier-avoid",
};
</script>

<template>
  <div v-if="hints.length" class="hints card">
    <div v-for="h in hints" :key="h.template_id" class="hint">
      <span class="tag" :class="TIER_CLASS[h.tier]">{{ SECTOR_TIER_LABEL[h.tier] ?? HOLD_LABEL[h.tier] ?? h.tier }}</span>
      <template v-if="h.links?.length">
        <span v-for="(s, i) in h.links" :key="i" class="sentence">{{ s.text }}</span>
      </template>
      <span v-else>{{ h.text }}</span>
    </div>
    <div class="muted small">提示由规则结果按模板生成，只描述数据状态，不构成投资建议。</div>
  </div>
</template>

<style scoped>
.hint {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  align-items: baseline;
  line-height: 1.7;
}
.small {
  font-size: 11px;
  margin-top: 4px;
}
.tier-focus {
  background: #dbeafe;
  color: #1e3a8a;
}
.tier-watch {
  background: #fef3c7;
  color: #92400e;
}
.tier-avoid {
  background: #e5e7eb;
  color: #374151;
}
</style>
