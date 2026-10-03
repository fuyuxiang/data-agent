/**
 * 结构化分析结果。
 *
 * 数擎最核心的产品差异：Data Agent 的回答不是一整段 Markdown，
 * 而是「结论 / KPI / 图表 / 表格 / 发现 / 文件 / 依据」的有序组合。
 */

const { computed } = Vue;

import { ChartCard, formatValue } from './chart.js';
import { Icon, iconForCategory } from './icons.js';
import { DataTable, EmptyState } from './ui.js';
import { actions, formatDate, formatSize, toast } from '../store.js';

/** Markdown 渲染：优先 marked + DOMPurify，两者缺一不可。 */
export function renderMarkdown(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (!window.marked || !window.DOMPurify) {
    // 渲染管线不完整时只输出纯文本，绝不把未经消毒的 HTML 交给 DOM。
    const escaped = text
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    return `<p class="markdown">${escaped.replaceAll('\n', '<br>')}</p>`;
  }
  const html = window.marked.parse(text);
  return window.DOMPurify.sanitize(
    html.replace(/<table>/g, '<div class="markdown-table-wrap"><table>')
      .replace(/<\/table>/g, '</table></div>'),
    { ADD_ATTR: ['target', 'rel'] },
  );
}

const TREND = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number) || Math.abs(number) < 0.0005) return '';
  return number > 0 ? 'trend-up' : 'trend-down';
};

const ARROW = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number) || Math.abs(number) < 0.0005) return '';
  return number > 0 ? 'arrowUp' : 'arrowDown';
};

const KpiCard = {
  name: 'KpiCard',
  props: { kpi: Object },
  computed: {
    display() {
      const value = this.kpi?.value;
      return typeof value === 'number' ? formatValue(value) : String(value ?? '—');
    },
    deltaClass() { return TREND(this.kpi.delta); },
    deltaIcon() { return ARROW(this.kpi.delta); },
  },
  template: `
    <div class="kpi">
      <div class="kpi__label">{{ kpi.label }}</div>
      <div class="kpi__value">{{ display }}</div>
      <div v-if="kpi.delta !== undefined" class="kpi__meta" :class="deltaClass">
        <Icon :name="deltaIcon" :size="13" />
        {{ kpi.deltaLabel || '环比' }} {{ Math.abs(kpi.delta * 100).toFixed(1) }}%
      </div>
    </div>`,
};

const InsightItem = {
  name: 'InsightItem',
  components: { Icon },
  props: { item: Object },
  computed: {
    tone() {
      const type = String(this.item?.type || 'fact');
      if (type === 'risk') return 'risk';
      if (type === 'inference' || type === 'suggestion') return 'inference';
      return 'fact';
    },
    label() {
      return { fact: '事实', inference: '推测', risk: '风险', suggestion: '建议' }[this.tone] || '发现';
    },
  },
  template: `
    <div class="insight" :class="'insight--' + tone">
      <div class="grow">
        <div class="insight__label">{{ label }}</div>
        <p class="insight__text">{{ item.text }}</p>
      </div>
    </div>`,
};

const FileCard = {
  name: 'FileCard',
  components: { Icon },
  props: { file: Object, canDownload: { type: Boolean, default: true } },
  emits: ['preview', 'source'],
  computed: {
    iconName() { return iconForCategory(this.file.category, this.file.kind); },
    meta() {
      return [formatSize(this.file.size_bytes || this.file.size), formatDate(this.file.created_at)]
        .filter(Boolean).join(' · ');
    },
  },
  methods: {
    async download() {
      try {
        await actions.download(this.file.download_url, this.file.filename || this.file.title);
      } catch (error) {
        toast(error.message, '下载失败', 'error');
      }
    },
  },
  template: `
    <div class="file-card">
      <span class="file-card__icon"><Icon :name="iconName" :size="19" /></span>
      <div class="grow">
        <div class="file-card__title truncate">{{ file.title }}</div>
        <div class="file-card__meta">{{ meta }}</div>
      </div>
      <button v-if="file.previewable" class="btn btn--sm" @click="$emit('preview', file)">
        <Icon name="eye" :size="14" />预览
      </button>
      <button v-if="canDownload" class="btn btn--sm" @click="download"><Icon name="download" :size="14" />下载</button>
    </div>`,
};

