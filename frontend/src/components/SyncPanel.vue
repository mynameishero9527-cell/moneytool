<script setup lang="ts">
import { computed } from "vue";
import type { SyncLane } from "../api/types";
import { clock, formatDuration, inflight, laneStatus, useSync } from "../composables/useSync";
import SyncBar from "./SyncBar.vue";

const { sync, query } = useSync();
const STEPS = [
  { key: "reference", label: "参考数据" },
  { key: "backfill", label: "补近期历史" },
  { key: "catchup", label: "计算结果" },
  { key: "deepening", label: "可使用 · 补更早历史" },
  { key: "ready", label: "全部完成" },
];
const ORDER: Record<string, number> = { starting: 0, reference: 0, backfill: 1, catchup: 2, deepening: 3, ready: 4 };
const current = computed(() => {
  const s = sync.value;
  if (!s) return -1;
  // 可用之后的补算（补深一层后重算近期）不退回到「计算结果」一步
  if (s.stage === "catchup" && s.usable) return 3;
  return ORDER[s.stage] ?? -1;
});
const lanes = computed(() =>
  sync.value
    ? [
        { key: "bars", hint: "Baostock · 日线与复权因子，由近及远分层补：第一层补完即开始计算", lane: sync.value.lanes.bars },
        { key: "flow", hint: "新浪财经 · 日频资金流", lane: sync.value.lanes.flow },
      ]
    : [],
);
const catchupPct = computed(() => {
  const c = sync.value?.catchup;
  return c && c.total ? Math.round((c.done / c.total) * 100) : 0;
});
const stateClass = (lane: SyncLane) =>
  lane.total > 0 && lane.done >= lane.total ? "confirmed" : lane.state === "paused" ? "intraday" : lane.state === "running" ? "running" : "missing";
</script>

<template>
  <div v-if="sync" class="sync-panel">
    <div class="head">
      <ol class="steps">
        <li v-for="(st, i) in STEPS" :key="st.key" :class="{ done: i < current, now: i === current }">
          <span class="dot">{{ i < current ? "✓" : i + 1 }}</span>{{ st.label }}
        </li>
      </ol>
      <span class="muted small">
        <template v-if="!sync.live">主程序未运行回补，显示的是库内进度</template>
        <template v-else-if="sync.complete">全部完成</template>
        <template v-else>当前：{{ sync.stage_label }}<template v-if="sync.eta_seconds != null"> · 预计剩余 {{ formatDuration(sync.eta_seconds) }}</template></template>
        · {{ query.isFetching.value ? "刷新中" : "每 5 秒自动刷新" }}
      </span>
    </div>

    <div class="grid grid-2">
      <div v-for="item in lanes" :key="item.key" class="card lane">
        <div class="row">
          <b>{{ item.lane.label }}</b>
          <span class="tag" :class="stateClass(item.lane)">{{ laneStatus(item.lane) }}</span>
          <span class="spacer" />
          <span class="pct">{{ item.lane.percent.toFixed(1) }}%</span>
        </div>
        <SyncBar :lane="item.lane" />
        <div v-if="item.lane.tiers?.length" class="tiers">
          <span v-for="t in item.lane.tiers" :key="t.key" class="tier" :class="{ done: t.pending === 0 }">
            <span class="tier-name">{{ t.pending === 0 ? "✓ " : "" }}{{ t.label }}</span>
            <span class="tier-bar"><i :style="{ width: t.percent + '%' }" /></span>
            <span class="tier-pct">{{ t.percent.toFixed(0) }}%</span>
          </span>
        </div>
        <div class="nums">
          <span>完成 <b>{{ item.lane.done }}</b> / {{ item.lane.total }}</span>
          <span>待补 {{ item.lane.pending }}</span>
          <span :class="{ fail: item.lane.failed > 0 }">失败 {{ item.lane.failed }}</span>
          <span v-if="inflight(item.lane)">本批已拉 {{ item.lane.batch_done }} / {{ item.lane.batch_total }}（写库后计入完成）</span>
        </div>
        <div v-if="item.lane.state === 'paused'" class="note">
          {{ item.lane.note || "数据源异常" }}<template v-if="item.lane.resume_at">，{{ clock(item.lane.resume_at) }} 自动恢复</template>
        </div>
        <div class="muted small">{{ item.hint }}<template v-if="item.lane.failed"> · 失败的股票排到队尾稍后重试</template></div>
      </div>
    </div>

    <div v-if="sync.stage === 'catchup' && sync.catchup.total" class="card catchup">
      <div class="row">
        <b>计算历史结果</b><span class="spacer" /><span class="pct">{{ sync.catchup.done }} / {{ sync.catchup.total }} 天</span>
      </div>
      <div class="plain-bar"><span :style="{ width: catchupPct + '%' }" /></div>
    </div>
  </div>
</template>

<style scoped>
.sync-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.head {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.steps {
  display: flex;
  gap: 4px;
  list-style: none;
  margin: 0;
  padding: 0;
}
.steps li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px 2px 4px;
  color: var(--c-text-2);
}
.steps li + li::before {
  content: "";
  width: 18px;
  height: 1px;
  background: var(--c-border);
  margin-right: 6px;
}
.dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid var(--c-border);
  font-size: 11px;
}
.steps li.done {
  color: var(--c-text);
}
.steps li.done .dot {
  background: var(--c-progress-done);
  border-color: var(--c-progress-done);
  color: #fff;
}
.steps li.now {
  color: var(--c-text);
  font-weight: 600;
}
.steps li.now .dot {
  border-color: var(--c-progress);
  color: var(--c-progress);
}
.lane {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.spacer {
  flex: 1;
}
.pct {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.nums {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.fail {
  color: #b45309;
}
.tiers {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
}
.tier {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--c-text-2);
}
.tier.done {
  color: var(--c-progress-done);
}
.tier-bar {
  width: 70px;
  height: 5px;
  border-radius: 3px;
  background: #e5e7eb;
  overflow: hidden;
}
.tier-bar i {
  display: block;
  height: 100%;
  background: var(--c-progress);
}
.tier.done .tier-bar i {
  background: var(--c-progress-done);
}
.tier-pct {
  font-variant-numeric: tabular-nums;
}
.note {
  font-size: 12px;
  color: #92400e;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 4px;
  padding: 4px 8px;
}
.small {
  font-size: 12px;
}
.tag.running {
  border-color: var(--c-progress);
  color: var(--c-progress);
}
.tag.confirmed {
  border-color: var(--c-progress-done);
  color: var(--c-progress-done);
}
.plain-bar {
  height: 8px;
  border-radius: 4px;
  background: #e5e7eb;
  overflow: hidden;
  margin-top: 6px;
}
.plain-bar span {
  display: block;
  height: 100%;
  background: var(--c-progress);
  transition: width 0.6s ease;
}
</style>
