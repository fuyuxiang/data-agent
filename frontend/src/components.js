const { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } = Vue;

export const Icon = {
  props: { name: String, size: { type: Number, default: 18 } },
  setup(props) {
    const paths = {
      chat: '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3 1.7-5A8 8 0 1 1 21 15Z"/><path d="M8 11h8M8 15h5"/>',
      database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/>',
      book: '<path d="M4 5a3 3 0 0 1 3-3h5v18H7a3 3 0 0 0-3 3V5Z"/><path d="M20 5a3 3 0 0 0-3-3h-5v18h5a3 3 0 0 1 3 3V5Z"/>',
      workflow: '<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="15" width="6" height="6" rx="1"/><path d="M9 6h4a4 4 0 0 1 4 4v5M15 18h-4a4 4 0 0 1-4-4V9"/>',
      dashboard: '<rect x="3" y="3" width="7" height="8" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="15" width="7" height="6" rx="1"/>',
      map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Z"/><path d="M9 3v15M15 6v15"/>',
      settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.9 2.9-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.1h-4v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.9-2.9.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3v-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.9-2.9.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.5V3h4v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.9 2.9-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.1v4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
      plus: '<path d="M12 5v14M5 12h14"/>',
      upload: '<path d="M12 16V3M7 8l5-5 5 5M4 15v5h16v-5"/>',
      play: '<path d="m8 5 11 7-11 7V5Z"/>',
      stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
      search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
      close: '<path d="M6 6l12 12M18 6 6 18"/>',
      chevron: '<path d="m9 18 6-6-6-6"/>',
      sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
      moon: '<path d="M21 13a9 9 0 1 1-10-10 7 7 0 0 0 10 10Z"/>',
      download: '<path d="M12 3v13M7 11l5 5 5-5M4 21h16"/>',
      refresh: '<path d="M20 6v5h-5M4 18v-5h5"/><path d="M18.5 10A7 7 0 0 0 6 6l-2 5M5.5 14A7 7 0 0 0 18 18l2-5"/>',
      check: '<path d="m5 12 4 4L19 6"/>',
      warning: '<path d="M12 3 2 21h20L12 3Z"/><path d="M12 9v5M12 18h.01"/>',
      table: '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 9h18M8 4v16M15 4v16"/>',
      chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
      brain: '<path d="M9.5 4A3.5 3.5 0 0 0 6 7.5v.7A3.5 3.5 0 0 0 5 15v.5A3.5 3.5 0 0 0 11 18V6a2 2 0 0 0-1.5-2ZM14.5 4A3.5 3.5 0 0 1 18 7.5v.7A3.5 3.5 0 0 1 19 15v.5A3.5 3.5 0 0 1 13 18V6a2 2 0 0 1 1.5-2Z"/>',
      bolt: '<path d="m13 2-9 12h7l-1 8 9-12h-7l1-8Z"/>',
      users: '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M16 4a4 4 0 0 1 0 8M17 15a6 6 0 0 1 5 6"/>',
    };
    return () => Vue.h('svg', {
      viewBox: '0 0 24 24', width: props.size, height: props.size,
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      innerHTML: paths[props.name] || paths.bolt,
    });
  },
};

export const Modal = {
  components: { Icon },
  props: { open: Boolean, title: String, wide: Boolean },
  emits: ['close'],
  template: `
    <Teleport to="body">
      <Transition name="fade">
        <div v-if="open" class="modal-backdrop" @mousedown.self="$emit('close')">
          <section class="modal" :class="{ 'modal--wide': wide }" role="dialog" aria-modal="true" :aria-label="title">
            <header class="modal__header"><div><span class="eyebrow">工作台</span><h2>{{ title }}</h2></div><button class="icon-button" @click="$emit('close')" aria-label="关闭"><Icon name="close" /></button></header>
            <div class="modal__body"><slot /></div>
            <footer v-if="$slots.footer" class="modal__footer"><slot name="footer" /></footer>
          </section>
        </div>
      </Transition>
    </Teleport>`,
};

