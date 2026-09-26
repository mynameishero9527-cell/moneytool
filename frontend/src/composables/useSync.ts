import { useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { api } from "../api/client";
import type { SyncData, SyncLane } from "../api/types";

/** 同步进度：未完成时每 5 秒刷新，完成后每分钟一次（顶部横幅与数据状态页共用同一查询）。 */
export function useSync() {
  const q = useQuery({
    queryKey: ["sync"],
    queryFn: api.sync,
    refetchInterval: (query) => ((query.state.data?.data as SyncData | undefined)?.complete ? 60_000 : 5_000),
  });
  const sync = computed(() => q.data.value?.data ?? null);
  return { query: q, sync };
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "估算中";
  if (seconds < 60) return "不到 1 分钟";
  const m = Math.floor(seconds / 60);
  const h = Math.floor(m / 60);
  return h ? `${h} 小时 ${m % 60} 分` : `${m} 分钟`;
}

export function clock(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export const LANE_STATE_LABEL: Record<string, string> = {
  waiting: "等待开始",
  running: "拉取中",
  paused: "已暂停",
  idle: "已补完",
  unknown: "未运行",
};

/** 当前批里已拉取、尚未写库的数量（进度条上浅色部分）。 */
export function inflight(lane: SyncLane): number {
  return lane.state === "running" ? Math.max(0, Math.min(lane.batch_done, lane.total - lane.done)) : 0;
}

export function laneStatus(lane: SyncLane): string {
  if (lane.total > 0 && lane.done >= lane.total) return "已完成";
  if (lane.state === "paused") return `暂停${lane.resume_at ? `至 ${clock(lane.resume_at)}` : ""}`;
  if (lane.state === "running") {
    return lane.rate_per_min ? `${Math.round(lane.rate_per_min)} 只/分 · 剩余约 ${formatDuration(lane.eta_seconds)}` : "拉取中 · 速度估算中";
  }
  return LANE_STATE_LABEL[lane.state] ?? lane.state;
}
