import type {
  ActionRow,
  BacktestRow,
  FlowHorizonsData,
  FlowSectorData,
  FlowSignalsData,
  BriefRow,
  Envelope,
  ListType,
  StatsData,
  TrackingRow,
  WatchRow,
  MarketHistoryRow,
  MarketOverview,
  SearchResult,
  SectorDetail,
  SectorHistoryRow,
  SectorStageRow,
  StatusData,
  SyncData,
  StockDetail,
  StockHistoryRow,
} from "./types";

export type DateScope = {
  trade_date?: string | null;
  segment?: string | null;
};

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

async function get<T>(path: string, params: Params | DateScope = {}): Promise<Envelope<T>> {
  const token = localStorage.getItem("moneytool.token");
  const res = await fetch(`/api${path}${qs({ ...params })}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as Envelope<T>;
}

async function send<T>(method: "POST" | "DELETE", path: string, body?: unknown): Promise<Envelope<T>> {
  const token = localStorage.getItem("moneytool.token");
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as Envelope<T>;
}

export const api = {
  status: () => get<StatusData>("/status"),
  sync: () => get<SyncData>("/sync"),
  search: (q: string) => get<SearchResult>("/search", { q }),
  marketOverview: (scope: DateScope) => get<MarketOverview | null>("/market/overview", scope),
  marketHistory: (days: number, end?: string | null) =>
    get<MarketHistoryRow[]>("/market/history", { days, end }),
  sectors: (scope: DateScope, level?: string | null, stage?: string | null) =>
    get<SectorStageRow[]>("/sectors", { ...scope, level, stage }),
  sectorDetail: (id: string, scope: DateScope) =>
    get<SectorDetail>(`/sectors/${encodeURIComponent(id)}`, scope),
  sectorHistory: (id: string, days: number, end?: string | null) =>
    get<SectorHistoryRow[]>(`/sectors/${encodeURIComponent(id)}/history`, { days, end }),
  stockDetail: (code: string, scope: DateScope) =>
    get<StockDetail>(`/stocks/${encodeURIComponent(code)}`, { trade_date: scope.trade_date }),
  stockHistory: (code: string, days: number, end?: string | null) =>
    get<StockHistoryRow[]>(`/stocks/${encodeURIComponent(code)}/history`, { days, end }),
  quality: (trade_date?: string | null) => get<unknown[]>("/quality", { trade_date }),
  actions: (listType: ListType, scope: DateScope) =>
    get<ActionRow[]>("/actions", { list_type: listType, ...scope }),
  tracking: (activeOnly = true) => get<TrackingRow[]>("/tracking", { active_only: activeOnly }),
  watchlist: (trade_date?: string | null) => get<WatchRow[]>("/watchlist", { trade_date }),
  addWatch: (code: string) => send<unknown>("POST", `/watchlist/${encodeURIComponent(code)}`),
  removeWatch: (code: string) => send<unknown>("DELETE", `/watchlist/${encodeURIComponent(code)}`),
  addMark: (code: string, mark: "bought" | "sold" | "ignored", note?: string) =>
    send<unknown>("POST", "/marks", { code, mark, note: note ?? null }),
  stats: (since?: string | null) => get<StatsData>("/stats", { since }),
  brief: (kind: "close" | "premarket", trade_date?: string | null) =>
    get<BriefRow>("/brief", { kind, trade_date }),
  briefs: (limit = 30) => get<BriefRow[]>("/briefs", { limit }),
  flowHorizons: (scope: DateScope, level: string) => get<FlowHorizonsData>("/flow/horizons", { ...scope, level }),
  flowSignals: (params: { days?: number; kind?: string | null; sector_id?: string | null; limit?: number } = {}) =>
    get<FlowSignalsData>("/flow/signals", params),
  flowSector: (id: string, scope: DateScope) =>
    get<FlowSectorData>(`/flow/sectors/${encodeURIComponent(id)}`, scope),
  flowBacktest: (trade_date?: string | null) => get<{ items: BacktestRow[]; note: string }>("/flow/backtest", { trade_date }),
};
