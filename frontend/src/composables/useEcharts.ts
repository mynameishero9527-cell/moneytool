import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { onBeforeUnmount, type Ref, watch } from "vue";

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkAreaComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

export type ChartOption = EChartsCoreOption;

/** 管一个 ECharts 实例的创建、option 更新、resize 与销毁。容器可晚于数据出现（v-if 切换）。 */
export function useEcharts(el: Ref<HTMLElement | null>, option: Ref<ChartOption | null>) {
  let chart: echarts.ECharts | null = null;
  let observed: HTMLElement | null = null;
  const ro = new ResizeObserver(() => chart?.resize());

  function dispose() {
    if (observed) ro.unobserve(observed);
    observed = null;
    chart?.dispose();
    chart = null;
  }

  function render() {
    const node = el.value;
    if (!node) {
      dispose();
      return;
    }
    if (chart && observed !== node) dispose();
    if (!option.value) return;
    if (!chart) {
      chart = echarts.init(node);
      ro.observe(node);
      observed = node;
    }
    chart.setOption(option.value, { notMerge: true });
  }

  watch([el, option], render, { flush: "post", immediate: true });
  onBeforeUnmount(() => {
    dispose();
    ro.disconnect();
  });
}
