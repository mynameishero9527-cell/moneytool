<script setup lang="ts">
defineProps<{ reason: string }>();
</script>

<template>
  <div class="empty card muted">{{ reason }}</div>
</template>

<style scoped>
.empty {
  padding: 24px;
  text-align: center;
}
</style>