export const EmptyState = {
  components: { Icon },
  props: { icon: { default: 'table' }, title: String, text: String },
  template: `<div class="empty-state"><span class="empty-state__icon"><Icon :name="icon" :size="25" /></span><h3>{{ title }}</h3><p>{{ text }}</p><slot /></div>`,
};

export const StatusPill = {
  props: { status: String, label: String },
  computed: {
    text() {
      const map = { ready: '就绪', active: '使用中', configured: '已配置', connected: '已连接', running: '运行中', queued: '排队中', waiting_input: '待确认', waiting_job: '远程作业中', waiting_approval: '待审批', paused: '已暂停', cancelling: '取消中', finished: '已结束', completed: '已完成', failed: '失败', cancelled: '已取消', draft: '草稿', published: '已发布', online: '在线', received: '已接收' };
      return this.label || map[this.status] || this.status || '未知';
    },
  },
  template: `<span class="status-pill" :data-status="status"><i></i>{{ text }}</span>`,
};

export const DataTable = {
  props: { rows: { type: Array, default: () => [] }, columns: { type: Array, default: () => [] }, maxHeight: { default: '420px' } },
  computed: {
    shownColumns() { return this.columns.length ? this.columns : (this.rows[0] ? Object.keys(this.rows[0]) : []); },
  },
  methods: {
    format(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return '—';
      if (typeof value === 'object') return JSON.stringify(value);
      return String(value);
    },
  },
  template: `<div class="data-table-wrap" :style="{ maxHeight }"><table class="data-table"><thead><tr><th v-for="column in shownColumns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in rows" :key="index"><td v-for="column in shownColumns" :key="column" :title="format(row[column])">{{ format(row[column]) }}</td></tr></tbody></table></div>`,
};

