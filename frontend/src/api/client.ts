import type {
  Envelope,
  MarketHistoryRow,
  MarketOverview,
  SearchResult,
  SectorDetail,
  SectorHistoryRow,
  SectorStageRow,
  StatusData,
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

export const api = {
  status: () => get<StatusData>("/status"),
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
};
