/**
 * 图表：统一 ECharts 主题。
 *
 * 重点打磨区 B。规则来自 V2 视觉规范：固定五色板、克制坐标轴、尊重
 * prefers-reduced-motion、深浅色跟随 CSS 变量，不做彩虹配色与 3D。
 */

const { nextTick, onBeforeUnmount, onMounted, ref, watch } = Vue;

const PALETTE_SLOTS = 6;
const COMPOSITION = new Set(['pie', 'donut', 'rose', 'treemap', 'sunburst', 'funnel', 'gauge']);

const BASE_TYPE = {
  bar: 'bar', grouped_bar: 'bar', stacked_bar: 'bar', diverging_bar: 'bar', waterfall: 'bar', bullet: 'bar',
  line: 'line', area: 'line', stacked_area: 'line', sparkline: 'line', slope: 'line', bump: 'line',
  scatter: 'scatter', bubble: 'scatter', density: 'line', boxplot: 'boxplot', violin: 'boxplot',
};

function tokens() {
  const style = getComputedStyle(document.documentElement);
  const read = (name, fallback) => (style.getPropertyValue(name).trim() || fallback);
  return {
    color: Array.from({ length: PALETTE_SLOTS }, (_, index) => read(`--chart-${index + 1}`, '#4F6BFF')),
    text: read('--text-primary', '#161B26'),
    secondary: read('--text-secondary', '#5F697B'),
    tertiary: read('--text-tertiary', '#8D96A8'),
    line: read('--border', '#E5E9F0'),
    surface: read('--surface', '#FFFFFF'),
  };
}

function baseOption(palette) {
  return {
    color: palette.color,
    textStyle: { fontFamily: 'Inter, system-ui, "PingFang SC", "Microsoft YaHei", sans-serif' },
    animationDuration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 320,
    animationDurationUpdate: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 220,
    tooltip: { backgroundColor: palette.surface, borderColor: palette.line, borderWidth: 1, padding: [8, 12], textStyle: { color: palette.text, fontSize: 12 } },
  };
}

function cartesian(spec, palette) {
  const categories = spec.encoding?.x || [];
  const series = spec.encoding?.series || [];
  const type = BASE_TYPE[spec.type] || 'bar';
  return {
    tooltip: { ...baseOption(palette).tooltip, trigger: type === 'scatter' ? 'item' : 'axis', valueFormatter: formatValue },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: palette.secondary, fontSize: 12 } },
    grid: { left: 8, right: 16, top: 20, bottom: 44, containLabel: true },
    xAxis: {
      type: 'category',
      data: categories,
      axisLabel: { color: palette.secondary, fontSize: 12, hideOverlap: true },
      axisLine: { lineStyle: { color: palette.line } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: palette.secondary, fontSize: 12, formatter: (value) => formatValue(value) },
      splitLine: { lineStyle: { color: palette.line, type: 'dashed' } },
    },
    series: series.map((item, index) => ({
      name: item.name,
      type,
      data: type === 'scatter' && series.length > 1
        ? item.values.map((value, i) => [categories[i], value])
        : item.values,
      smooth: ['line', 'area', 'stacked_area', 'sparkline'].includes(spec.type),
      areaStyle: ['area', 'stacked_area'].includes(spec.type) ? { opacity: 0.16 } : undefined,
      stack: ['stacked_bar', 'stacked_area'].includes(spec.type) ? 'total' : undefined,
      barMaxWidth: 34,
      symbolSize: spec.type === 'bubble' ? 12 : 6,
      itemStyle: { borderRadius: type === 'bar' ? [4, 4, 0, 0] : 0 },
      emphasis: { focus: 'series' },
    })),
  };
}

function composition(spec, palette) {
  const series = spec.encoding?.series?.[0] || { values: [] };
  const data = (spec.encoding?.x || []).map((name, index) => ({
    name: String(name),
    value: Number(series.values[index]) || 0,
  }));
  const theme = baseOption(palette);
  if (['treemap', 'sunburst'].includes(spec.type)) {
    return { tooltip: theme.tooltip, series: [{ type: spec.type, data, radius: ['12%', '80%'], label: { color: palette.text, fontSize: 12 } }] };
  }
  if (['funnel'].includes(spec.type)) {
    return { tooltip: { ...theme.tooltip, valueFormatter: formatValue }, series: [{ type: 'funnel', sort: 'descending', data, left: '10%', width: '80%', label: { color: palette.text } }] };
  }
  if (spec.type === 'gauge') {
    return { series: [{ type: 'gauge', progress: { show: true }, detail: { valueAnimation: true, formatter: (value) => formatValue(value) }, data: [{ value: Number(series.values[0]) || 0, name: series.name }] }] };
  }
  return {
    tooltip: { ...theme.tooltip, trigger: 'item', valueFormatter: formatValue },
    legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: palette.secondary, fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: spec.type === 'donut' ? ['46%', '70%'] : ['0%', '68%'],
      roseType: spec.type === 'rose' ? 'radius' : undefined,
      data,
      label: { color: palette.secondary, fontSize: 12 },
      itemStyle: { borderColor: palette.surface, borderWidth: 1 },
    }],
  };
}

