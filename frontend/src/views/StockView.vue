<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { api } from "../api/client";
import type { EvidenceGroup } from "../api/types";
import { stockHistoryOption } from "../charts/options";
import { HOLD_LABEL, LIST_LABEL, POINT_LABEL, ROLE_LABEL, STAGE_LABEL, pct, yi, num, signClass } from "../charts/format";
import EmptyState from "../components/EmptyState.vue";
import EvidenceCard from "../components/EvidenceCard.vue";
import HintBlock from "../components/HintBlock.vue";
import IndexGauge from "../components/IndexGauge.vue";
import MetaBadge from "../components/MetaBadge.vue";
import StageBadge from "../components/StageBadge.vue";
import { useEcharts } from "../composables/useEcharts";
import { useTradeDateStore } from "../stores/tradeDate";

const props = defineProps<{ code: string }>();
const store = useTradeDateStore();
const qc = useQueryClient();

const detail = useQuery({
  queryKey: computed(() => ["stock", props.code, store.date]),
  queryFn: () => api.stockDetail(props.code, store.scope),
});
const history = useQuery({
  queryKey: computed(() => ["stock", props.code, "history", store.date]),
  queryFn: () => api.stockHistory(props.code, 120, store.date),
});
const watchlist = useQuery({ queryKey: ["watchlist", null], queryFn: () => api.watchlist(null) });
const inWatch = computed(() => (watchlist.data.value?.data ?? []).some((w) => w.code === props.code));

const d = computed(() => detail.data.value?.data ?? null);
const chartEl = ref<HTMLElement | null>(null);
const option = computed(() => (history.data.value?.data.length ? stockHistoryOption(history.data.value.data) : null));
useEcharts(chartEl, option);

const toggleWatch = useMutation({
  mutationFn: () => (inWatch.value ? api.removeWatch(props.code) : api.addWatch(props.code)),
  onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
});
const note = ref("");
const mark = useMutation({
  mutationFn: (m: "bought" | "sold" | "ignored") => api.addMark(props.code, m, note.value || undefined),
  onSuccess: () => {
    note.value = "";
    void qc.invalidateQueries({ queryKey: ["stock", props.code] });
  },
});
const MARK_LABEL: Record<string, string> = { bought: "已买入", sold: "已卖出", ignored: "忽略" };

const keyFeatures = [
  ["main_ratio", "净占比", "pct"],
  ["main_mean_5d", "5 日均值 亿", "yi"],
  ["inflow_days_5d", "5 日流入天数", "int"],
  ["retention_5d", "5 日留存 亿", "yi"],
  ["ret_5d", "5 日涨跌", "pct"],
  ["ret_20d", "20 日涨跌", "pct"],
  ["range_pos_250d", "250 日位置", "pct"],
  ["turnover_vs_20d", "换手/20 日", "num"],
  ["super_share", "超大单占比", "pct"],
  ["drawdown_20d", "20 日回撤", "pct"],
  ["vwap_gap_20d", "距 20 日成交均价", "pct"],
] as const;
function fmt(kind: string, v: number | boolean | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (kind === "pct") return pct(v, 1, true);
  if (kind === "yi") return yi(v);
  if (kind === "int") return String(v);
  return num(v, 2);
}

const EXCL_LABEL: Record<string, string> = { control: "控盘风险", crash: "历史暴涨暴跌", untradable: "不可交易" };
const exclusions = computed(() =>
  Object.entries(d.value?.profile?.exclusions ?? {}) as [string, EvidenceGroup][],
);
const hitExclusions = computed(() => exclusions.value.filter(([, g]) => g.hit));
const showExcl = ref(false);
const openRole = ref<string | null>(null);
const openAction = ref<string | null>(null);
</script>

