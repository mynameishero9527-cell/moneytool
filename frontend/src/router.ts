import { createRouter, createWebHistory } from "vue-router";
import { useTradeDateStore } from "./stores/tradeDate";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "market", component: () => import("./views/MarketView.vue") },
    { path: "/board", name: "board", component: () => import("./views/BoardView.vue") },
    { path: "/sectors/:id", name: "sector", component: () => import("./views/SectorDetailView.vue"), props: true },
    { path: "/stocks/:code", name: "stock", component: () => import("./views/StockView.vue"), props: true },
    { path: "/status", name: "status", component: () => import("./views/DataStatusView.vue") },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

// URL ?date=YYYY-MM-DD&segment=... 进入回看；所有路由保留该参数
router.beforeEach((to) => {
  const store = useTradeDateStore();
  const date = typeof to.query.date === "string" ? to.query.date : null;
  const segment = typeof to.query.segment === "string" ? to.query.segment : null;
  if (date !== store.date || segment !== store.segment) store.set(date, segment);
  return true;
});
