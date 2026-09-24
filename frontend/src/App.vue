<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "./api/client";
import { useTradeDateStore } from "./stores/tradeDate";

const store = useTradeDateStore();
const route = useRoute();
const router = useRouter();

// 交易时段内每 60 秒拉状态；仅在非回看模式
const status = useQuery({
  queryKey: ["status"],
  queryFn: api.status,
  refetchInterval: () => (store.isReplay ? false : 60_000),
});

const gate = computed(() => status.data.value?.data.risk_gate ?? false);
const gateReason = computed(() => {
  const d = status.data.value?.data;
  return d?.data_status === "degraded" ? d.degraded_reason : null;
});

const q = ref("");
const search = useQuery({
  queryKey: computed(() => ["search", q.value]),
  queryFn: () => api.search(q.value),
  enabled: computed(() => q.value.trim().length > 0),
});
function go(kind: string, code: string) {
  q.value = "";
  const query = { ...route.query };
  void router.push(kind === "stock" ? { name: "stock", params: { code }, query } : { name: "sector", params: { id: code }, query });
}

const replayDate = computed({
  get: () => store.date ?? "",
  set: (v: string) => {
    void router.replace({ query: { ...route.query, date: v || undefined } });
  },
});
function exitReplay() {
  const { date: _d, segment: _s, ...rest } = route.query;
  void router.replace({ query: rest });
}
const linkQuery = computed(() => (store.isReplay ? { date: store.date, segment: store.segment ?? undefined } : {}));
</script>

<template>
  <div v-if="gate" class="gate-banner">
    市场风控：暂停新增买入关注与低位企稳，原因：<router-link :to="{ name: 'market', query: linkQuery }">查看依据</router-link>
  </div>
  <div v-if="gateReason" class="gate-banner">{{ gateReason }}</div>
  <div v-if="store.isReplay" class="gate-banner replay">
    回看模式 {{ store.date }}<span v-if="store.segment"> · {{ store.segment }}</span>，数据为当日已确认口径，盘中轮询已停止。
    <a href="#" @click.prevent="exitReplay">回到最新</a>
  </div>
  <header class="top">
    <router-link class="brand" :to="{ name: 'market', query: linkQuery }">moneytool</router-link>
    <nav>
      <router-link :to="{ name: 'market', query: linkQuery }">总览</router-link>
      <router-link :to="{ name: 'board', query: linkQuery }">看板</router-link>
      <router-link :to="{ name: 'status', query: linkQuery }">数据状态</router-link>
    </nav>
    <div class="spacer" />
    <label class="date muted">
      日期
      <input v-model.lazy="replayDate" type="date" />
    </label>
    <div class="search">
      <input v-model="q" placeholder="代码 / 名称 / 拼音首字母" />
      <div v-if="q && search.data.value" class="results card">
        <div
          v-for="s in search.data.value.data.sectors"
          :key="s.code"
          class="item"
          @click="go('sector', s.code)"
        >
          <span class="tag">{{ s.kind }}</span> {{ s.name }}<span class="code">{{ s.code }}</span>
        </div>
        <div
          v-for="s in search.data.value.data.stocks"
          :key="s.code"
          class="item"
          @click="go('stock', s.code)"
        >
          {{ s.name }}<span class="code">{{ s.code }}</span>
        </div>
        <div
          v-if="!search.data.value.data.sectors.length && !search.data.value.data.stocks.length"
          class="muted item"
        >
          无匹配
        </div>
      </div>
    </div>
    <span v-if="status.data.value" class="muted small">
      最近确认 {{ status.data.value.data.latest_confirmed ?? "—" }}
      <span v-if="status.data.value.data.segments_captured.length"> · 今日分段 {{ status.data.value.data.segments_captured.length }}</span>
    </span>
  </header>
  <main>
    <router-view />
  </main>
  <footer class="footer">分析工具，不构成投资建议；资金流为行情商估算；历史统计不代表未来结果。</footer>
</template>

<style scoped>
.top {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--c-border);
}
.brand {
  font-weight: 700;
  font-size: 15px;
}
nav {
  display: flex;
  gap: 12px;
}
nav a.router-link-active {
  font-weight: 600;
  border-bottom: 2px solid var(--c-text);
}
.spacer {
  flex: 1;
}
.date input,
.search input {
  font: inherit;
  padding: 3px 8px;
  border: 1px solid var(--c-border);
  border-radius: 4px;
}
.search {
  position: relative;
}
.search input {
  width: 220px;
}
.results {
  position: absolute;
  top: 30px;
  right: 0;
  width: 320px;
  z-index: 10;
  background: #fff;
  max-height: 360px;
  overflow: auto;
}
.item {
  padding: 4px 6px;
  cursor: pointer;
}
.item:hover {
  background: #f3f4f6;
}
.small {
  font-size: 12px;
}
.replay {
  background: #eff6ff;
  color: #1e3a8a;
  border-bottom-color: #bfdbfe;
}
</style>