function genericSeries(spec) {
  const categories = spec.encoding?.x || [];
  const series = spec.encoding?.series || [];
  const records = spec.records || [];
  const columns = spec.columns || [];
  if (spec.type === 'heatmap' && columns.length >= 3) {
    const xValues = [...new Set(records.map(row => String(row[columns[0]])))];
    const yValues = [...new Set(records.map(row => String(row[columns[1]])))];
    const values = records.map(row => [xValues.indexOf(String(row[columns[0]])), yValues.indexOf(String(row[columns[1]])), Number(row[columns[2]]) || 0]);
    const max = Math.max(1, ...values.map(item => item[2]));
    return { tooltip:{}, grid:{left:70,right:30,top:25,bottom:55}, xAxis:{type:'category',data:xValues}, yAxis:{type:'category',data:yValues}, visualMap:{min:0,max,calculable:true,orient:'horizontal',left:'center',bottom:0}, series:[{type:'heatmap',data:values,label:{show:values.length<80}}] };
  }
  if (['network', 'chord'].includes(spec.type) && columns.length >= 2) {
    const nodeNames = [...new Set(records.flatMap(row => [String(row[columns[0]]), String(row[columns[1]])]))];
    return { tooltip:{}, series:[{type:'graph',layout:'force',roam:true,label:{show:true},force:{repulsion:150,edgeLength:90},data:nodeNames.map(name=>({name})),links:records.map(row=>({source:String(row[columns[0]]),target:String(row[columns[1]]),value:Number(row[columns[2]])||1,lineStyle:{width:Math.min(8,1+(Number(row[columns[2]])||1)/10)}}))}] };
  }
  if (spec.type === 'sankey' && columns.length >= 2) {
    const names = [...new Set(records.flatMap(row => [String(row[columns[0]]), String(row[columns[1]])]))];
    return { tooltip:{trigger:'item'}, series:[{type:'sankey',data:names.map(name=>({name})),links:records.map(row=>({source:String(row[columns[0]]),target:String(row[columns[1]]),value:Number(row[columns[2]])||1})),emphasis:{focus:'adjacency'},lineStyle:{color:'gradient',curveness:.5}}] };
  }
  if (spec.type === 'parallel' && columns.length >= 2) {
    const axes = columns.slice(0,8).map((column,index)=>({dim:index,name:column,type:records.every(row=>Number.isFinite(Number(row[column])))?'value':'category',data:records.every(row=>Number.isFinite(Number(row[column])))?undefined:[...new Set(records.map(row=>String(row[column])))].slice(0,80)}));
    return { parallelAxis:axes, parallel:{left:55,right:45,top:30,bottom:30}, series:[{type:'parallel',lineStyle:{width:1,opacity:.45},data:records.slice(0,300).map(row=>axes.map(axis=>row[axis.name]))}] };
  }
  if (spec.type === 'radar' && series.length) {
    const indicators = series.map(item=>({name:item.name,max:Math.max(1,...item.values.map(value=>Math.abs(Number(value)||0)))}));
    const rows = Math.min(categories.length,6);
    return { tooltip:{},legend:{bottom:0},radar:{indicator:indicators,radius:'68%'},series:[{type:'radar',data:Array.from({length:rows},(_,index)=>({name:String(categories[index]),value:series.map(item=>Number(item.values[index])||0)}))}] };
  }
  if (['boxplot', 'violin'].includes(spec.type) && series.length) {
    const quantile = (sorted,p)=>{const index=(sorted.length-1)*p;const low=Math.floor(index);const high=Math.ceil(index);return sorted[low]+(sorted[high]-sorted[low])*(index-low)};
    const data=series.map(item=>{const values=item.values.map(Number).filter(Number.isFinite).sort((a,b)=>a-b);return values.length?[values[0],quantile(values,.25),quantile(values,.5),quantile(values,.75),values.at(-1)]:[0,0,0,0,0]});
    return {tooltip:{trigger:'item'},grid:{left:55,right:25,top:25,bottom:45},xAxis:{type:'category',data:series.map(item=>item.name)},yAxis:{type:'value',splitArea:{show:true}},series:[{type:'boxplot',data}]} ;
  }
  if (spec.type === 'calendar' && records.length >= 2) {
    const start = String(records[0][columns[0]]).slice(0,10); const year = start.slice(0,4) || new Date().getFullYear();
    return {tooltip:{},visualMap:{min:0,max:Math.max(1,...records.map(row=>Number(row[columns[1]])||0)),calculable:true,orient:'horizontal',left:'center',bottom:0},calendar:{range:year,cellSize:['auto',16],top:25,left:45,right:20},series:[{type:'heatmap',coordinateSystem:'calendar',data:records.map(row=>[String(row[columns[0]]).slice(0,10),Number(row[columns[1]])||0])}]};
  }
  const typeMap = {
    bar: 'bar', grouped_bar: 'bar', stacked_bar: 'bar', diverging_bar: 'bar', waterfall: 'bar', bullet: 'bar', pyramid: 'bar',
    line: 'line', area: 'line', stacked_area: 'line', sparkline: 'line', slope: 'line', bump: 'line', horizon: 'line', cycle: 'line', connected_scatter: 'line', circular_line: 'line',
    scatter: 'scatter', bubble: 'scatter', dot: 'scatter', beeswarm: 'scatter', density: 'line', ridgeline: 'line', violin: 'boxplot', boxplot: 'boxplot', error_bar: 'scatter',
  };
  const baseType = typeMap[spec.type] || 'bar';
  return {
    tooltip: { trigger: baseType === 'scatter' ? 'item' : 'axis' },
    legend: { bottom: 0, textStyle: { color: '#667085' } },
    grid: { left: 48, right: 24, top: 26, bottom: 58, containLabel: true },
    xAxis: { type: 'category', data: categories, axisLabel: { color: '#667085', hideOverlap: true }, axisLine: { lineStyle: { color: '#d9e0ea' } } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#e8edf4' } } },
    series: series.map((item, index) => ({
      name: item.name,
      type: baseType,
      data: baseType === 'scatter' && series.length > 1 ? item.values.map((value, i) => [categories[i], value]) : item.values,
      smooth: ['line', 'area', 'stacked_area', 'sparkline'].includes(spec.type),
      areaStyle: ['area', 'stacked_area', 'horizon'].includes(spec.type) ? { opacity: 0.18 } : undefined,
      stack: ['stacked_bar', 'stacked_area'].includes(spec.type) ? 'total' : undefined,
      barMaxWidth: 42,
      symbolSize: spec.type === 'bubble' ? 14 : 8,
      itemStyle: { borderRadius: baseType === 'bar' ? [5, 5, 0, 0] : 0 },
    })),
  };
}

function compositionOption(spec) {
  const series = spec.encoding?.series?.[0] || { values: [] };
  const data = (spec.encoding?.x || []).map((name, index) => ({ name: String(name), value: series.values[index] }));
  if (['treemap', 'sunburst'].includes(spec.type)) {
    return { tooltip: {}, series: [{ type: spec.type, data, radius: ['10%', '82%'], label: { color: '#24334a' } }] };
  }
  if (spec.type === 'funnel' || spec.type === 'pyramid') {
    return { tooltip: {}, series: [{ type: 'funnel', sort: spec.type === 'pyramid' ? 'ascending' : 'descending', data, left: '12%', width: '76%', label: { color: '#24334a' } }] };
  }
  if (spec.type === 'gauge') {
    return { series: [{ type: 'gauge', progress: { show: true }, detail: { valueAnimation: true }, data: [{ value: series.values[0] || 0, name: series.name }] }] };
  }
  return { tooltip: { trigger: 'item' }, legend: { bottom: 0 }, series: [{ type: 'pie', radius: spec.type === 'donut' ? ['42%', '70%'] : ['0%', '70%'], roseType: spec.type === 'rose' ? 'area' : undefined, data, label: { color: '#4d5f78' } }] };
}

export const ChartView = {
  props: { spec: Object },
  setup(props) {
    const root = ref(null);
    let chart;
    const render = () => {
      if (!root.value || !props.spec || !window.echarts) return;
      chart ||= echarts.init(root.value);
      const composition = ['pie', 'donut', 'rose', 'treemap', 'sunburst', 'funnel', 'pyramid', 'gauge', 'waffle', 'marimekko'].includes(props.spec.type);
      const option = props.spec.option
        ? JSON.parse(JSON.stringify(props.spec.option))
        : (composition ? compositionOption(props.spec) : genericSeries(props.spec));
      option.color = ['#167c80', '#e59b4c', '#4058b4', '#9a5bc4', '#42a46f', '#df6b62'];
      option.animationDuration = 450;
      chart.setOption(option, true);
      chart.resize();
    };
    const resize = () => chart?.resize();
    onMounted(() => { nextTick(render); window.addEventListener('resize', resize); });
    onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose(); });
    watch(() => props.spec, () => nextTick(render), { deep: true });
    return { root };
  },
  template: `<div ref="root" class="chart-view" role="img" :aria-label="spec?.title || '数据图表'"></div>`,
};

export const ToastStack = {
  props: { items: Array },
  components: { Icon },
  template: `<Teleport to="body"><div class="toast-stack" aria-live="polite"><TransitionGroup name="toast"><div v-for="item in items" :key="item.id" class="toast" :data-tone="item.tone"><Icon :name="item.tone === 'error' ? 'warning' : 'check'"/><div><strong>{{ item.title }}</strong><p v-if="item.message">{{ item.message }}</p></div></div></TransitionGroup></div></Teleport>`,
};

export function renderMarkdown(value) {
  const raw = window.marked ? window.marked.parse(value || '') : String(value || '').replace(/\n/g, '<br>');
  return window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw;
}