<template>
  <div class="page">
    <EmptyState v-if="detail.isLoading.value" reason="加载中…" />
    <EmptyState v-else-if="detail.isError.value" :reason="String(detail.error.value)" />
    <template v-else-if="d">
      <h1 class="page-title">
        {{ d.security.name }}<span class="code">{{ d.security.code }}</span>
        <span v-if="d.security.board" class="tag">{{ d.security.board }}</span>
        <span v-if="d.security.is_st" class="tag outline-danger">ST</span>
        <span v-for="[k] in hitExclusions" :key="k" class="tag outline-danger">{{ EXCL_LABEL[k] ?? k }}</span>
        <MetaBadge :meta="detail.data.value?.meta" />
        <span class="spacer" />
        <button class="btn" :disabled="toggleWatch.isPending.value" @click="toggleWatch.mutate()">{{ inWatch ? "移出自选" : "加入自选" }}</button>
      </h1>

      <div class="grid grid-4">
        <div class="card stat">
          <span class="value" :class="signClass(d.bar?.pct_chg as number | null)">{{ num(d.bar?.close as number | null) }} <small>{{ pct(d.bar?.pct_chg as number | null, 2, true) }}</small></span>
          <span class="label">收盘 · 成交 {{ yi(d.bar?.amount as number | null) }} 亿 · 换手 {{ pct(d.bar?.turnover as number | null, 2) }}</span>
        </div>
        <div class="card stat">
          <span class="value" :class="signClass(d.flow?.net_main as number | null)">{{ yi(d.flow?.net_main as number | null) }} 亿</span>
          <span class="label">主力净流入 · 净占比 {{ pct(d.flow?.main_ratio as number | null, 2) }}<span v-if="d.flow && d.flow.reconciled === false" class="tag unreconciled" style="margin-left: 6px">未对账</span><span v-if="!d.flow" class="tag missing" style="margin-left: 6px">缺失</span></span>
        </div>
        <div class="card stat">
          <span class="value">{{ d.profile?.score ?? "—" }}<small v-if="d.profile?.score_tier"> {{ d.profile.score_tier }}</small></span>
          <span class="label">结构分（依据 {{ d.profile?.basis_sector_name ?? "—" }}）<span v-if="d.profile && !d.profile.tradable" class="tag missing" style="margin-left: 6px">不可交易</span></span>
        </div>
        <div class="card stat">
          <span class="value" :class="d.hold_eval ? 'hold-' + d.hold_eval.eval : ''">{{ d.hold_eval ? HOLD_LABEL[d.hold_eval.eval] ?? d.hold_eval.eval : "—" }}<small v-if="d.hold_eval?.changed_from"> 自{{ HOLD_LABEL[d.hold_eval.changed_from] }}</small></span>
          <span class="label">{{ d.hold_eval?.evidence?.text ?? "持有结构评估：仅对已标记买入或自选个股计算" }}</span>
        </div>
      </div>

      <HintBlock :hints="d.hints" style="margin-top: 12px" />

      <div class="grid grid-2" style="margin-top: 12px">
        <div class="card">
          <div class="section-title" style="margin-top: 0">身份</div>
          <div v-if="d.profile?.identity" class="identity">
            <span class="tag">资金体量 {{ d.profile.identity.amount_tier ?? "—" }}</span>
            <span v-if="d.profile.identity.float_mv_tier" class="tag">流通市值 {{ d.profile.identity.float_mv_tier }}</span>
            <span v-for="i in d.profile.identity.indices" :key="i" class="tag">{{ i }}</span>
            <span v-for="c in d.profile.identity.concepts" :key="c" class="tag concept">{{ c }}</span>
          </div>
          <div v-else class="muted">当日无画像</div>
          <div v-if="d.profile?.score_components" class="comps">
            <span v-for="c in d.profile.score_components.items" :key="c.key" class="muted">{{ c.name }} {{ c.value ?? "—" }}/{{ c.max }}</span>
            <span v-if="d.profile.score_components.penalty" class="muted">扣分 {{ d.profile.score_components.penalty }}（{{ d.profile.score_components.penalty_reasons.join("、") }}）</span>
          </div>
          <a v-if="exclusions.length" href="#" class="muted" @click.prevent="showExcl = !showExcl">{{ showExcl ? "收起排除项依据" : "排除项依据" }}</a>
          <div v-if="showExcl">
            <div v-for="[k, g] in exclusions" :key="k" style="margin-top: 6px">
              <b>{{ EXCL_LABEL[k] ?? k }}</b> <span :class="g.hit ? 'tag outline-danger' : 'muted'">{{ g.hit ? "命中" : "未命中" }}</span>
              <EvidenceCard :evidence="{ [k]: g.items }" />
            </div>
          </div>
        </div>
        <div class="card">
          <div class="section-title" style="margin-top: 0">派生量</div>
          <table class="dt">
            <tbody>
              <tr v-for="[k, label, kind] in keyFeatures" :key="k">
                <td>{{ label }}</td>
                <td class="num">{{ fmt(kind, d.features?.[k]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="section-title">所属板块与角色</div>
      <table class="dt">
        <thead>
          <tr>
            <th style="width: 60px">层级</th>
            <th>板块</th>
            <th style="width: 160px">阶段</th>
            <th style="width: 90px">角色</th>
            <th>标签</th>
            <th class="num" style="width: 70px">角色分</th>
            <th style="width: 60px" />
          </tr>
        </thead>
        <tbody>
          <template v-for="s in d.sectors" :key="s.sector_id">
            <tr class="clickable" @click="$router.push({ name: 'sector', params: { id: s.sector_id }, query: $route.query })">
              <td><span class="tag">{{ s.level }}</span></td>
              <td>{{ s.name }}<span class="code">{{ s.sector_id }}</span></td>
              <td><StageBadge v-if="s.stage" :stage="s.stage" :days="s.days_in_stage" /><span v-else class="muted">无阶段</span></td>
              <template v-for="r in d.roles.filter((x) => x.sector_id === s.sector_id)" :key="r.sector_id">
                <td><span class="tag" :class="'role-' + r.role">{{ ROLE_LABEL[r.role] }}</span></td>
                <td><span v-for="t in r.tags ?? []" :key="t" class="tag">{{ t }}</span></td>
                <td class="num">{{ r.score ?? "—" }}</td>
                <td><a href="#" @click.prevent.stop="openRole = openRole === r.sector_id ? null : r.sector_id">依据</a></td>
              </template>
              <template v-if="!d.roles.some((x) => x.sector_id === s.sector_id)">
                <td class="muted" colspan="4">未参与角色判定</td>
              </template>
            </tr>
            <tr v-if="openRole === s.sector_id">
              <td colspan="7"><EvidenceCard :evidence="d.roles.find((x) => x.sector_id === s.sector_id)?.evidence" /></td>
            </tr>
          </template>
        </tbody>
      </table>

      <template v-if="d.actions.length">
        <div class="section-title">今日动作</div>
        <table class="dt">
          <tbody>
            <template v-for="a in d.actions" :key="`${a.list_type}|${a.sector_id}|${a.point_type ?? ''}`">
              <tr>
                <td style="width: 120px"><b>{{ LIST_LABEL[a.list_type] ?? a.list_type }}</b></td>
                <td style="width: 180px">{{ a.sector_name ?? a.sector_id }}</td>
                <td>
                  <span v-if="a.point_type" class="tag">{{ POINT_LABEL[a.point_type] ?? a.point_type }}</span>
                  <span v-for="t in a.tags ?? []" :key="t" class="tag">{{ t }}</span>
                </td>
                <td style="width: 60px"><a href="#" @click.prevent="openAction = openAction === a.list_type + a.sector_id ? null : a.list_type + a.sector_id">依据</a></td>
              </tr>
              <tr v-if="openAction === a.list_type + a.sector_id">
                <td colspan="4"><EvidenceCard :evidence="a.evidence" /></td>
              </tr>
            </template>
          </tbody>
        </table>
      </template>

      <template v-if="d.indices.length">
        <div class="section-title">指数（描述性，不影响判定）</div>
        <div class="grid grid-2">
          <IndexGauge v-for="ix in d.indices" :key="ix.index_name" :index="ix" />
        </div>
      </template>

      <div class="section-title">近 120 日：收盘价与主力净流入（未对账日柱体淡显；缺失日留空）</div>
      <div ref="chartEl" class="chart card" />

      <template v-if="d.tracking.length">
        <div class="section-title">跟踪记录</div>
        <table class="dt">
          <thead>
            <tr>
              <th>来源</th>
              <th>依据板块</th>
              <th>入选日</th>
              <th class="num">入选价</th>
              <th>入选时阶段</th>
              <th>退出</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in d.tracking" :key="`${t.list_type}|${t.sector_id}|${t.entered_date}`">
              <td>{{ LIST_LABEL[t.list_type] ?? t.list_type }}</td>
              <td>{{ t.sector_name ?? t.sector_id }}</td>
              <td>{{ t.entered_date }}</td>
              <td class="num">{{ num(t.entered_price) }}</td>
              <td>{{ t.entered_stage ? STAGE_LABEL[t.entered_stage] : "—" }}</td>
              <td>{{ t.exited_date ? `${t.exited_date} · ${POINT_LABEL[t.exit_reason ?? ""] ?? t.exit_reason ?? ""}` : "跟踪中" }}</td>
            </tr>
          </tbody>
        </table>
      </template>

      <div class="section-title">我的标记</div>
      <div class="toolbar">
        <input v-model="note" placeholder="备注（可选）" style="width: 240px" />
        <button :disabled="mark.isPending.value" @click="mark.mutate('bought')">标记已买入</button>
        <button :disabled="mark.isPending.value" @click="mark.mutate('sold')">标记已卖出</button>
        <button :disabled="mark.isPending.value" @click="mark.mutate('ignored')">忽略</button>
        <span v-if="mark.isError.value" class="muted">{{ String(mark.error.value) }}</span>
      </div>
      <table v-if="d.marks.length" class="dt">
        <tbody>
          <tr v-for="m in d.marks" :key="m.marked_at">
            <td style="width: 180px" class="muted">{{ m.marked_at }}</td>
            <td style="width: 80px">{{ MARK_LABEL[m.mark] ?? m.mark }}</td>
            <td>{{ m.note ?? "" }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.chart {
  height: 320px;
  padding: 0;
}
.spacer {
  flex: 1;
}
.btn {
  font: inherit;
  font-size: 12px;
  padding: 3px 10px;
  border: 1px solid var(--c-border);
  background: #fff;
  border-radius: 4px;
  cursor: pointer;
}
.identity {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.concept {
  background: #ede9fe;
  color: #5b21b6;
}
.comps {
  display: flex;
  gap: 10px;
  margin: 8px 0 4px;
  font-size: 12px;
}
.role-core {
  background: #dbeafe;
  color: #1e3a8a;
}
.role-avoid {
  background: #e5e7eb;
  color: #374151;
}
.hold-broken {
  color: var(--c-text-2);
}
</style>
