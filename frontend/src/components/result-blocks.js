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
  components: { Icon },
  props: { kpi: Object },
  computed: {
    display() {
      const value = this.kpi?.value;
      return typeof value === 'number' ? formatValue(value) : String(value ?? '—');
    },
    deltaClass() { return TREND(this.kpi.delta); },
    deltaIcon() { return ARROW(this.kpi.delta); },
    deltaDisplay() {
      const value = this.kpi?.delta;
      if (value === null || value === '' || !Number.isFinite(Number(value))) return '—';
      const number = Number(value) * 100;
      return `${number > 0 ? '+' : ''}${number.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%`;
    },
  },
  template: `
    <div class="kpi">
      <div class="kpi__label">{{ kpi.label }}</div>
      <div class="kpi__value" :title="String(kpi.value ?? '—')">{{ display }}</div>
      <div v-if="kpi.delta !== undefined" class="kpi__meta" :class="deltaClass">
        <Icon v-if="deltaIcon" :name="deltaIcon" :size="13" />
        {{ kpi.deltaLabel || '环比' }} {{ deltaDisplay }}
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
      return { fact: '事实', inference: '推测', risk: '风险', suggestion: '建议' }[this.item?.type] || '发现';
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
  data() { return { downloading: false }; },
  computed: {
    iconName() { return iconForCategory(this.file.category, this.file.kind); },
    meta() {
      return [formatSize(this.file.size_bytes || this.file.size), formatDate(this.file.created_at)]
        .filter(Boolean).join(' · ');
    },
  },
  methods: {
    async download() {
      if (this.downloading) return;
      this.downloading = true;
      try {
        await actions.download(this.file.download_url, this.file.filename || this.file.title);
        toast('文件已开始下载', '下载');
      } catch (error) {
        toast(error.message, '下载失败', 'error');
      } finally {
        this.downloading = false;
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
      <button v-if="canDownload" class="btn btn--sm" :disabled="downloading" @click="download"><Icon name="download" :size="14" />{{ downloading ? '下载中…' : '下载' }}</button>
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
  emits: ['preview', 'source', 'table-page'],
  data() { return { moreCharts: false, moreInsights: false }; },
  setup(props) {
    const summaryHtml = computed(() => renderMarkdown(props.payload?.summary || ''));
    const charts = computed(() => (props.payload?.charts || [])
      .filter(item => item.available !== false));
    const kpis = computed(() => (props.payload?.kpis || []));
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
      return items;
    });
    const basis = computed(() => (props.payload?.evidence_refs || []).map(String));
    const limitations = computed(() => [...new Set((props.payload?.limitations || props.payload?.report?.limitations || [])
      .filter(Boolean).map(String))]);
    const hasContent = computed(() => Boolean(summaryHtml.value || kpis.value.length || charts.value.length
      || props.tables.length || props.artifacts.length || insights.value.length));
    return { summaryHtml, charts, kpis, insights, basis, limitations, hasContent };
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
      <EmptyState v-if="!hasContent" icon="chart2" title="本次分析没有可展示的结论或数据" text="可查看来源与执行过程，确认数据范围后重新提问。" />
      <div v-if="summaryHtml" class="markdown result__conclusion" v-html="summaryHtml"></div>

      <div v-if="kpis.length" class="kpi-grid">
        <KpiCard v-for="kpi in kpis" :key="kpi.id" :kpi="kpi" />
      </div>

      <ChartCard v-for="chart in moreCharts ? charts : charts.slice(0, 2)" :key="chart.id" :spec="chart" />
      <button v-if="charts.length > 2" class="btn btn--sm" :aria-expanded="moreCharts" @click="moreCharts = !moreCharts">
        {{ moreCharts ? '收起更多图表' : '查看其余 ' + (charts.length - 2) + ' 张图表' }}
      </button>

      <div v-for="table in tables" :key="table.id" class="result-table">
        <div class="result-table__head">
          <b>{{ table.title || '数据明细' }}</b>
          <span class="small muted grow" role="status">
            {{ table.total === null || table.total === undefined ? '共 ' + (table.rows || []).length + ' 条' : '共 ' + table.total + ' 条' }}
            <template v-if="table.total > (table.rows || []).length"> · 当前 {{ (table.offset || 0) + 1 }}–{{ (table.offset || 0) + (table.rows || []).length }} 条</template>
          </span>
          <template v-if="table.paginated && table.total > (table.limit || 50)">
            <button class="btn btn--sm" :aria-label="(table.title || '数据明细') + '上一页'"
                    :disabled="table.loading || !(table.offset > 0)"
                    @click="$emit('table-page', { table, offset: Math.max(0, table.offset - (table.limit || 50)) })">上一页</button>
            <button class="btn btn--sm" :aria-label="(table.title || '数据明细') + '下一页'"
                    :disabled="table.loading || (table.offset || 0) + (table.rows || []).length >= table.total"
                    @click="$emit('table-page', { table, offset: (table.offset || 0) + (table.limit || 50) })">下一页</button>
          </template>
        </div>
        <p v-if="table.sourceTotal > table.total || ['partial', 'truncated', 'sampled'].includes(table.completeness)" class="result-table__status small muted">
          本次返回 {{ table.total }} 条{{ table.sourceTotal > table.total ? '，原始结果共 ' + table.sourceTotal + ' 条' : '' }}；当前明细为部分数据。
        </p>
        <p v-if="!table.paginated && table.total > (table.rows || []).length" class="result-table__status small muted">当前为前 {{ (table.rows || []).length }} 条预览；完整明细请使用导出 Excel。</p>
        <p v-if="table.error" class="result-table__status small" role="alert">{{ table.error }}
          <button class="btn btn--sm" @click="$emit('table-page', { table, offset: table.failedOffset ?? table.offset ?? 0 })">重试加载</button>
        </p>
        <p v-if="table.loading" class="result-table__status small muted" role="status">正在加载明细…</p>
        <DataTable :rows="table.rows || []" :columns="table.columns || []" :max-height="'380px'" />
      </div>

      <div v-if="insights.length" class="stack" style="display:flex;flex-direction:column;gap:8px">
        <InsightItem v-for="(item, index) in moreInsights ? insights : insights.slice(0, 6)" :key="index" :item="item" />
        <button v-if="insights.length > 6" class="btn btn--sm" :aria-expanded="moreInsights" @click="moreInsights = !moreInsights">
          {{ moreInsights ? '收起更多发现' : '查看其余 ' + (insights.length - 6) + ' 条发现与建议' }}
        </button>
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
      <details v-if="limitations.length" class="result__limits">
        <summary>结果说明与限制 · {{ limitations.length }} 项</summary>
        <ul><li v-for="item in limitations" :key="item">{{ item }}</li></ul>
      </details>
    </section>`,
};

