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

const numericValue = value => value == null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value);

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
    tooltip: { confine: true, backgroundColor: palette.surface, borderColor: palette.line, borderWidth: 1, padding: [8, 12], textStyle: { color: palette.text, fontSize: 12 }, extraCssText: 'max-width:320px;white-space:normal;overflow-wrap:anywhere;' },
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
    value: numericValue(series.values[index]),
  }));
  const theme = baseOption(palette);
  if (['treemap', 'sunburst'].includes(spec.type)) {
    return { tooltip: theme.tooltip, series: [{ type: spec.type, data, radius: ['12%', '80%'], label: { color: palette.text, fontSize: 12 } }] };
  }
  if (['funnel'].includes(spec.type)) {
    return { tooltip: { ...theme.tooltip, valueFormatter: formatValue }, series: [{ type: 'funnel', sort: 'descending', data, left: '10%', width: '80%', label: { color: palette.text } }] };
  }
  if (spec.type === 'gauge') {
    const value = numericValue(series.values[0]);
    return { series: [{ type: 'gauge', min: Math.min(0, value ?? 0), progress: { show: true }, detail: { valueAnimation: true, formatter: (value) => formatValue(value) }, data: [{ value, name: series.name }] }] };
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
  const points = records.filter(row => row[columns[2]] != null && row[columns[2]] !== '' && Number.isFinite(Number(row[columns[2]])))
    .map(row => [xs.indexOf(String(row[columns[0]])), ys.indexOf(String(row[columns[1]])), Number(row[columns[2]])]);
  return {
    tooltip: { ...baseOption(palette).tooltip, position: 'top' },
    grid: { left: 8, right: 16, top: 20, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: xs, splitArea: { show: true }, axisLabel: { color: palette.secondary, fontSize: 11 } },
    yAxis: { type: 'category', data: ys, splitArea: { show: true }, axisLabel: { color: palette.secondary, fontSize: 11 } },
    visualMap: { min: Math.min(0, ...points.map(point => point[2])), max: Math.max(1, ...points.map(point => point[2])), calculable: true, orient: 'horizontal', left: 'center', bottom: 0, textStyle: { color: palette.secondary, fontSize: 11 } },
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
    const values = item.values.filter(value => value != null && value !== '').map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    return values.length
      ? [values[0], quantile(values, 0.25), quantile(values, 0.5), quantile(values, 0.75), values.at(-1)]
      : [null, null, null, null, null];
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
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '—');
  const abs = Math.abs(number);
  if (abs >= 1e8) return `${(number / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${(number / 1e4).toFixed(abs >= 1e6 ? 0 : 1)} 万`;
  if (Number.isInteger(number)) return number.toLocaleString('zh-CN');
  return number.toLocaleString('zh-CN', { maximumSignificantDigits: 8 });
}

/** 保留服务端图形和数据，只补充缺失的主题、布局与长标签约束。 */
function readableOption(option, palette) {
  const output = { ...baseOption(palette), ...option };
  output.tooltip = { ...baseOption(palette).tooltip, valueFormatter: formatValue, ...option.tooltip };
  const series = Array.isArray(output.series) ? output.series : output.series ? [output.series] : [];
  const mapAxes = (axes) => {
    const styleAxis = axis => ({
      ...axis,
      axisLabel: {
        color: palette.secondary, fontSize: 12, hideOverlap: true,
        ...(axis.type === 'category' ? {
          width: 104, overflow: 'truncate', interval: (axis.data?.length || 0) <= 12 ? 0 : 'auto',
        } : { formatter: formatValue }),
        ...axis.axisLabel,
      },
      axisLine: { ...axis.axisLine, lineStyle: { color: palette.line, ...axis.axisLine?.lineStyle } },
      axisTick: { show: false, ...axis.axisTick },
    });
    return Array.isArray(axes) ? axes.map(styleAxis) : styleAxis(axes);
  };
  if (output.xAxis || output.yAxis) {
    if (output.xAxis) output.xAxis = mapAxes(output.xAxis);
    if (output.yAxis) output.yAxis = mapAxes(output.yAxis);
    const gridDefaults = { left: 12, right: 20, top: 28, bottom: 48, containLabel: true };
    output.grid = Array.isArray(output.grid)
      ? output.grid.map(grid => ({ ...gridDefaults, ...grid })) : { ...gridDefaults, ...output.grid };
    const xAxis = Array.isArray(output.xAxis) ? output.xAxis[0] : output.xAxis;
    if (xAxis?.type === 'category' && xAxis.data?.length > 12 && !output.dataZoom && !Array.isArray(output.grid)) {
      output.dataZoom = [{ type: 'slider', height: 18, bottom: 16, startValue: 0, endValue: 11 }, { type: 'inside' }];
    }
    output.tooltip = { ...baseOption(palette).tooltip, trigger: series.some(item => item.type === 'scatter') ? 'item' : 'axis', valueFormatter: formatValue, ...output.tooltip };
  }
  output.series = series.map(item => {
    if (item.type === 'pie') return {
      radius: ['0%', '62%'], center: ['50%', output.title?.show !== false && output.title?.text ? '54%' : '44%'], stillShowZeroSum: false, ...item,
      label: { color: palette.secondary, fontSize: 12, width: 110, overflow: 'truncate', ...item.label },
      labelLayout: { hideOverlap: true, ...item.labelLayout },
    };
    return item;
  });
  if (output.legend || series.length > 1 || series.some(item => item.type === 'pie')) {
    const styleLegend = legend => ({
      type: 'scroll', bottom: 0, left: 'center', itemWidth: 10, itemHeight: 10,
      tooltip: { show: true },
      textStyle: { color: palette.secondary, fontSize: 12, width: 140, overflow: 'truncate' },
      ...legend,
    });
    output.legend = Array.isArray(output.legend) ? output.legend.map(styleLegend) : styleLegend(output.legend);
  }
  if (output.grid && !Array.isArray(output.grid)) {
    // 底部组件各占一行，既不遮住缩放手柄，也不把图例挤到标题上。
    const zoom = (Array.isArray(output.dataZoom) ? output.dataZoom : [output.dataZoom])
      .find(item => item?.type === 'slider' && item.show !== false && item.orient !== 'vertical' && item.top == null);
    let bottom = zoom ? (Number(zoom.bottom) || 0) + (Number(zoom.height) || 18) + 18 : 0;
    const legends = Array.isArray(output.legend) ? output.legend : [output.legend];
    const hasLegendData = series.some(item => item.name || (item.type === 'pie' && item.data?.some(point => point?.name)));
    for (const legend of legends) {
      if (!legend || legend.show === false || legend.top != null || legend.orient === 'vertical' || !hasLegendData) continue;
      legend.bottom = Math.max(Number(legend.bottom) || 0, bottom + 4);
      bottom = legend.bottom + 32;
    }
    const visualMaps = Array.isArray(output.visualMap) ? output.visualMap : [output.visualMap];
    for (const visualMap of visualMaps) {
      if (!visualMap || visualMap.show === false || visualMap.top != null || visualMap.orient !== 'horizontal') continue;
      visualMap.bottom = Math.max(Number(visualMap.bottom) || 0, bottom + 4);
      bottom = visualMap.bottom + 64;
    }
    output.grid.bottom = Math.max(Number(output.grid.bottom) || 0, bottom + 12);
    const title = Array.isArray(output.title) ? output.title[0] : output.title;
    if (title?.text && title.show !== false && title.bottom == null) {
      output.grid.top = Math.max(Number(output.grid.top) || 0, 56);
    }
  }
  return output;
}

