/**
 * 指标中心：让 AI 正确理解企业业务指标。
 *
 * 普通业务用户可搜索、查看定义、查看口径、试算；有权限的用户额外能新建、
 * 编辑、停用。不做两套指标中心——同一套页面，权限决定按钮是否出现。
 */

import { ChartCard, formatValue } from '../components/chart.js';
import { Icon } from '../components/icons.js';
import { DataTable, EmptyState, Modal, SearchInput, Tabs, Status } from '../components/ui.js';
import { actions, canAdmin, state, toast } from '../store.js';
import { navigate } from '../router.js';

const METRIC_TYPES = {
  atomic: '原子指标',
  derived: '派生指标',
  composite: '复合指标',
};

export const MetricsView = {
  name: 'MetricsView',
  components: { ChartCard, DataTable, EmptyState, Icon, Modal, SearchInput, Status, Tabs },
  props: { admin: Boolean },
  setup() {
    return { canAdmin, navigate, state, toast };
  },
  data() {
    return {
      tab: 'metrics',
      metrics: [],
      models: [],
      sources: [],
      query: '',
      statusFilter: '',
      selected: null,
      advanced: false,
      trial: { metric: '', groupBy: [], filters: [], limit: 200, timeRange: {} },
      trialResult: null,
      trialPlan: null,
      trialing: false,
      trialRequest: 0,
      trialError: '',
      editor: null,
      modelEditor: null,
      modelDeleteTarget: null,
      metricDeleteTarget: null,
      modelDeleteReferences: null,
      metricDeleteReferences: null,
      deleteReferencesLoading: false,
      deleteReferencesError: '',
      modelTables: [],
      modelColumns: [],
      modelTablesLoading: false,
      modelTablesError: '',
      modelTablesRequest: 0,
      saving: false,
      deleting: false,
      changingMetrics: [],
      loading: true,
      loadError: '',
    };
  },
  async mounted() {
    await this.load();
  },
  beforeUnmount() {
    this.trialRequest += 1;
    this.modelTablesRequest += 1;
  },
  watch: {
    trial: {
      deep: true,
      handler() {
        this.invalidateTrial();
      },
    },
  },
  methods: {
    invalidateTrial() {
      this.trialRequest += 1;
      this.trialing = false;
      this.trialResult = null;
      this.trialPlan = null;
      this.trialError = '';
    },
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const [metrics, models, sources] = await Promise.all([
          actions.get('/api/semantic/metrics'),
          actions.get('/api/semantic/models'),
          actions.get('/api/sources'),
        ]);
        this.metrics = metrics.items || [];
        this.models = models.items || [];
        this.sources = sources.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    typeLabel(item) {
      return METRIC_TYPES[item.metric_type] || '指标';
    },
    unit(item) {
      return item.unit || '';
    },
    select(metric) {
      if (!metric) return;
      this.selected = metric;
      this.advanced = false;
      this.invalidateTrial();
      this.trial = { metric: metric.name, groupBy: [], filters: [], limit: 200, timeRange: {} };
    },
    /** 试算走受治理的指标编译器：管理员预览到的就是 Agent 之后会产出的。 */
    async runTrial() {
      if (this.trialing || !this.canTrial || !this.selected) return;
      if (!Number.isInteger(this.trial.limit) || this.trial.limit < 1 || this.trial.limit > 5000) {
        this.trialError = '行数上限应为 1 到 5000 的整数';
        return;
      }
      if (this.trial.timeRange.start && this.trial.timeRange.end
          && this.trial.timeRange.start > this.trial.timeRange.end) {
        this.trialError = '开始日期不能晚于结束日期';
        return;
      }
      const requestId = ++this.trialRequest;
      this.trialing = true;
      this.trialResult = null;
      this.trialPlan = null;
      this.trialError = '';
      try {
        const response = await actions.post('/api/admin/metric-trial', {
          metric: this.trial.metric,
          group_by: this.trial.groupBy,
          filters: this.trial.filters.filter(item => item.dimension),
          time_range: this.trial.timeRange,
          limit: this.trial.limit,
        });
        if (requestId === this.trialRequest) {
          this.trialResult = response.result;
          this.trialPlan = response.plan;
        }
      } catch (error) {
        if (requestId === this.trialRequest) this.trialError = error.message;
      } finally {
        if (requestId === this.trialRequest) this.trialing = false;
      }
    },
    addFilter() {
      this.trial.filters.push({ dimension: '', op: '=', value: '' });
    },
    newMetric() {
      if (!this.models.length) {
        this.tab = 'models';
        this.newModel();
        return;
      }
      this.editor = {
        name: '', label: '', description: '', model_id: this.models[0].id,
        metric_type: 'atomic', measure: '', expression: '', unit: '', format: ',.2f',
        aliases: '', business_owner: '', technical_owner: '', description_detail: '',
      };
    },
    async saveMetric() {
      if (this.saving || !this.editor) return;
      if (!this.editor.label || !this.editor.name || !this.editor.model_id
          || !(this.editor.metric_type === 'atomic' ? this.editor.measure : this.editor.expression)) {
        toast('请填写业务名称、技术名称、语义模型和计算方式', '指标信息不完整', 'error');
        return;
      }
      this.saving = true;
      try {
        const body = {
          ...this.editor,
          aliases: (typeof this.editor.aliases === 'string' ? this.editor.aliases.split(/[,，]/) : this.editor.aliases)
            .map(item => item.trim()).filter(Boolean),
          description: this.editor.description,
          status: 'draft',
        };
        const response = this.editor.id
          ? await actions.patch(`/api/semantic/metrics/${this.editor.id}`, body)
          : await actions.post('/api/semantic/metrics', body);
        this.metrics = this.metrics.filter(item => item.id !== response.item.id).concat(response.item);
        if (this.selected?.id === response.item.id) this.selected = response.item;
        this.editor = null;
        toast('指标已保存为草稿', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    editMetric(metric) {
      this.editor = { ...metric, aliases: (metric.aliases || []).join(', ') };
    },
    canEditModel(model) {
      return state.workspaceRole === 'owner'
        || !this.metrics.some(metric => metric.model_id === model.id && metric.status === 'approved');
    },
    async openModelDelete(model) {
      this.modelDeleteTarget = model;
      this.modelDeleteReferences = null;
      this.deleteReferencesError = '';
      await this.loadModelDeleteReferences();
    },
    closeModelDelete() {
      if (!this.deleting) this.modelDeleteTarget = null;
    },
    async loadModelDeleteReferences() {
      const model = this.modelDeleteTarget;
      if (!model) return;
      this.deleteReferencesLoading = true;
      this.deleteReferencesError = '';
      try {
        const response = await actions.get(`/api/semantic/models/${model.id}/references`);
        if (this.modelDeleteTarget?.id === model.id) this.modelDeleteReferences = response.metrics || [];
      } catch (error) {
        if (this.modelDeleteTarget?.id === model.id) this.deleteReferencesError = error.message;
      } finally {
        if (this.modelDeleteTarget?.id === model.id) this.deleteReferencesLoading = false;
      }
    },
    showModelMetric() {
      const reference = this.modelDeleteReferences?.[0];
      this.tab = 'metrics';
      this.query = '';
      this.statusFilter = '';
      const metric = this.metrics.find(item => item.id === reference?.id);
      if (metric) this.select(metric);
      this.modelDeleteTarget = null;
    },
    async deleteModel() {
      const model = this.modelDeleteTarget;
      if (!model || this.deleting || this.deleteReferencesLoading || !this.modelDeleteReferences
          || this.modelDeleteReferences.length) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/semantic/models/${model.id}`);
        this.models = this.models.filter(item => item.id !== model.id);
        this.modelDeleteTarget = null;
        this.modelDeleteReferences = null;
        if (this.modelEditor?.id === model.id) this.modelEditor = null;
        toast('语义模型已删除', '完成');
      } catch (error) {
        toast(error.message, '删除语义模型失败', 'error');
        await this.loadModelDeleteReferences();
      } finally {
        this.deleting = false;
      }
    },
    async openMetricDelete(metric) {
      this.metricDeleteTarget = metric;
      this.metricDeleteReferences = null;
      this.deleteReferencesError = '';
      await this.loadMetricDeleteReferences();
    },
    closeMetricDelete() {
      if (!this.deleting) this.metricDeleteTarget = null;
    },
    async loadMetricDeleteReferences() {
      const metric = this.metricDeleteTarget;
      if (!metric) return;
      this.deleteReferencesLoading = true;
      this.deleteReferencesError = '';
      try {
        const response = await actions.get(`/api/semantic/metrics/${metric.id}/references`);
        if (this.metricDeleteTarget?.id === metric.id) {
          this.metricDeleteReferences = {
            metrics: response.metrics || [], agents: response.agents || [],
          };
        }
      } catch (error) {
        if (this.metricDeleteTarget?.id === metric.id) this.deleteReferencesError = error.message;
      } finally {
        if (this.metricDeleteTarget?.id === metric.id) this.deleteReferencesLoading = false;
      }
    },
    async deleteMetric() {
      const metric = this.metricDeleteTarget;
      if (!metric || this.deleting || this.deleteReferencesLoading || !this.metricDeleteReferences
          || this.metricDeleteReferences.metrics.length || this.metricDeleteReferences.agents.length) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/semantic/metrics/${metric.id}`);
        this.metrics = this.metrics.filter(item => item.id !== metric.id);
        if (this.selected?.id === metric.id) this.selected = null;
        if (this.editor?.id === metric.id) this.editor = null;
        this.metricDeleteTarget = null;
        this.metricDeleteReferences = null;
        toast('指标已删除', '完成');
      } catch (error) {
        toast(error.message, '删除指标失败', 'error');
        await this.loadMetricDeleteReferences();
      } finally {
        this.deleting = false;
      }
    },
    newModel() {
      const source = this.sources.find(item => item.status === 'ready');
      if (!source) {
        toast('请先在数据管理中添加可用的数据源', '无法建立语义模型', 'error');
        return;
      }
      this.modelEditor = {
        name: '', description: '', source_id: source.id, table: '', grain: '',
        dimensions: [], measures: [], entities: [], default_time_dimension: '', enabled: true,
      };
      this.loadModelTables();
    },
    editModel(model) {
      this.modelEditor = JSON.parse(JSON.stringify(model));
      this.loadModelTables();
    },
    async loadModelTables() {
      if (!this.modelEditor?.source_id) return;
      const editor = this.modelEditor;
      const sourceId = editor.source_id;
      const requestId = ++this.modelTablesRequest;
      this.modelTables = [];
      this.modelColumns = [];
      this.modelTablesLoading = true;
      this.modelTablesError = '';
      try {
        const response = await actions.get(`/api/sources/${sourceId}/schema`);
        if (requestId !== this.modelTablesRequest || this.modelEditor !== editor) return;
        this.modelTables = response.schema?.tables || [];
        if (!this.modelTables.find(item => item.name === this.modelEditor.table)) {
          this.modelEditor.table = this.modelTables[0]?.name || '';
        }
        this.updateModelColumns();
      } catch (error) {
        if (requestId === this.modelTablesRequest && this.modelEditor === editor) this.modelTablesError = error.message;
      } finally {
        if (requestId === this.modelTablesRequest) this.modelTablesLoading = false;
      }
    },
    updateModelColumns() {
      this.modelColumns = this.modelTables.find(item => item.name === this.modelEditor?.table)?.columns || [];
    },
    addModelField(kind) {
      if (this.modelTablesLoading || !this.modelColumns.length) return;
      const column = this.modelColumns.find(item => !this.modelEditor[kind].some(field => field.column === item.name));
      const name = column?.name || '';
      this.modelEditor[kind].push({
        name: /^[\w\u4e00-\u9fff]+$/.test(name) ? name : '',
        column: name, label: name,
        ...(kind === 'dimensions' ? { type: 'categorical' } : { aggregation: 'sum' }),
      });
    },
    async saveModel() {
      if (this.saving || this.modelTablesLoading) return;
      if (!this.modelEditor?.name || !this.modelEditor.table || !this.modelEditor.measures.length) {
        toast('请填写模型名称、数据表，并至少添加一个度量', '模型信息不完整', 'error');
        return;
      }
      this.saving = true;
      try {
        const response = this.modelEditor.id
          ? await actions.patch(`/api/semantic/models/${this.modelEditor.id}`, this.modelEditor)
          : await actions.post('/api/semantic/models', this.modelEditor);
        this.models = this.models.filter(item => item.id !== response.item.id).concat(response.item);
        this.modelEditor = null;
        toast('语义模型已保存，可以继续新建指标', '完成');
      } catch (error) {
        toast(error.message, '保存语义模型失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    async publish(metric) {
      if (this.changingMetrics.includes(metric.id)) return;
      this.changingMetrics.push(metric.id);
      try {
        await actions.patch(`/api/semantic/metrics/${metric.id}`, { status: 'approved' });
        metric.status = 'approved';
        toast('指标已发布，分析将优先使用它', '完成');
      } catch (error) {
        toast(error.message, '发布失败', 'error');
      } finally {
        this.changingMetrics = this.changingMetrics.filter(id => id !== metric.id);
      }
    },
    async disable(metric) {
      if (this.changingMetrics.includes(metric.id)) return;
      this.changingMetrics.push(metric.id);
      try {
        await actions.patch(`/api/semantic/metrics/${metric.id}`, { status: 'deprecated' });
        metric.status = 'deprecated';
        toast('指标已停用', '完成');
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      } finally {
        this.changingMetrics = this.changingMetrics.filter(id => id !== metric.id);
      }
    },
    askWith(metric) {
      navigate('workbench', { ask: `${metric.label || metric.name}是多少？` });
    },
    trialChart() {
      if (!this.trialResult) return null;
      const columns = this.trialResult.columns || [];
      const labels = columns.filter(name => name !== this.trial.metric && this.trial.groupBy.includes(name));
      const value = columns.find(name => name === this.trial.metric);
      const rows = this.trialResult.data || [];
      if (!labels.length || !value || !rows.length) return null;
      const valueLabel = this.selected.label || value;
      return {
        id: 'trial',
        title: `${valueLabel}${this.selected.unit ? '（' + this.selected.unit + '）' : ''} · ${labels.map(name => this.supportedDimensions.find(item => item.name === name)?.label || name).join(' / ')}`,
        type: labels.length === 1 && /日期|时间|月份|年月|date|time|month|year/i.test(labels[0]) ? 'line' : 'bar',
        available: true,
        encoding: {
          x: rows.slice(0, 20).map(row => labels.map(name => row[name] ?? '—').join(' / ')),
          series: [{ name: valueLabel, values: rows.slice(0, 20).map(row => row[value] === null || row[value] === undefined || row[value] === '' ? null : Number(row[value])) }],
        },
      };
    },
  },
  template: `
    <div :class="admin ? '' : 'view--page'">
      <div class="view__inner">
        <header class="page-head">
          <div class="grow">
            <h1 class="page-head__title">指标中心</h1>
            <p class="page-head__desc">
              正式指标是 AI 理解业务的口径来源。有指标时分析优先走指标定义，
              没有对应指标时，AI 会结合数据进行探索分析。
            </p>
          </div>
          <div v-if="admin" class="page-head__actions">
            <button class="btn btn--sm" @click="newMetric">
              <Icon name="plus" :size="14" />新建指标
            </button>
          </div>
        </header>

        <div v-if="loadError" class="card stack"><p class="small" role="alert" style="color:var(--danger)">{{ loadError }}</p><button class="btn btn--sm" @click="load">重新加载指标中心</button></div>
        <template v-else>
        <Tabs v-model="tab" :items="[
          { key: 'metrics', label: '指标', count: metrics.length },
          { key: 'dimensions', label: '维度', count: dimensions.length },
          { key: 'models', label: '语义模型', count: models.length },
          { key: 'terms', label: '业务术语' },
        ]" />

        <div v-if="tab === 'metrics'" class="metric-layout" style="margin-top:18px">
          <div>
            <div class="toolbar">
              <SearchInput v-model="query" placeholder="搜索指标名称、口径或同义词" />
              <div class="segmented">
                <button :class="{ active: !statusFilter }" @click="statusFilter = ''">全部</button>
                <button :class="{ active: statusFilter === 'approved' }" @click="statusFilter = 'approved'">可用</button>
                <button :class="{ active: statusFilter === 'draft' }" @click="statusFilter = 'draft'">草稿</button>
                <button :class="{ active: statusFilter === 'deprecated' }" @click="statusFilter = 'deprecated'">已停用</button>
              </div>
            </div>

            <div v-if="loading" class="stack">
              <div v-for="index in 4" :key="index" class="skeleton" style="height:64px"></div>
            </div>
            <EmptyState v-else-if="!filtered.length" icon="metric" :title="query || statusFilter ? '没有匹配的指标' : '还没有指标'"
                        :text="query || statusFilter ? '调整关键词或状态筛选后重试。' : '指标定义了业务口径，让不同人问同一个问题时得到同一个答案。'" />

            <div v-else class="metric-list">
              <article v-for="item in filtered" :key="item.id" class="card card--interactive metric-item"
                       :class="{ 'metric-item--selected': selected?.id === item.id }"
                       style="display:flex;align-items:center;gap:12px;text-align:left;width:100%"
                       role="button" tabindex="0" @click="select(item)"
                       @keydown.enter.self="select(item)" @keydown.space.self.prevent="select(item)">
                <span class="agent-card__mark" style="width:34px;height:34px">
                  <Icon name="metric" :size="17" />
                </span>
                <span class="grow">
                  <span class="row row--between">
                    <b>{{ item.label || item.name }}</b>
                    <Status :status="item.status" />
                  </span>
                  <span class="small muted truncate" style="display:block">
                    {{ typeLabel(item) }} · {{ item.expression || item.measure || '—' }}
                    <template v-if="item.unit"> · {{ item.unit }}</template>
                  </span>
                  <span class="xs faint truncate" style="display:block;margin-top:2px">
                    {{ item.description || '暂无业务定义' }}
                  </span>
                </span>
                <button v-if="state.workspaceRole === 'owner'" class="btn btn--sm" style="color:var(--danger)"
                        @click.stop="openMetricDelete(item)">删除指标</button>
              </article>
            </div>
          </div>

          <aside>
            <div v-if="!selected" class="card">
              <EmptyState icon="search" title="选择一个指标"
                          text="查看它的业务定义、计算方式、支持维度，并直接试算。" />
            </div>

            <div v-else class="stack">
              <div class="card">
                <div class="card__head">
                  <div class="grow">
                    <h2 class="card__title">{{ selected.label || selected.name }}</h2>
                    <p class="card__hint">{{ typeLabel(selected) }} · {{ selectedModel?.name }}</p>
                  </div>
                  <Status :status="selected.status" />
                </div>

                <dl class="definition">
                  <dt>业务定义</dt>
                  <dd>{{ selected.description || '尚未填写' }}</dd>
                  <dt>计算方式</dt>
                  <dd class="mono">{{ selected.expression || selected.measure || '—' }}</dd>
                  <dt>时间口径</dt>
                  <dd>{{ selected.time_semantics || selected.grain || '—' }}</dd>
                  <dt>单位</dt>
                  <dd>{{ selected.unit || '无' }}</dd>
                  <dt>支持维度</dt>
                  <dd>
                    <span v-if="!supportedDimensions.length" class="faint">该指标暂无可用分组维度</span>
                    <span v-for="item in supportedDimensions" :key="item.name" class="badge" style="margin-right:4px">
                      {{ item.label }}
                    </span>
                  </dd>
                  <dt>同义词</dt>
                  <dd>
                    <span v-if="!(selected.aliases || []).length" class="faint">—</span>
                    <span v-for="alias in selected.aliases" :key="alias" class="badge" style="margin-right:4px">
                      {{ alias }}
                    </span>
                  </dd>
                  <dt>数据来源</dt>
                  <dd>{{ sources.find(s => s.id === selectedModel?.source_id)?.name || '—' }}
                    <span class="xs faint">· {{ selectedModel?.table }}</span></dd>
                </dl>

                <button class="advanced-toggle" @click="advanced = !advanced">
                  <Icon :name="advanced ? 'chevronDown' : 'chevronRight'" :size="14" />
                  高级设置（版本、负责人、权限、验证）
                </button>
                <dl v-if="advanced" class="definition" style="margin-top:12px">
                  <dt>版本</dt><dd>v{{ selected.version }}</dd>
                  <dt>业务负责人</dt><dd>{{ selected.business_owner || '未指定' }}</dd>
                  <dt>技术负责人</dt><dd>{{ selected.technical_owner || '未指定' }}</dd>
                  <dt>指标 ID</dt><dd class="mono xs">{{ selected.id }}</dd>
                  <dt>语义模型</dt><dd>{{ selectedModel?.name }} v{{ selectedModel?.version }}</dd>
                </dl>

                <div class="row" style="margin-top:16px">
                  <button v-if="canAnalyze && selected.status === 'approved'" class="btn btn--primary btn--sm" @click="askWith(selected)">
                    <Icon name="chat" :size="14" />直接提问
                  </button>
                  <template v-if="canAdmin">
                    <button v-if="selected.status !== 'approved' || state.workspaceRole === 'owner'"
                            class="btn btn--sm" @click="editMetric(selected)">编辑</button>
                    <button v-if="selected.status === 'draft' && state.workspaceRole === 'owner'" class="btn btn--sm" :disabled="changingMetrics.includes(selected.id)" @click="publish(selected)">{{ changingMetrics.includes(selected.id) ? '发布中…' : '发布' }}</button>
                    <button v-if="selected.status === 'approved' && state.workspaceRole === 'owner'" class="btn btn--sm" :disabled="changingMetrics.includes(selected.id)" @click="disable(selected)">{{ changingMetrics.includes(selected.id) ? '停用中…' : '停用' }}</button>
                    <button v-if="state.workspaceRole === 'owner'" class="btn btn--sm"
                            style="color:var(--danger)" @click="openMetricDelete(selected)">删除指标</button>
                  </template>
                </div>
              </div>

              <div class="card">
                <div class="card__head">
                  <div class="grow">
                    <h2 class="card__title">试算</h2>
                    <p class="card__hint">按当前指标口径试算，结果与分析中使用的口径保持一致。</p>
                  </div>
                </div>

                <div class="stack" style="display:flex;flex-direction:column;gap:12px">
                  <div v-if="supportedDimensions.length">
                    <div class="small muted" style="margin-bottom:6px">按哪些维度查看</div>
                    <div class="tag-row">
                      <label v-for="item in supportedDimensions" :key="item.name" class="checkbox">
                        <input type="checkbox" :value="item.name" v-model="trial.groupBy" />
                        <span class="small">{{ item.label }}</span>
                      </label>
                    </div>
                  </div>

                  <div>
                    <div class="row row--between" style="margin-bottom:6px">
                      <span class="small muted">筛选条件</span>
                      <button class="btn btn--sm btn--ghost" @click="addFilter"><Icon name="plus" :size="13" />添加</button>
                    </div>
                    <div v-if="!trial.filters.length" class="xs faint">暂不筛选</div>
                    <div v-for="(filter, index) in trial.filters" :key="index" class="row" style="margin-bottom:6px">
                      <select v-model="filter.dimension" class="select input--sm" style="flex:1">
                        <option value="">选择维度</option>
                        <option v-for="item in supportedDimensions" :key="item.name" :value="item.name">{{ item.label }}</option>
                      </select>
                      <select v-model="filter.op" class="select input--sm" style="width:96px">
                        <option value="=">=</option><option value="!=">≠</option>
                        <option value=">">&gt;</option><option value=">=">≥</option>
                        <option value="<">&lt;</option><option value="<=">≤</option>
                      </select>
                      <input v-model.trim="filter.value" class="input input--sm" style="flex:1" placeholder="值" />
                      <button class="icon-btn icon-btn--danger" aria-label="移除筛选"
                              @click="trial.filters.splice(index, 1)"><Icon name="close" :size="14" /></button>
                    </div>
                  </div>

                  <div class="row">
                    <label class="field grow">
                      <span class="xs">开始日期</span>
                      <input type="date" v-model="trial.timeRange.start" class="input input--sm" />
                    </label>
                    <label class="field grow">
                      <span class="xs">结束日期</span>
                      <input type="date" v-model="trial.timeRange.end" class="input input--sm" />
                    </label>
                    <label class="field" style="width:88px">
                      <span class="xs">行数上限</span>
                      <input type="number" v-model.number="trial.limit" min="1" max="5000" class="input input--sm" />
                    </label>
                  </div>

                  <p v-if="!canTrial" class="small muted">{{ canAnalyze ? '草稿指标仅管理员可试算。' : '当前为只读权限，可查看口径；试算需要分析权限。' }}</p>
                  <p v-if="trialError" class="small" role="alert" style="color:var(--danger)">{{ trialError }}。调整条件后可重新试算。</p>
                  <button v-if="canTrial" class="btn btn--primary" :disabled="trialing" @click="runTrial">
                    <Icon name="play" :size="15" />{{ trialing ? '试算中…' : '运行试算' }}
                  </button>
                </div>

                <div v-if="trialResult" class="stack" style="margin-top:16px;display:flex;flex-direction:column;gap:12px">
                  <p class="small muted" role="status">{{ selected.label || selected.name }} · 返回 {{ (trialResult.data || []).length }} 行<span v-if="selected.unit"> · 单位：{{ selected.unit }}</span><span v-if="trialResult.truncated"> · 已达到行数上限，部分数据未展示</span></p>
                  <div v-if="trialChart()" class="small muted">
                    <ChartCard :spec="trialChart()" />
                    <p v-if="(trialResult.data || []).length > 20" class="xs faint">图表展示前 20 组，全部返回数据见下方表格。</p>
                  </div>
                  <DataTable :rows="trialResult.data || []" :columns="(trialResult.columns || []).map(c => ({ key: c, label: c }))"
                             max-height="280px" />
                  <details v-if="trialPlan">
                    <summary class="small muted" style="cursor:pointer">查看 SQL 与口径</summary>
                    <pre style="margin-top:8px">{{ trialPlan.sql }}</pre>
                    <p class="xs faint" style="margin-top:6px">
                      {{ trialPlan.model?.name }} v{{ trialPlan.model?.version }} ·
                      {{ trialPlan.metric?.label }} v{{ trialPlan.metric?.version }}
                    </p>
                  </details>
                </div>
              </div>
            </div>
          </aside>
        </div>

        <div v-else-if="tab === 'models'" style="margin-top:18px">
          <div v-if="admin" class="row" style="margin-bottom:12px">
            <button class="btn btn--primary btn--sm" @click="newModel"><Icon name="plus" :size="14" />新建语义模型</button>
          </div>
          <div class="grid grid--2">
          <article v-for="model in models" :key="model.id" class="card">
            <div class="card__head">
              <div class="grow">
                <h2 class="card__title">{{ model.name }}</h2>
                <p class="card__hint">{{ model.table }} · {{ model.grain || '未声明粒度' }}</p>
              </div>
              <Status :status="model.enabled === false ? 'disabled' : 'ready'" />
            </div>
            <p class="small muted">{{ model.description || '暂无说明' }}</p>
            <div class="tag-row" style="margin-top:10px">
              <span class="badge">{{ (model.dimensions || []).length }} 个维度</span>
              <span class="badge">{{ (model.measures || []).length }} 个度量</span>
            </div>
            <div v-if="canAdmin" class="row" style="margin-top:12px">
              <button v-if="canEditModel(model)" class="btn btn--sm" @click="editModel(model)">编辑模型</button>
              <button class="btn btn--sm" style="color:var(--danger)"
                      @click="openModelDelete(model)">删除模型</button>
            </div>
          </article>
          <EmptyState v-if="!models.length" icon="database" title="还没有语义模型"
                      text="语义模型把物理表映射成业务维度与度量，是指标的基础。" />
          </div>
        </div>

        <div v-else-if="tab === 'dimensions'" class="stack" style="margin-top:18px">
          <div v-for="model in models" :key="model.id" class="card">
            <div class="card__head">
              <div class="grow">
                <h2 class="card__title">{{ model.name }}</h2>
                <p class="card__hint">可用的分析维度</p>
              </div>
            </div>
            <div class="tag-row">
              <span v-for="item in model.dimensions || []" :key="item.name" class="badge">
                <Icon name="sort" :size="12" />{{ item.label || item.name }}
                <span class="faint">· {{ item.type === 'time' ? '时间' : '分类' }}</span>
              </span>
            </div>
          </div>
          <EmptyState v-if="!models.length" icon="sort" title="暂无可用维度" text="建立语义模型后，这里会列出可分析维度。" />
        </div>

        <div v-else class="card" style="margin-top:18px">
          <EmptyState icon="book" title="业务术语" text="业务术语、同义词与业务规则在「管理后台 → 知识」中维护，会被 Agent 在检索时使用。" />
        </div>
        </template>
      </div>
    </div>

    <Modal :open="!!modelDeleteTarget" title="删除语义模型" size="small" @close="closeModelDelete">
      <template v-if="modelDeleteTarget">
        <p v-if="deleteReferencesLoading" class="small muted">正在检查关联指标…</p>
        <div v-else-if="deleteReferencesError" class="stack">
          <p class="small" style="color:var(--danger)">{{ deleteReferencesError }}</p>
          <button class="btn btn--sm" @click="loadModelDeleteReferences">重试检查</button>
        </div>
        <p v-else-if="modelDeleteReferences?.length" class="small">
          「{{ modelDeleteTarget.name }}」仍被以下指标引用。请先删除或迁移这些指标，再删除模型：
          {{ modelDeleteReferences.map(item => item.name).join('、') }}。
          <span v-if="modelDeleteTarget.enabled === false">模型已停用，请先启用模型以管理关联指标。</span>
        </p>
        <p v-else class="small">确定删除语义模型「{{ modelDeleteTarget.name }}」吗？删除后将不再出现在指标中心。</p>
      </template>
      <template #footer>
        <button class="btn" :disabled="deleting" @click="closeModelDelete">取消</button>
        <button v-if="modelDeleteReferences?.length && modelDeleteTarget?.enabled === false" class="btn btn--primary"
                @click="editModel(modelDeleteTarget); closeModelDelete()">编辑模型</button>
        <button v-else-if="modelDeleteReferences?.length" class="btn btn--primary"
                @click="showModelMetric">查看关联指标</button>
        <button v-else class="btn btn--danger"
                :disabled="deleting || deleteReferencesLoading || !!deleteReferencesError || !modelDeleteReferences" @click="deleteModel">
          {{ deleting ? '删除中…' : '确认删除' }}
        </button>
      </template>
    </Modal>

    <Modal :open="!!metricDeleteTarget" title="删除指标" size="small" @close="closeMetricDelete">
      <p v-if="deleteReferencesLoading" class="small muted">正在检查指标和智能体引用…</p>
      <div v-else-if="deleteReferencesError" class="stack">
        <p class="small" style="color:var(--danger)">{{ deleteReferencesError }}</p>
        <button class="btn btn--sm" @click="loadMetricDeleteReferences">重试检查</button>
      </div>
      <div v-else-if="metricDeleteReferences?.metrics.length || metricDeleteReferences?.agents.length" class="stack small">
        <p v-if="metricDeleteReferences.metrics.length">仍被指标 {{ metricDeleteReferences.metrics.map(item => item.name).join('、') }} 引用，请先修改这些指标的公式。</p>
        <p v-if="metricDeleteReferences.agents.length">仍被智能体 {{ metricDeleteReferences.agents.map(item => item.name).join('、') }} 使用，请先调整智能体配置。</p>
      </div>
      <p v-else-if="metricDeleteTarget" class="small">
        确定删除指标「{{ metricDeleteTarget.label || metricDeleteTarget.name }}」吗？历史分析记录会保留，但该指标将不再用于后续分析。
      </p>
      <template #footer>
        <button class="btn" :disabled="deleting" @click="closeMetricDelete">取消</button>
        <button v-if="metricDeleteReferences?.metrics.length" class="btn btn--primary"
                @click="select(metrics.find(item => item.id === metricDeleteReferences.metrics[0].id)); closeMetricDelete()">查看关联指标</button>
        <button v-else-if="metricDeleteReferences?.agents.length" class="btn btn--primary"
                @click="closeMetricDelete(); navigate('admin/agents')">管理智能体</button>
        <button v-else class="btn btn--danger"
                :disabled="deleting || deleteReferencesLoading || !!deleteReferencesError || !metricDeleteReferences" @click="deleteMetric">
          {{ deleting ? '删除中…' : '确认删除' }}
        </button>
      </template>
    </Modal>

    <Modal :open="!!modelEditor" :title="modelEditor?.id ? '编辑语义模型' : '新建语义模型'" size="editor" @close="!saving && (modelEditor = null)">
      <div v-if="modelEditor" class="stack">
        <div class="grid grid--2 model-basics" style="gap:12px">
          <label class="field"><span>模型名称 *</span><input v-model.trim="modelEditor.name" class="input" placeholder="销售事实模型" /></label>
          <label class="field"><span>数据源 *</span><select v-model="modelEditor.source_id" class="select" @change="loadModelTables">
            <option v-for="source in sources.filter(item => item.status === 'ready')" :key="source.id" :value="source.id">{{ source.name }}</option>
          </select></label>
          <label class="field"><span>数据表 *</span><select v-model="modelEditor.table" class="select" :disabled="modelTablesLoading" @change="updateModelColumns">
            <option v-for="table in modelTables" :key="table.name" :value="table.name">{{ table.name }}</option>
          </select></label>
          <label class="field"><span>数据粒度</span><input v-model.trim="modelEditor.grain" class="input" placeholder="每行代表一笔订单" /></label>
        </div>
        <p v-if="modelTablesLoading" class="small muted" role="status">正在读取数据表与字段…</p>
        <div v-else-if="modelTablesError" class="row"><p class="small" role="alert" style="color:var(--danger)">{{ modelTablesError }}</p><button class="btn btn--sm" @click="loadModelTables">重新读取</button></div>
        <label class="field"><span>业务说明</span><textarea v-model.trim="modelEditor.description" class="textarea"></textarea></label>
        <div class="row row--between"><b>维度</b><button class="btn btn--sm" :disabled="modelTablesLoading || !modelColumns.length" @click="addModelField('dimensions')">添加维度</button></div>
        <div v-for="(field, index) in modelEditor.dimensions" :key="'d' + index" class="row model-field-row">
          <input v-model.trim="field.name" class="input" placeholder="技术名称" aria-label="维度技术名称" />
          <input v-model.trim="field.label" class="input" placeholder="显示名称" aria-label="维度显示名称" />
          <select v-model="field.column" class="select" aria-label="维度数据字段"><option v-for="column in modelColumns" :key="column.name" :value="column.name">{{ column.name }}</option></select>
          <select v-model="field.type" class="select" aria-label="维度类型"><option value="categorical">分类</option><option value="time">时间</option><option value="numeric">数值</option><option value="boolean">布尔</option></select>
          <button class="icon-btn" aria-label="删除维度" @click="modelEditor.dimensions.splice(index, 1)"><Icon name="close" :size="14" /></button>
        </div>
        <div class="row row--between"><b>度量 *</b><button class="btn btn--sm" :disabled="modelTablesLoading || !modelColumns.length" @click="addModelField('measures')">添加度量</button></div>
        <div v-for="(field, index) in modelEditor.measures" :key="'m' + index" class="row model-field-row">
          <input v-model.trim="field.name" class="input" placeholder="技术名称" aria-label="度量技术名称" />
          <input v-model.trim="field.label" class="input" placeholder="显示名称" aria-label="度量显示名称" />
          <select v-model="field.column" class="select" aria-label="度量数据字段"><option v-for="column in modelColumns" :key="column.name" :value="column.name">{{ column.name }}</option></select>
          <select v-model="field.aggregation" class="select" aria-label="度量聚合方式"><option value="sum">求和</option><option value="count">计数</option><option value="avg">平均</option><option value="min">最小</option><option value="max">最大</option></select>
          <button class="icon-btn" aria-label="删除度量" @click="modelEditor.measures.splice(index, 1)"><Icon name="close" :size="14" /></button>
        </div>
        <label v-if="modelEditor.dimensions.some(item => item.type === 'time')" class="field"><span>默认时间维度</span>
          <select v-model="modelEditor.default_time_dimension" class="select"><option value="">不指定</option><option v-for="field in modelEditor.dimensions.filter(item => item.type === 'time')" :key="field.name" :value="field.name">{{ field.label || field.name }}</option></select>
        </label>
      </div>
      <template #footer><button class="btn" :disabled="saving" @click="modelEditor = null">取消</button><button class="btn btn--primary" :disabled="saving || modelTablesLoading || !!modelTablesError" @click="saveModel">{{ saving ? '保存中…' : '保存模型' }}</button></template>
    </Modal>

    <Modal :open="!!editor" :title="editor?.id ? '编辑指标' : '新建指标'" wide @close="!saving && (editor = null)">
      <div v-if="editor" class="stack">
        <div class="grid grid--2" style="gap:12px">
          <label class="field"><span>业务名称<em> *</em></span>
            <input v-model.trim="editor.label" class="input" placeholder="销售额" /></label>
          <label class="field"><span>技术名称<em> *</em></span>
            <input v-model.trim="editor.name" class="input" placeholder="sales_amount" /></label>
          <label class="field"><span>语义模型<em> *</em></span>
            <select v-model="editor.model_id" class="select">
              <option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}</option>
            </select></label>
          <label class="field"><span>指标类型</span>
            <select v-model="editor.metric_type" class="select">
              <option v-for="(label, key) in metricTypes" :key="key" :value="key">{{ label }}</option>
            </select></label>
          <label v-if="editor.metric_type === 'atomic'" class="field"><span>聚合度量</span>
            <select v-model="editor.measure" class="select">
              <option value="">请选择</option>
              <option v-for="item in modelMeasures" :key="item.name" :value="item.name">{{ item.label || item.name }}</option>
            </select></label>
          <label v-else class="field"><span>指标公式</span>
            <input v-model.trim="editor.expression" class="input" placeholder="gross_sales - refunds" /></label>
          <label class="field"><span>单位</span>
            <input v-model.trim="editor.unit" class="input" placeholder="元" /></label>
          <label class="field"><span>格式</span>
            <input v-model.trim="editor.format" class="input" placeholder=",.2f" /></label>
        </div>
        <label class="field"><span>同义词（逗号分隔）</span>
          <input v-model.trim="editor.aliases" class="input" placeholder="GMV, 成交额" /></label>
        <label class="field"><span>业务定义</span>
          <textarea v-model.trim="editor.description" class="textarea"
                    placeholder="写清业务口径，Agent 会据此理解用户的问题"></textarea></label>
        <label class="field"><span>时间口径</span>
          <input v-model.trim="editor.time_semantics" class="input" placeholder="按支付完成月份归集" /></label>
      </div>
      <template #footer>
        <button class="btn" :disabled="saving" @click="editor = null">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="saveMetric">
          {{ saving ? '保存中…' : '保存为草稿' }}
        </button>
      </template>
    </Modal>`,
  computed: {
    canAnalyze() {
      return ['owner', 'editor', 'analyst'].includes(state.workspaceRole);
    },
    canTrial() {
      return this.canAnalyze && (this.selected?.status === 'approved' || ['owner', 'editor'].includes(state.workspaceRole));
    },
    filtered() {
      const keyword = this.query.trim().toLowerCase();
      return this.metrics.filter(item => {
        if (this.statusFilter && item.status !== this.statusFilter) return false;
        if (!keyword) return true;
        return `${item.label || ''}${item.name} ${item.description || ''}${(item.aliases || []).join(' ')}`
          .toLowerCase().includes(keyword);
      });
    },
    modelById() {
      return Object.fromEntries(this.models.map(item => [item.id, item]));
    },
    selectedModel() {
      return this.selected ? this.modelById[this.selected.model_id] : null;
    },
    supportedDimensions() {
      return (this.selectedModel?.dimensions || []).map(item => ({ name: item.name, label: item.label || item.name }));
    },
    modelMeasures() {
      return this.modelById[this.editor?.model_id]?.measures || [];
    },
    metricTypes() {
      return METRIC_TYPES;
    },
    dimensions() {
      return this.models.flatMap(model => model.dimensions || []);
    },
  },
};