function heatmap(spec, palette) {
  const records = spec.records || [];
  const columns = spec.columns || [];
  if (columns.length < 3) return null;
  const xs = [...new Set(records.map(row => String(row[columns[0]])))];
  const ys = [...new Set(records.map(row => String(row[columns[1]])))];
  const points = records.map(row => [xs.indexOf(String(row[columns[0]])), ys.indexOf(String(row[columns[1]])), Number(row[columns[2]]) || 0]);
  return {
    tooltip: { ...baseOption(palette).tooltip, position: 'top' },
    grid: { left: 8, right: 16, top: 20, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: xs, splitArea: { show: true }, axisLabel: { color: palette.secondary, fontSize: 11 } },
    yAxis: { type: 'category', data: ys, splitArea: { show: true }, axisLabel: { color: palette.secondary, fontSize: 11 } },
    visualMap: { min: 0, max: Math.max(1, ...points.map(point => point[2])), calculable: true, orient: 'horizontal', left: 'center', bottom: 0, textStyle: { color: palette.secondary, fontSize: 11 } },
    series: [{ type: 'heatmap', data: points, label: { show: points.length < 60, color: palette.text, fontSize: 11 } }],
  };
}

function boxplot(spec, palette) {
  const series = spec.encoding?.series || [];
  if (!series.length) return null;
  const quantile = (sorted, p) => {
    const index = (sorted.length - 1) * p;
    const low = Math.floor(index);
    const high = Math.ceil(index);
    return sorted[low] + (sorted[high] - sorted[low]) * (index - low);
  };
  const data = series.map(item => {
    const values = item.values.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    return values.length
      ? [values[0], quantile(values, 0.25), quantile(values, 0.5), quantile(values, 0.75), values.at(-1)]
      : [0, 0, 0, 0, 0];
  });
  return {
    tooltip: baseOption(palette).tooltip,
    grid: { left: 8, right: 16, top: 20, bottom: 40, containLabel: true },
    xAxis: { type: 'category', data: series.map(item => item.name), axisLabel: { color: palette.secondary, fontSize: 12 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: palette.line, type: 'dashed' } }, axisLabel: { color: palette.secondary, fontSize: 12 } },
    series: [{ type: 'boxplot', data }],
  };
}

/** 数值展示：大数收敛成万/亿，避免坐标轴被一串零淹没。 */
export function formatValue(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '—');
  const abs = Math.abs(number);
  if (abs >= 1e8) return `${(number / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${(number / 1e4).toFixed(abs >= 1e6 ? 0 : 1)} 万`;
  if (Number.isInteger(number)) return number.toLocaleString('zh-CN');
  return number.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

export function buildOption(spec) {
  if (!spec) return null;
  const palette = tokens();
  if (spec.option) {
    return { ...baseOption(palette), ...JSON.parse(JSON.stringify(spec.option)) };
  }
  if (spec.type === 'heatmap') return heatmap(spec, palette);
  if (spec.type === 'boxplot' || spec.type === 'violin') return boxplot(spec, palette);
  if (COMPOSITION.has(spec.type)) return composition(spec, palette);
  return cartesian(spec, palette);
}

export const ChartView = {
  name: 'ChartView',
  props: { spec: Object },
  emits: ['empty'],
  setup(props) {
    const root = ref(null);
    let chart = null;

    const render = () => {
      if (!root.value || !window.echarts) return;
      const option = buildOption(props.spec);
      if (!option) {
        emitEmpty();
        return;
      }
      chart = chart || window.echarts.init(root.value);
      chart.setOption(option, true);
      chart.resize();
    };
    const emitEmpty = () => {
      if (root.value) {
        root.value.innerHTML = '';
        root.value.classList.add('is-empty');
      }
    };
    const onResize = () => chart?.resize();

    onMounted(() => {
      nextTick(render);
      window.addEventListener('resize', onResize);
      const observer = new ResizeObserver(onResize);
      if (root.value) observer.observe(root.value);
      onBeforeUnmount(() => observer.disconnect());
    });
    onBeforeUnmount(() => {
      window.removeEventListener('resize', onResize);
      chart?.dispose();
      chart = null;
    });
    watch(() => props.spec, () => nextTick(render), { deep: true });

    return { root };
  },
  template: `<div ref="root" class="chart-card__canvas" role="img" :aria-label="spec?.title || '数据图表'"></div>`,
};

export const ChartCard = {
  name: 'ChartCard',
  components: { ChartView },
  props: { spec: Object, note: String, tall: Boolean },
  template: `
    <figure class="chart-card" :class="{ 'chart-card--tall': tall }">
      <figcaption class="chart-card__head">
        <span class="chart-card__title">{{ spec?.title || '图表' }}</span>
        <span v-if="note" class="chart-card__note">{{ note }}</span>
      </figcaption>
      <ChartView :spec="spec" />
    </figure>`,
};