export const ResultView = {
  name: 'ResultView',
  components: { ChartCard, DataTable, EmptyState, FileCard, Icon, InsightItem, KpiCard },
  props: {
    payload: { type: Object, default: null },
    artifacts: { type: Array, default: () => [] },
    tables: { type: Array, default: () => [] },
    loading: Boolean,
    canExport: { type: Boolean, default: true },
  },
  emits: ['preview', 'source'],
  setup(props) {
    const summaryHtml = computed(() => renderMarkdown(props.payload?.summary || ''));
    const charts = computed(() => (props.payload?.charts || [])
      .filter(item => item.available !== false).slice(0, 2));
    const kpis = computed(() => (props.payload?.kpis || []).slice(0, 4));
    const insights = computed(() => {
      const report = props.payload?.report || {};
      const items = [];
      (report.attribution || []).forEach(value => items.push(
        typeof value === 'string' ? { type: 'inference', text: value } : { type: 'fact', ...value },
      ));
      ['short_term', 'medium_term', 'long_term'].forEach(bucket => {
        (report.recommendations?.[bucket] || []).forEach(value => {
          items.push({ type: 'suggestion', text: value });
        });
      });
      return items.slice(0, 6);
    });
    const basis = computed(() => (props.payload?.evidence_refs || []).map(String));
    return { summaryHtml, charts, kpis, insights, basis };
  },
  methods: {
    formatValue,
  },
  template: `
    <section v-if="loading" class="stack">
      <div class="skeleton" style="height:72px"></div>
      <div class="kpi-grid">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:96px"></div>
      </div>
      <div class="skeleton" style="height:300px"></div>
    </section>

    <section v-else-if="!payload" class="stack">
      <EmptyState icon="chart2" title="还没有可展示的结果" text="分析完成后，结论、指标、图表和依据会出现在这里。" />
    </section>

    <section v-else class="result">
      <div v-if="summaryHtml" class="markdown result__conclusion" v-html="summaryHtml"></div>

      <div v-if="kpis.length" class="kpi-grid">
        <KpiCard v-for="kpi in kpis" :key="kpi.id" :kpi="kpi" />
      </div>

      <ChartCard v-for="chart in charts" :key="chart.id" :spec="chart" />

      <div v-for="table in tables" :key="table.id">
        <DataTable :rows="table.rows || []" :columns="table.columns || []" :max-height="'380px'" />
      </div>

      <div v-if="insights.length" class="stack" style="display:flex;flex-direction:column;gap:8px">
        <InsightItem v-for="(item, index) in insights" :key="index" :item="item" />
      </div>

      <div v-if="artifacts.length" class="stack" style="display:flex;flex-direction:column;gap:8px">
        <FileCard v-for="file in artifacts" :key="file.id" :file="file" :can-download="canExport" @preview="$emit('preview', $event)" />
      </div>

      <div v-if="basis.length" class="basis">
        <Icon name="shield" :size="15" />
        <span>依据</span>
        <button v-for="ref in basis.slice(0, 4)" :key="ref" class="basis__link" @click="$emit('source', ref)">
          <span class="mono">{{ ref }}</span>
          <Icon name="chevronRight" :size="12" />
        </button>
        <span v-if="basis.length > 4" class="faint">+{{ basis.length - 4 }}</span>
      </div>
    </section>`,
};

/* ------------------------------------------------------------------ 澄清卡片 */

export const ClarificationCard = {
  name: 'ClarificationCard',
  components: { Icon },
  props: { contract: Object },
  emits: ['submit', 'cancel'],
  data() {
    return {
      choices: {
        period: null,
        focus: null,
        start: '',
        end: '',
      },
      expanded: false,
    };
  },
  computed: {
    objective() {
      return this.contract?.payload?.objective || '这个问题';
    },
    period() {
      const range = this.contract?.payload?.time_range || {};
      if (range.start || range.end) {
        return [range.start, range.end].filter(Boolean).join(' 至 ');
      }
      return '本月';
    },
  },
  template: `
    <div class="clarify">
      <div class="row" style="align-items:flex-start">
        <Icon name="compass" :size="18" class="muted" />
        <div class="grow">
          <h3 class="clarify__title">我理解你想分析{{ objective }}</h3>
          <p class="clarify__desc">确认下面几点，我就能按这个口径开始分析。改错了也不影响，随时可以追问。</p>
        </div>
      </div>

      <div class="clarify__group">
        <div class="clarify__label">分析时间</div>
        <div class="chip-group">
          <button v-for="item in ['本月','上月','最近3个月','最近12个月','自定义']" :key="item"
                  class="chip" :class="{ active: choices.period === item }"
                  @click="choices.period = item">{{ item }}</button>
        </div>
        <div v-if="choices.period === '自定义'" class="row" style="margin-top:8px">
          <label class="field grow"><span>开始日期</span><input v-model="choices.start" type="date" class="input" /></label>
          <label class="field grow"><span>结束日期</span><input v-model="choices.end" type="date" class="input" /></label>
        </div>
      </div>

      <div class="clarify__group">
        <div class="clarify__label">重点关注</div>
        <div class="chip-group">
          <button v-for="item in ['整体表现','趋势变化','城市差异','品类表现','下降原因']" :key="item"
                  class="chip" :class="{ active: choices.focus === item }"
                  @click="choices.focus = item">{{ item }}</button>
        </div>
      </div>

      <div v-if="expanded" class="clarify__group">
        <div class="clarify__label">统计范围</div>
        <pre class="mono">{{ JSON.stringify(contract?.payload || {}, null, 2) }}</pre>
      </div>

      <div class="clarify__foot">
        <button class="btn btn--ghost btn--sm" @click="expanded = !expanded">
          {{ expanded ? '收起口径' : '查看口径' }}
        </button>
        <button class="btn btn--sm" @click="$emit('cancel')">重新描述</button>
        <button class="btn btn--primary" @click="$emit('submit', choices)">
          <Icon name="play" :size="15" />开始分析
        </button>
      </div>
    </div>`,
};

export { KpiCard, InsightItem, FileCard };
