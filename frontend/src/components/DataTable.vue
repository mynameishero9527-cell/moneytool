<script setup lang="ts" generic="T extends Record<string, unknown>">
import { computed, ref } from "vue";

export interface Column<Row> {
  key: string;
  title: string;
  width?: number;
  num?: boolean;
  /** 排序取值；缺省取 row[key] */
  sortValue?: (row: Row) => number | string | null | undefined;
  /** 小屏隐藏 */
  hideBelow?: number;
}

const props = defineProps<{
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  defaultSort?: { key: string; desc: boolean };
  clickable?: boolean;
}>();
const emit = defineEmits<{ rowClick: [row: T] }>();

const sortKey = ref<string | null>(props.defaultSort?.key ?? null);
const sortDesc = ref(props.defaultSort?.desc ?? true);

function toggleSort(key: string) {
  if (sortKey.value === key) sortDesc.value = !sortDesc.value;
  else {
    sortKey.value = key;
    sortDesc.value = true;
  }
}

const sorted = computed(() => {
  const key = sortKey.value;
  if (!key) return props.rows;
  const col = props.columns.find((c) => c.key === key);
  const get = col?.sortValue ?? ((r: T) => r[key] as number | string | null | undefined);
  return [...props.rows].sort((a, b) => {
    const va = get(a);
    const vb = get(b);
    if (va === vb) return 0;
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    const cmp = va < vb ? -1 : 1;
    return sortDesc.value ? -cmp : cmp;
  });
});
</script>

<template>
  <table class="dt">
    <colgroup>
      <col v-for="c in columns" :key="c.key" :style="c.width ? { width: c.width + 'px' } : {}" />
    </colgroup>
    <thead>
      <tr>
        <th
          v-for="c in columns"
          :key="c.key"
          :class="{ num: c.num, ['hide-' + c.hideBelow]: !!c.hideBelow }"
          @click="toggleSort(c.key)"
        >
          {{ c.title }}
          <span v-if="sortKey === c.key" class="muted">{{ sortDesc ? "▼" : "▲" }}</span>
        </th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="row in sorted"
        :key="rowKey(row)"
        :class="{ clickable }"
        @click="clickable && emit('rowClick', row)"
      >
        <td
          v-for="c in columns"
          :key="c.key"
          :class="{ num: c.num, ['hide-' + c.hideBelow]: !!c.hideBelow }"
        >
          <slot :name="c.key" :row="row">{{ row[c.key] ?? "—" }}</slot>
        </td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
@media (max-width: 1280px) {
  .hide-1280 {
    display: none;
  }
}
@media (max-width: 768px) {
  .hide-768 {
    display: none;
  }
}
</style>
