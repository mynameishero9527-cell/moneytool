<script setup lang="ts">
import { computed } from "vue";
import type { Evidence, EvidenceMap } from "../api/types";

const props = defineProps<{
  evidence: EvidenceMap | null | undefined;
  footer?: string;
}>();

const RULE_LABEL: Record<string, string> = {
  freeze: "冰点",
  start: "启动",
  spread: "扩散加速",
  climax: "高潮拥挤",
  diverge: "分歧背离",
  ebb: "退潮",
  half: "前后半段",
  pulse: "脉冲",
  gate: "风控开关",
};

const groups = computed(() => {
  const ev = props.evidence ?? {};
  const out: { rule: string; items: Evidence[]; hitCount: number }[] = [];
  for (const [rule, items] of Object.entries(ev)) {
    if (!Array.isArray(items)) continue;
    const list = items.filter((e): e is Evidence => typeof e === "object" && e !== null && "metric" in e);
    if (!list.length) continue;
    out.push({ rule, items: list, hitCount: list.filter((e) => e.hit).length });
  }
  return out;
});

function fmt(v: Evidence["value"]): string {
  if (v === null || v === undefined) return "缺失";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (Math.abs(v) >= 1e6) return (v / 1e8).toFixed(2) + "亿";
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(3);
}
</script>

<template>
  <div class="evidence card">
    <div v-if="!groups.length" class="muted">无证据记录</div>
    <div v-for="g in groups" :key="g.rule" class="group">
      <div class="group-title">
        {{ RULE_LABEL[g.rule] ?? g.rule }}
        <span class="muted">{{ g.hitCount }}/{{ g.items.length }} 命中</span>
      </div>
      <table>
        <tbody>
          <tr v-for="(e, i) in g.items" :key="i" :class="{ miss: !e.hit }">
            <td class="mark">{{ e.hit ? "✓" : "✗" }}</td>
            <td class="metric">{{ e.metric }}</td>
            <td class="num">{{ fmt(e.value) }}</td>
            <td class="op">{{ e.op }}</td>
            <td class="num">{{ fmt(e.threshold) }}</td>
            <td class="note muted">{{ e.note ?? "" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="footer" class="footer-line muted">{{ footer }}</div>
  </div>
</template>

<style scoped>
.evidence {
  font-size: 12px;
}
.group + .group {
  margin-top: 8px;
}
.group-title {
  font-weight: 600;
  margin-bottom: 2px;
  display: flex;
  gap: 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
}
td {
  padding: 1px 6px;
  white-space: nowrap;
}
td.mark {
  width: 18px;
  color: var(--c-up);
}
tr.miss td {
  color: var(--c-text-2);
}
tr.miss td.mark {
  color: var(--c-missing);
}
td.metric {
  font-family: ui-monospace, Menlo, monospace;
}
td.num {
  font-variant-numeric: tabular-nums;
  text-align: right;
}
td.note {
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 220px;
}
.footer-line {
  margin-top: 8px;
  border-top: 1px dashed var(--c-border);
  padding-top: 4px;
}
</style>
