import { defineStore } from "pinia";
import { computed, ref } from "vue";

/** 当前查看的交易日与分段。`date` 为空表示「最新」；设置了日期即回看模式。 */
export const useTradeDateStore = defineStore("tradeDate", () => {
  const date = ref<string | null>(null);
  const segment = ref<string | null>(null);

  const isReplay = computed(() => date.value !== null);
  const scope = computed(() => ({ trade_date: date.value, segment: segment.value }));

  function set(next: string | null, seg: string | null = null) {
    date.value = next;
    segment.value = seg;
  }
  function reset() {
    date.value = null;
    segment.value = null;
  }
  return { date, segment, isReplay, scope, set, reset };
});
