---
name: frontend-vue-echarts
description: moneytool 前端开发约定。写 Vue 3 + TypeScript + Vite + ECharts + TanStack Table 代码时使用；涵盖目录、组件拆分、状态与 API 客户端、图表封装、回看与盘中的处理、构建产物拷贝。
paths: ["frontend/**"]
---

# 前端开发约定

技术栈：Vue 3（`<script setup lang="ts">`）、Vite、TypeScript strict、Pinia、TanStack Query（Vue）、TanStack Table（Vue）、ECharts 5、CSS 用原生 CSS 变量 + 少量 scoped 样式，不引入 UI 组件库（表格与徽标自己写，见 `ui-design-system`）。

## 目录

```text
frontend/
  src/
    api/            自动生成的类型 + 每个接口一个函数
    components/     通用组件：StageBadge、EvidenceCard、IdentityTags、ActionItem、DataTable、FlowChart
    views/          页面：Market、Board、Compare、SectorDetail、Stock、Actions、Watchlist、Replay、Glossary、DataStatus
    stores/         Pinia：tradeDate（当前查看日期与 segment）、settings、watchlist
    composables/    useTradeDate、useIntradayPolling、useReplay
    charts/         ECharts option 生成函数，纯函数，输入数据输出 option
    styles/         tokens.css（颜色、字号、阶段色）、base.css
  index.html
  vite.config.ts    dev 代理 /api → http://127.0.0.1:8000
```

## 类型与 API

- 后端 FastAPI 自带 OpenAPI。用 `openapi-typescript` 从 `/openapi.json` 生成 `src/api/schema.d.ts`，不手写接口类型。脚本 `npm run gen:api`。
- 每个接口一个函数，放 `src/api/*.ts`，函数只做 fetch + 类型断言，不做业务处理。
- 全部查询接口带 `trade_date` 与可选 `segment`，由 `useTradeDate()` 统一注入。任何页面不要自己拼日期。
- 响应里的口径元信息（`meta.status`: `confirmed | intraday | unreconciled | missing`，`meta.param_version`）由 `DataTable` 与 `EvidenceCard` 自动渲染角标，业务组件不重复处理。

## 状态

- `tradeDate` store：`date`、`segment`、`isReplay`。回看时 `isReplay = true`，所有轮询停止，页面顶部显示回看横幅。
- `useIntradayPolling`：交易时段内每 60 秒拉 `/api/status`，`latest_segment` 变化才触发 Query 失效，不做定时全量刷新。
- TanStack Query 的 key 一律包含 `[resource, tradeDate, segment, ...params]`。

## 组件规则

- 组件只接 props，不在组件内调 API；数据在 view 层用 Query 取，再传下去。
- 表格统一 `DataTable`（TanStack Table 封装）：固定列宽、数字右对齐、排序切换（留存 / 评分）、行折叠（一级 → 二级）。看板与行动清单都用它。
- 徽标、标签、证据卡片按 `ui-design-system` 的规范，不在业务组件里重复写颜色。
- 空状态组件 `EmptyState` 必须传 `reason`，不允许空白。

## ECharts

- 一个 `useEcharts(el, optionRef)` composable 管实例创建、`resize`、销毁。
- option 生成放 `src/charts/`，纯函数、可单测。数据格式统一为 `{ x: string[]; series: { name, data, color? }[] }`。
- 阶段色带用 `markArea`，颜色取 `tokens.css` 的阶段色（通过 `getComputedStyle` 读取一次缓存）。
- 大数据量（60 日 × 8 板块）不需要 dataZoom，保持简单。
- tooltip 用 `formatter` 函数输出表格样式，列出全部系列。

## 回看

- URL 带 `?date=YYYY-MM-DD` 时进入回看；所有路由保留该参数。
- 回看时禁用「今日新增 / 移出」以外的盘中标记，数据来自 confirmed。

## 构建与分发

- `npm run build` 输出 `frontend/dist/`。
- `python scripts/build_frontend.py` 调用构建并把 `dist/` 拷到 `moneytool/static/`，同时写入 `moneytool/static/BUILD_INFO`（git sha、时间）。
- `moneytool/static/` 提交进仓库，保证 `pip install` 即可用；CI 校验重新构建后无 diff。
- 不在运行时依赖 CDN；ECharts 打进 bundle。

## 检查清单

`npm run lint && npm run typecheck && npm run test` 通过；新组件有 props 类型；新接口走生成的类型；页面在 768 宽下能走完主路径；构建产物已更新。
