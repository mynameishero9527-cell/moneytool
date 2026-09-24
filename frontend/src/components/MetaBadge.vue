<script setup lang="ts">
import type { Meta } from "../api/types";
import { SEGMENT_LABEL, STATUS_LABEL } from "../charts/format";

defineProps<{ meta: Meta | null | undefined }>();
</script>

<template>
  <span v-if="meta" class="meta">
    <span class="tag" :class="meta.status">{{ STATUS_LABEL[meta.status] ?? meta.status }}</span>
    <span v-if="meta.trade_date" class="muted">{{ meta.trade_date }}</span>
    <span v-if="meta.segment && meta.segment !== 'close'" class="muted">{{ SEGMENT_LABEL[meta.segment] ?? meta.segment }}</span>
    <span v-if="meta.param_version" class="muted">参数 {{ meta.param_version }}</span>
    <span v-if="meta.reason" class="muted">· {{ meta.reason }}</span>
  </span>
</template>

<style scoped>
.meta {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
  font-weight: 400;
}
</style>
