<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed } from "vue";
import { api } from "../api/client";
import { HOLD_LABEL, pct, yi, num, signClass } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import MetaBadge from "../components/MetaBadge.vue";
import { useTradeDateStore } from "../stores/tradeDate";

const store = useTradeDateStore();
const qc = useQueryClient();
const watch = useQuery({
  queryKey: computed(() => ["watchlist", store.date]),
  queryFn: () => api.watchlist(store.date),
});
const remove = useMutation({
  mutationFn: (code: string) => api.removeWatch(code),
  onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
});
const rows = computed(() => watch.data.value?.data ?? []);
const stocks = computed(() => rows.value.filter((r) => r.kind === "stock"));
const sectors = computed(() => rows.value.filter((r) => r.kind === "sector"));
</script>

<template>
  <div class="page">
    <h1 class="page-title">自选 <MetaBadge :meta="watch.data.value?.meta" /></h1>
    <EmptyState v-if="watch.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="!rows.length" reason="尚未添加自选；在个股或板块页点「加入自选」" />
    <template v-else>
      <div v-if="sectors.length" class="section-title">板块</div>
      <table v-if="sectors.length" class="dt">
        <thead>
          <tr>
            <th>板块</th>
            <th class="num">涨跌</th>
            <th class="num">主力净流入 亿</th>
            <th class="num">净占比</th>
            <th>加入</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in sectors" :key="s.code" class="clickable" @click="$router.push({ name: 'sector', params: { id: s.code }, query: $route.query })">
            <td>{{ s.name ?? s.code }}<span class="code">{{ s.code }}</span></td>
            <td class="num" :class="signClass(s.pct_chg)">{{ pct(s.pct_chg, 2, true) }}</td>
            <td class="num" :class="signClass(s.net_main)">{{ yi(s.net_main) }}</td>
            <td class="num" :class="signClass(s.main_ratio)">{{ pct(s.main_ratio, 2) }}</td>
            <td class="muted">{{ s.added_at.slice(0, 10) }}</td>
            <td style="width: 60px"><a href="#" @click.prevent.stop="remove.mutate(s.code)">移除</a></td>
          </tr>
        </tbody>
      </table>
      <div v-if="stocks.length" class="section-title">个股</div>
      <table v-if="stocks.length" class="dt">
        <thead>
          <tr>
            <th>个股</th>
            <th class="num">收盘</th>
            <th class="num">涨跌</th>
            <th class="num">主力净流入 亿</th>
            <th class="num">净占比</th>
            <th>持有结构</th>
            <th>说明</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in stocks" :key="s.code" class="clickable" @click="$router.push({ name: 'stock', params: { code: s.code }, query: $route.query })">
            <td>{{ s.name ?? "—" }}<span class="code">{{ s.code }}</span></td>
            <td class="num">{{ num(s.close) }}</td>
            <td class="num" :class="signClass(s.pct_chg)">{{ pct(s.pct_chg, 2, true) }}</td>
            <td class="num" :class="signClass(s.net_main)">{{ yi(s.net_main) }}</td>
            <td class="num" :class="signClass(s.main_ratio)">{{ pct(s.main_ratio, 2) }}</td>
            <td><span v-if="s.hold_eval" class="tag" :class="'hold-' + s.hold_eval">{{ HOLD_LABEL[s.hold_eval] }}</span><span v-else class="muted">未评估</span></td>
            <td class="muted">{{ s.hold_text ?? "" }}</td>
            <td style="width: 60px"><a href="#" @click.prevent.stop="remove.mutate(s.code)">移除</a></td>
          </tr>
        </tbody>
      </table>
      <p class="muted small">持有结构评估只对已标记买入或自选的个股计算，描述的是资金与价格结构，不构成投资建议。</p>
    </template>
  </div>
</template>

<style scoped>
.small {
  font-size: 12px;
}
</style>
