<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { StatGroup } from "../api/types";
import { HOLD_LABEL, LIST_LABEL, POINT_LABEL, pct } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";

const tab = ref<"stats" | "brief">("stats");
const since = ref("");
const stats = useQuery({
  queryKey: computed(() => ["stats", since.value]),
  queryFn: () => api.stats(since.value || null),
  enabled: computed(() => tab.value === "stats"),
});
const briefs = useQuery({ queryKey: ["briefs"], queryFn: () => api.briefs(60), enabled: computed(() => tab.value === "brief") });
const picked = ref<{ date: string; kind: "close" | "premarket" } | null>(null);
const brief = useQuery({
  queryKey: computed(() => ["brief", picked.value?.date, picked.value?.kind]),
  queryFn: () => api.brief(picked.value?.kind ?? "close", picked.value?.date ?? null),
  enabled: computed(() => tab.value === "brief"),
  retry: false,
});

function groupLabel(g: StatGroup): string {
  if (g.list_type.startsWith("point:")) {
    const t = g.list_type.slice(6);
    return `买卖点 · ${POINT_LABEL[t] ?? t}`;
  }
  if (g.list_type.startsWith("hold_")) return `持有评估 · ${HOLD_LABEL[g.list_type.slice(5)] ?? g.list_type}`;
  const base = LIST_LABEL[g.list_type] ?? g.list_type;
  return g.point_type ? `${base} · ${POINT_LABEL[g.point_type] ?? g.point_type}` : base;
}
const groups = computed(() => stats.data.value?.data.groups ?? []);

/** 极简 Markdown：标题、列表、段落；简报由后端模板生成，不含外部 HTML。 */
function renderMd(md: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const out: string[] = [];
  let inList = false;
  for (const raw of md.split("\n")) {
    const line = esc(raw.trimEnd()).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    const li = /^[-*]\s+(.*)$/.exec(line);
    if (!li && inList) {
      out.push("</ul>");
      inList = false;
    }
    if (h) {
      const level = (h[1] ?? "#").length + 1;
      out.push(`<h${level}>${h[2] ?? ""}</h${level}>`);
    } else if (li) {
      if (!inList) out.push("<ul>");
      inList = true;
      out.push(`<li>${li[1] ?? ""}</li>`);
    } else if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">回看</h1>
    <div class="toolbar">
      <button :class="{ active: tab === 'stats' }" @click="tab = 'stats'">事后统计</button>
      <button :class="{ active: tab === 'brief' }" @click="tab = 'brief'">复盘简报</button>
    </div>

    <template v-if="tab === 'stats'">
      <div class="toolbar">
        <label class="muted">入选日起 <input v-model.lazy="since" type="date" /></label>
        <span v-if="stats.data.value" class="muted small">
          参数 {{ stats.data.value.meta.param_version }} · 样本少于 {{ stats.data.value.data.min_samples }} 的组淡显，仅作参考
        </span>
      </div>
      <EmptyState v-if="stats.isLoading.value" reason="加载中…" />
      <EmptyState v-else-if="!groups.length" reason="尚无到期样本；标签入选后满观察窗口才计入" />
      <table v-else class="dt">
        <thead>
          <tr>
            <th>标签</th>
            <th class="num">窗口（交易日）</th>
            <th>风控</th>
            <th class="num">样本</th>
            <th class="num">平均收益</th>
            <th class="num">中位收益</th>
            <th class="num">上涨占比</th>
            <th class="num">超额 vs 板块</th>
            <th class="num">超额 vs 全 A</th>
            <th class="num">平均最大回撤</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in groups" :key="`${g.list_type}|${g.point_type}|${g.horizon}|${g.risk_gate}`" :class="{ thin: !g.enough }">
            <td>{{ groupLabel(g) }}</td>
            <td class="num">{{ g.horizon }}</td>
            <td>{{ g.risk_gate === null ? "—" : g.risk_gate ? "开" : "关" }}</td>
            <td class="num">{{ g.n }}<span v-if="!g.enough" class="tag missing">样本不足</span></td>
            <td class="num">{{ pct(g.mean_ret, 2, true) }}</td>
            <td class="num">{{ pct(g.median_ret, 2, true) }}</td>
            <td class="num">{{ pct(g.win_rate, 0) }}</td>
            <td class="num">{{ pct(g.mean_excess_sector, 2, true) }}</td>
            <td class="num">{{ pct(g.mean_excess_eqw, 2, true) }}</td>
            <td class="num">{{ pct(g.mean_max_drawdown, 2) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted small">统计为规则标签的历史表现描述，历史统计不代表未来结果。</p>
    </template>

    <div v-else class="brief-layout">
      <div class="card list">
        <div v-if="!briefs.data.value?.data.length" class="muted">暂无简报</div>
        <div
          v-for="b in briefs.data.value?.data ?? []"
          :key="`${b.trade_date}|${b.kind}`"
          class="item clickable"
          :class="{ active: picked?.date === b.trade_date && picked?.kind === b.kind }"
          @click="picked = { date: b.trade_date, kind: b.kind }"
        >
          {{ b.trade_date }} <span class="tag">{{ b.kind === "close" ? "收盘" : "盘前" }}</span>
        </div>
      </div>
      <div class="card content">
        <EmptyState v-if="brief.isError.value" :reason="String(brief.error.value)" />
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div v-else-if="brief.data.value?.data.markdown" class="md" v-html="renderMd(brief.data.value.data.markdown)" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.small {
  font-size: 12px;
}
tr.thin td {
  color: var(--c-text-2);
}
.brief-layout {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 12px;
}
.list .item {
  padding: 4px 6px;
}
.list .item.active {
  background: #e5e7eb;
  border-radius: 4px;
}
.md :deep(h2) {
  font-size: 16px;
  margin: 8px 0 4px;
}
.md :deep(h3) {
  font-size: 14px;
  margin: 8px 0 4px;
}
.md :deep(ul) {
  margin: 2px 0 6px 18px;
  padding: 0;
}
.md :deep(p) {
  margin: 4px 0;
}
</style>