export function buildOption(spec) {
  if (!spec) return null;
  const palette = tokens();
  if (spec.option) {
    const option = JSON.parse(JSON.stringify(spec.option));
    const list = Array.isArray(option.series) ? option.series : [option.series];
    if (spec.type === 'heatmap' && Array.isArray(spec.records) && spec.columns?.length >= 3
        && list.length === 1 && list[0]?.type === 'heatmap'
        && (!list[0].coordinateSystem || list[0].coordinateSystem === 'cartesian2d')) {
      // 旧保存规格可能已把 null 写成 0；有原始记录时恢复其缺失语义和色域。
      const data = heatmap(spec, palette);
      list[0].data = data.series[0].data;
      const range = { min: data.visualMap.min, max: data.visualMap.max };
      option.visualMap = Array.isArray(option.visualMap)
        ? option.visualMap.map(item => ({ ...item, ...range })) : { ...option.visualMap, ...range };
    }
    return readableOption(option, palette);
  }
  const option = spec.type === 'heatmap' ? heatmap(spec, palette)
    : spec.type === 'boxplot' || spec.type === 'violin' ? boxplot(spec, palette)
      : COMPOSITION.has(spec.type) ? composition(spec, palette) : cartesian(spec, palette);
  return option ? readableOption(option, palette) : null;
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
      root.value.classList.remove('is-empty');
      chart = chart || window.echarts.init(root.value);
      chart.setOption(option, true);
      chart.resize();
    };
    const emitEmpty = () => {
      if (root.value) {
        chart?.dispose();
        chart = null;
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
  computed: {
    emptyReason() {
      if (this.spec?.option?.dataset) return '';
      if (this.spec?.type === 'heatmap' && Array.isArray(this.spec.records) && this.spec.columns?.length >= 3) {
        return this.spec?.records?.some(row => numericValue(row[this.spec.columns?.[2]]) !== null) ? '' : '暂无可绘制的数据';
      }
      const series = this.spec?.option?.series || this.spec?.encoding?.series || [];
      const list = Array.isArray(series) ? series : [series];
      const values = list.flatMap(item => item.data || item.values || []);
      if (!values.length) return '暂无可绘制的数据';
      const numbers = values.flatMap(item => {
        const value = item && typeof item === 'object' && !Array.isArray(item) ? item.value : item;
        return Array.isArray(value) ? value : [value];
      }).map(numericValue).filter(value => value !== null);
      if (!numbers.length && !list.some(item => ['graph', 'sankey', 'treemap', 'sunburst'].includes(item.type))) return '暂无可绘制的数据';
      if (['pie', 'donut', 'rose'].includes(this.spec?.type)
        && numbers.length && numbers.every(value => value === 0)) {
        return '各类目数值均为 0，暂无构成比例';
      }
      return '';
    },
  },
  template: `
    <figure class="chart-card" :class="{ 'chart-card--tall': tall }">
      <figcaption class="chart-card__head">
        <span class="chart-card__title">{{ spec?.title || '图表' }}</span>
        <span v-if="note" class="chart-card__note">{{ note }}</span>
      </figcaption>
      <div v-if="emptyReason" class="empty" style="padding:40px 16px"><p class="small muted">{{ emptyReason }}</p></div>
      <ChartView v-else :spec="spec" />
    </figure>`,
};