/* ------------------------------------------------------------------ 澄清卡片 */

export const ClarificationCard = {
  name: 'ClarificationCard',
  components: { Icon },
  props: { contract: Object, busy: Boolean },
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
    dateError() {
      if (this.choices.period !== '自定义') return '';
      if (!this.choices.start || !this.choices.end) return '请选择开始和结束日期';
      return this.choices.start > this.choices.end ? '结束日期不能早于开始日期' : '';
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
        <p class="small muted" style="margin-bottom:8px">当前口径：{{ period }}</p>
        <div class="chip-group">
          <button v-for="item in ['本月','上月','最近3个月','最近12个月','自定义']" :key="item"
                  class="chip" :class="{ active: choices.period === item }"
                  :disabled="busy" @click="choices.period = item">{{ item }}</button>
        </div>
        <div v-if="choices.period === '自定义'" class="row" style="margin-top:8px">
          <label class="field grow"><span>开始日期</span><input v-model="choices.start" :disabled="busy" type="date" class="input" /></label>
          <label class="field grow"><span>结束日期</span><input v-model="choices.end" :disabled="busy" type="date" class="input" /></label>
        </div>
        <p v-if="dateError" class="small" style="color:var(--danger);margin-top:6px" role="status">{{ dateError }}</p>
      </div>

      <div class="clarify__group">
        <div class="clarify__label">重点关注</div>
        <div class="chip-group">
          <button v-for="item in ['整体表现','趋势变化','城市差异','品类表现','下降原因']" :key="item"
                  class="chip" :class="{ active: choices.focus === item }"
                  :disabled="busy" @click="choices.focus = item">{{ item }}</button>
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
        <button class="btn btn--sm" :disabled="busy" @click="$emit('cancel')">重新描述</button>
        <button class="btn btn--primary" :disabled="busy || !!dateError" @click="$emit('submit', { ...choices })">
          <Icon name="play" :size="15" />{{ busy ? '正在提交…' : '开始分析' }}
        </button>
      </div>
    </div>`,
};

export { KpiCard, InsightItem, FileCard };
