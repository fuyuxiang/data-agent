/**
 * 数据：文件、数据库、API 与数据集的统一入口。
 *
 * 数据质量保留，但不做产品主线——它服务于"能不能用"，不是"今天分析什么"。
 */

import { Icon } from '../components/icons.js';
import { DataTable, Drawer, EmptyState, Modal, SearchInput, Status, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';

const DETAIL_TABS = [
  { key: 'overview', label: '概览' },
  { key: 'schema', label: '结构' },
  { key: 'preview', label: '预览' },
  { key: 'quality', label: '数据质量' },
];

const LOCAL_MYSQL_DEFAULTS = {
  name: '本地 MySQL',
  host: '127.0.0.1',
  port: '3306',
  database: 'dataagent',
  username: 'dataagent',
};
const DATABASE_PORTS = { mysql: '3306', doris: '9030', starrocks: '9030', postgresql: '5432', sqlserver: '1433' };

export const DataView = {
  name: 'DataView',
  components: { DataTable, Drawer, EmptyState, Icon, Modal, SearchInput, Status, Tabs },
  setup() {
    return { state, toast };
  },
  data() {
    return {
      sources: [],
      loading: true,
      loadError: '',
      query: '',
      kind: '',
      active: null,
      detailTab: 'overview',
      schema: null,
      schemaLoading: false,
      schemaError: '',
      schemaRequest: 0,
      schemaTable: '',
      schemaOffset: 0,
      catalog: null,
      catalogQuery: '',
      catalogLoading: false,
      catalogError: '',
      catalogRequest: 0,
      catalogPages: [],
      refreshing: false,
      knownColumns: [],
      preview: null,
      previewTable: '',
      previewLoading: false,
      previewError: '',
      previewRequest: 0,
      profile: null,
      profileTable: '',
      profileLoading: false,
      profileError: '',
      profileRequest: 0,
      profileMode: 'sample',
      profileColumns: [],
      profileSampleRows: 500,
      profileTimeColumn: '',
      profileTimeFormat: 'iso',
      profileTimeStart: '',
      profileTimeEnd: '',
      profileJobId: '',
      profileTimer: null,
      profileMessage: '',
      profileCancelling: false,
      fieldQuery: '',
      fieldOptions: [],
      fieldLoading: false,
      fieldError: '',
      fieldOffset: 0,
      fieldNext: null,
      fieldRequest: 0,
      busy: false,
      uploadOpen: false,
      dbOpen: false,
      httpOpen: false,
      form: { name: '', url: '', description: '' },
      dbForm: { ...LOCAL_MYSQL_DEFAULTS, password: '', driver: 'mysql' },
    };
  },
  computed: {
    filtered() {
      const keyword = this.query.trim().toLowerCase();
      return this.sources.filter(item => {
        if (this.kind && item.kind !== this.kind) return false;
        if (!keyword) return true;
        return `${item.name} ${item.description || ''}`.toLowerCase().includes(keyword);
      });
    },
    kinds() {
      return [...new Set(this.sources.map(item => item.kind))];
    },
    detailTabs() {
      return DETAIL_TABS.map(tab => tab.key === 'quality' && this.active?.kind === 'database'
        ? { ...tab, label: '数据探查' } : tab);
    },
    availableTables() {
      const tables = new Map((this.active?.tables || []).map(table => [table.name, table]));
      for (const table of this.catalog?.items || []) tables.set(table.name, table);
      return [...tables.values()].filter(table => !Array.isArray(this.active?.analysis_tables)
        || this.active.analysis_tables.includes(table.name));
    },
    qualityStats() {
      const profile = this.profile || {};
      return [
        { label: profile.mode === 'metadata' ? '估计行数' : profile.sampled ? '检查行数' : '范围内行数', value: profile.rows ?? '—' },
        { label: '列数', value: profile.column_count ?? (this.active?.tables?.find(item => item.name === this.profileTable)?.columns ?? '—') },
        { label: '缺失单元格', value: profile.missing_cells ?? '—' },
        { label: this.active?.kind === 'database' ? '所选字段重复记录' : '重复行', value: profile.duplicate_rows ?? '—' },
      ];
    },
  },
  watch: {
    profileColumns() { this.profile = null; },
    profileTimeColumn() { this.profile = null; },
    profileTimeFormat() { this.profile = null; },
    profileTimeStart() { this.profile = null; },
    profileTimeEnd() { this.profile = null; },
    profileSampleRows() { this.profile = null; },
    detailTab(tab) {
      if (tab === 'schema' && this.active && !this.schema && !this.schemaLoading) {
        this.loadSchema();
      }
    },
  },
  async mounted() {
    await this.load();
  },
  beforeUnmount() {
    this.schemaRequest += 1;
    this.previewRequest += 1;
    this.profileRequest += 1;
    this.catalogRequest += 1;
    this.fieldRequest += 1;
    this.stopProfile();
  },
  methods: {
    kindLabel(item) {
      return item.kind === 'database' ? `${item.engine_family === 'doris' ? 'Doris' : item.engine_family === 'starrocks' ? 'StarRocks' : '数据库'} · 只读连接` : `${item.kind} 数据源`;
    },
    tableSize(table) {
      if (table.rows === null || table.rows === undefined) return '行数待统计';
      return `${table.rows_estimated ? '约 ' : ''}${new Intl.NumberFormat('zh-CN').format(table.rows)} 行`;
    },
    schemaRows(table) {
      return (table.columns || []).map(column => ({ 字段: column.name, 类型: column.type || column.dtype || '—',
        说明: column.comment || '—', 允许空值: column.nullable === false ? '否' : '是' }));
    },
    profileRows() {
      return (this.profile?.columns || []).map(column => ({ 字段: column.name,
        说明: column.comment || this.knownColumns.find(item => item.name === column.name)?.comment || '—',
        类型: column.type || column.dtype || '—', 缺失数: column.missing ?? '—',
        缺失率: column.missing_rate == null ? '—' : `${(column.missing_rate * 100).toFixed(2)}%`,
        去重数: column.unique == null ? '—' : `${column.unique_estimated ? '≈ ' : ''}${column.unique}` }));
    },
    stopProfile() {
      clearTimeout(this.profileTimer);
      this.profileTimer = null;
      if (this.profileJobId) actions.post(`/api/jobs/${this.profileJobId}/cancel`, {}).catch(() => {});
      this.profileJobId = '';
    },
    closeDetail() {
      this.stopProfile();
      this.profileRequest += 1;
      this.catalogRequest += 1;
      this.schemaRequest += 1;
      this.fieldRequest += 1;
      this.active = null;
    },
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/sources');
        this.sources = response.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    async open(source) {
      this.stopProfile();
      this.active = source;
      this.detailTab = 'overview';
      this.schema = null;
      this.schemaLoading = false;
      this.schemaError = '';
      this.schemaRequest += 1;
      this.schemaTable = source.tables?.find(table => !Array.isArray(source.analysis_tables) || source.analysis_tables.includes(table.name))?.name || '';
      this.schemaOffset = 0;
      this.catalog = null;
      this.catalogQuery = '';
      this.catalogError = '';
      this.catalogLoading = false;
      this.catalogRequest += 1;
      this.catalogPages = [];
      this.knownColumns = [];
      this.fieldQuery = '';
      this.fieldOptions = [];
      this.fieldNext = null;
      this.fieldOffset = 0;
      this.fieldLoading = false;
      this.fieldError = '';
      this.fieldRequest += 1;
      this.preview = null;
      this.previewTable = this.schemaTable;
      this.previewLoading = false;
      this.previewError = '';
      this.previewRequest += 1;
      this.profile = null;
      this.profileTable = this.schemaTable;
      this.profileMode = 'sample';
      this.profileColumns = [];
      this.profileTimeColumn = '';
      this.profileTimeFormat = 'iso';
      this.profileTimeStart = '';
      this.profileTimeEnd = '';
      this.profileMessage = '';
      this.profileCancelling = false;
      this.profileLoading = false;
      this.profileError = '';
      this.profileRequest += 1;
      if (source.kind === 'database') {
        await this.loadCatalog();
        if (this.schemaTable) await this.loadSchema();
      } else if (source.kind !== 'file' && source.kind !== 'http') await this.loadSchema();
    },
    async loadCatalog(cursor = '', reset = true) {
      if (typeof cursor !== 'string') cursor = '';
      if (!this.active || this.active.kind !== 'database') return;
      const sourceId = this.active.id;
      const requestId = ++this.catalogRequest;
      this.catalogLoading = true;
      this.catalogError = '';
      try {
        const response = await actions.get(`/api/sources/${sourceId}/catalog?q=${encodeURIComponent(this.catalogQuery)}&cursor=${encodeURIComponent(cursor)}&limit=25`);
        if (this.active?.id !== sourceId || this.catalogRequest !== requestId) return;
        this.catalog = response;
        if (reset) this.catalogPages = [''];
        else if (this.catalogPages.at(-1) !== cursor) this.catalogPages.push(cursor);
        if (!this.schemaTable && response.items?.length) this.selectTable(response.items[0].name);
      } catch (error) {
        if (this.active?.id === sourceId && this.catalogRequest === requestId) this.catalogError = error.message;
      } finally {
        if (this.active?.id === sourceId && this.catalogRequest === requestId) this.catalogLoading = false;
      }
    },
    previousCatalogPage() {
      this.catalogPages.pop();
      this.loadCatalog(this.catalogPages.at(-1) || '', false);
    },
    selectTable(name) {
      this.schemaRequest += 1;
      this.schemaLoading = false;
      this.schema = null;
      this.schemaTable = name;
      this.schemaOffset = 0;
      this.knownColumns = [];
      this.fieldOptions = [];
      this.fieldQuery = '';
      this.fieldNext = null;
      this.fieldOffset = 0;
      this.fieldLoading = false;
      this.fieldError = '';
      this.fieldRequest += 1;
      this.previewTable = name;
      this.preview = null;
      this.previewRequest += 1;
      this.profileTable = name;
      this.profile = null;
      this.profileColumns = [];
      this.profileTimeColumn = '';
      this.profileTimeStart = '';
      this.profileTimeEnd = '';
      this.profileRequest += 1;
      this.stopProfile();
      this.profileLoading = false;
      this.loadSchema();
    },
    async refreshCatalog() {
      if (!this.active || this.refreshing) return;
      const sourceId = this.active.id;
      this.refreshing = true;
      try {
        const response = await actions.post(`/api/sources/${sourceId}/refresh`, {});
        this.sources = this.sources.map(source => source.id === sourceId ? response.item : source);
        if (this.active?.id === sourceId) await this.open(response.item);
        toast('目录已刷新，字段将在查看时重新读取', '完成');
      } catch (error) {
        toast(error.message, '刷新失败', 'error');
      } finally {
        this.refreshing = false;
      }
    },
    async loadSchema() {
      if (!this.active || this.schemaLoading) return;
      const sourceId = this.active.id;
      const requestId = ++this.schemaRequest;
      this.schemaLoading = true;
      this.schemaError = '';
      try {
        const query = this.active.kind === 'database' && this.schemaTable
          ? `?table=${encodeURIComponent(this.schemaTable)}&column_offset=${this.schemaOffset}&column_limit=50` : '';
        const response = await actions.get(`/api/sources/${sourceId}/schema${query}`);
        if (this.active?.id === sourceId && this.schemaRequest === requestId) {
          this.schema = response.schema || response;
          const known = new Map(this.knownColumns.map(column => [column.name, column]));
          for (const table of this.schema.tables || []) for (const column of table.columns || []) known.set(column.name, column);
          this.knownColumns = [...known.values()];
          if (!this.fieldOptions.length) {
            this.fieldOptions = this.knownColumns.slice(0, 50);
            this.fieldNext = this.schema.tables?.[0]?.next_column_offset ?? null;
          }
          if (!this.profileColumns.length) this.profileColumns = this.knownColumns.slice(0, 10).map(column => column.name);
        }
      } catch (error) {
        if (this.active?.id === sourceId && this.schemaRequest === requestId) {
          this.schemaError = error.message;
          toast(error.message, '读取结构失败', 'error');
        }
      } finally {
        if (this.active?.id === sourceId && this.schemaRequest === requestId) this.schemaLoading = false;
      }
    },
    async loadPreview() {
      if (!this.active) return;
      const sourceId = this.active.id;
      const table = this.previewTable;
      const requestId = ++this.previewRequest;
      this.previewLoading = true;
      this.previewError = '';
      this.preview = null;
      try {
        const response = await actions.get(
          `/api/sources/${sourceId}/preview?limit=50${table ? `&table=${encodeURIComponent(table)}` : ''}`,
        );
        if (this.active?.id === sourceId && this.previewRequest === requestId) {
          this.preview = response.preview || response;
        }
      } catch (error) {
        if (this.active?.id === sourceId && this.previewRequest === requestId) this.previewError = error.message;
      } finally {
        if (this.active?.id === sourceId && this.previewRequest === requestId) this.previewLoading = false;
      }
    },
    async loadProfile() {
      if (!this.active) return;
      const sourceId = this.active.id;
      const table = this.profileTable;
      const requestId = ++this.profileRequest;
      this.stopProfile();
      this.profileLoading = true;
      this.profileError = '';
      this.profile = null;
      this.profileCancelling = false;
      try {
        if (this.active.kind === 'database') {
          const response = await actions.post(`/api/sources/${sourceId}/profile-jobs`, {
            table, mode: this.profileMode, columns: this.profileColumns.length ? this.profileColumns : undefined,
            sample_rows: this.profileSampleRows, time_column: this.profileTimeColumn,
            time_format: this.profileTimeFormat,
            time_start: this.profileTimeStart, time_end: this.profileTimeEnd,
          });
          if (this.active?.id !== sourceId || this.profileRequest !== requestId) {
            if (response.job?.id) actions.post(`/api/jobs/${response.job.id}/cancel`, {}).catch(() => {});
            return;
          }
          this.profileJobId = response.job.id;
          this.profileMessage = '等待执行';
          this.pollProfile(sourceId, requestId);
          return;
        }
        const query = table ? `?table=${encodeURIComponent(table)}` : '';
        const response = await actions.get(`/api/sources/${sourceId}/profile${query}`);
        if (this.active?.id === sourceId && this.profileTable === table && this.profileRequest === requestId) {
          this.profile = response.profile || response;
        }
      } catch (error) {
        if (this.active?.id === sourceId && this.profileRequest === requestId) {
          this.profileError = error.message;
        }
      } finally {
        if (this.active?.id === sourceId && this.profileRequest === requestId && !this.profileJobId) this.profileLoading = false;
      }
    },
    changeProfileMode() {
      this.profile = null;
      this.profileError = '';
      const maximum = this.profileMode === 'aggregate' ? 10 : 50;
      this.profileColumns = this.profileColumns.slice(0, maximum);
    },
    toggleProfileColumn(name) {
      if (this.profileColumns.includes(name)) {
        this.profileColumns = this.profileColumns.filter(column => column !== name);
        return;
      }
      const maximum = this.profileMode === 'aggregate' ? 10 : 50;
      if (this.profileColumns.length >= maximum) {
        toast(`当前方式最多选择 ${maximum} 个字段，请先取消不需要的字段`, '字段数量已达上限');
        return;
      }
      this.profileColumns = [...this.profileColumns, name];
    },
    async loadFieldOptions(offset = 0) {
      if (!this.active || this.fieldLoading) return;
      const sourceId = this.active.id;
      const table = this.profileTable;
      const requestId = ++this.fieldRequest;
      this.fieldLoading = true;
      this.fieldError = '';
      try {
        const response = await actions.get(`/api/sources/${sourceId}/schema?table=${encodeURIComponent(table)}&column_query=${encodeURIComponent(this.fieldQuery)}&column_offset=${offset}&column_limit=50`);
        if (this.active?.id !== sourceId || this.profileTable !== table || this.fieldRequest !== requestId) return;
        const selected = response.schema?.tables?.[0];
        this.fieldOptions = selected?.columns || [];
        this.fieldOffset = offset;
        this.fieldNext = selected?.next_column_offset ?? null;
        const known = new Map(this.knownColumns.map(column => [column.name, column]));
        for (const column of this.fieldOptions) known.set(column.name, column);
        this.knownColumns = [...known.values()];
      } catch (error) {
        if (this.active?.id === sourceId && this.fieldRequest === requestId) this.fieldError = error.message;
      } finally {
        if (this.active?.id === sourceId && this.fieldRequest === requestId) this.fieldLoading = false;
      }
    },
    async pollProfile(sourceId, requestId) {
      if (!this.profileJobId) return;
      try {
        const response = await actions.get(`/api/jobs/${this.profileJobId}`);
        if (this.active?.id !== sourceId || this.profileRequest !== requestId) return;
        const job = response.item;
        this.profileMessage = job.message || '正在检查';
        if (['completed', 'failed', 'cancelled', 'blocked'].includes(job.status)) {
          this.profileJobId = '';
          this.profileLoading = false;
          this.profileCancelling = false;
          if (job.status === 'completed') this.profile = job.result?.profile || null;
          else this.profileError = job.status === 'cancelled' ? '本次探查已取消，可以调整范围后重新检查。' : job.error || '探查未完成，请调整范围后重试。';
          return;
        }
        this.profileTimer = setTimeout(() => this.pollProfile(sourceId, requestId), 700);
      } catch (error) {
        if (this.active?.id === sourceId && this.profileRequest === requestId) {
          this.profileError = `${error.message}，查询状态暂时不可用，仍可取消当前任务。`;
          this.profileTimer = setTimeout(() => this.pollProfile(sourceId, requestId), 1500);
        }
      }
    },
    async cancelProfile() {
      if (!this.profileJobId || this.profileCancelling) return;
      this.profileCancelling = true;
      try {
        await actions.post(`/api/jobs/${this.profileJobId}/cancel`, {});
        this.profileMessage = '正在取消，等待查询结束';
      } catch (error) {
        this.profileCancelling = false;
        this.profileError = error.message;
      }
    },
    changeDatabaseDriver(event) {
      const previous = this.dbForm.driver;
      const driver = event.target.value;
      this.dbForm.driver = driver;
      if (this.dbForm.port === DATABASE_PORTS[previous]) this.dbForm.port = DATABASE_PORTS[driver];
      if (previous === 'mysql' && driver !== 'mysql') {
        for (const key of ['name', 'host', 'database', 'username']) {
          if (this.dbForm[key] === LOCAL_MYSQL_DEFAULTS[key]) this.dbForm[key] = '';
        }
      } else if (driver === 'mysql' && !this.dbForm.host && !this.dbForm.database) {
        Object.assign(this.dbForm, LOCAL_MYSQL_DEFAULTS);
      }
    },
    async remove(source) {
      try {
        await actions.remove(`/api/sources/${source.id}`);
        this.sources = this.sources.filter(item => item.id !== source.id);
        this.active = null;
        toast('数据源已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      }
    },
    async upload(event) {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file || this.busy) return;
      this.busy = true;
      try {
        const form = new FormData();
        form.append('file', file);
        form.append('workspace_id', state.workspaceId);
        const response = await fetch('/api/sources/upload', {
          method: 'POST',
          headers: {
            'X-Workspace-Id': state.workspaceId,
            'X-CSRF-Token': sessionStorage.getItem('shuqing-csrf') || '',
          },
          body: form,
        });
        const payload = await response.json();
        if (!response.ok || payload.ok === false) throw new Error(payload.error || '上传失败');
        await this.load();
        this.uploadOpen = false;
        toast('数据已接入', '完成');
      } catch (error) {
        toast(error.message, '上传失败', 'error');
      } finally {
        this.busy = false;
      }
    },
    async connectDatabase() {
      if (this.busy) return;
      const required = [
        ['名称', this.dbForm.name],
        ['主机', this.dbForm.host],
        ['数据库', this.dbForm.database],
      ];
      const missing = required.filter(([, value]) => !value).map(([label]) => label);
      if (missing.length) {
        toast(`请填写${missing.join('、')}`, '信息不完整', 'error');
        return;
      }
      if (this.dbForm.driver === 'mysql'
          && this.dbForm.host === LOCAL_MYSQL_DEFAULTS.host
          && this.dbForm.username === LOCAL_MYSQL_DEFAULTS.username
          && !this.dbForm.password) {
        toast('请填写 dataagent 数据库账号的密码，数擎登录密码不能用于连接 MySQL', '需要数据库密码', 'error');
        return;
      }
      this.busy = true;
      try {
        await actions.post('/api/sources/database', { ...this.dbForm, workspace_id: state.workspaceId });
        await this.load();
        this.dbOpen = false;
        this.dbForm.password = '';
        toast('数据库已接入', '完成');
      } catch (error) {
        toast(error.message, '接入失败', 'error');
      } finally {
        this.busy = false;
      }
    },
    async connectHttp() {
      if (this.busy || !this.form.name || !this.form.url) return;
      this.busy = true;
      try {
        await actions.post('/api/sources/http', { ...this.form, workspace_id: state.workspaceId });
        await this.load();
        this.httpOpen = false;
        toast('API 数据源已接入', '完成');
      } catch (error) {
        toast(error.message, '接入失败', 'error');
      } finally {
        this.busy = false;
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">数据</h1>
          <p class="page-head__desc">接入业务数据，查看结构、预览和数据质量。分析仅使用当前用户有权访问的数据。</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--sm" @click="httpOpen = true"><Icon name="link" :size="14" />接入 API 数据源</button>
          <button class="btn btn--sm" @click="dbOpen = true"><Icon name="database" :size="14" />连接数据库</button>
          <label class="btn btn--primary btn--sm">
            <Icon name="upload" :size="14" />{{ busy ? '接入中…' : '上传文件' }}
            <input type="file" hidden :disabled="busy" accept=".csv,.tsv,.xlsx,.xls,.json,.parquet" @change="upload" />
          </label>
        </div>
      </header>

      <div class="toolbar">
        <SearchInput v-model="query" placeholder="搜索数据源" style="width:240px" />
        <div class="segmented">
          <button :class="{ active: !kind }" @click="kind = ''">全部</button>
          <button v-for="item in kinds" :key="item" :class="{ active: kind === item }" @click="kind = item">{{ item }}</button>
        </div>
        <span class="toolbar__spacer"></span>
        <span v-if="!loading && !loadError" class="small faint">{{ filtered.length }} 个数据源</span>
      </div>

      <div v-if="loading" class="grid grid--3">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:120px"></div>
      </div>

      <div v-else-if="loadError" class="card stack"><p class="small" role="alert" style="color:var(--danger)">{{ loadError }}</p><button class="btn btn--sm" @click="load">重新加载数据源</button></div>
      <EmptyState v-else-if="!filtered.length" icon="database" :title="query || kind ? '没有匹配的数据源' : '还没有数据源'"
                  :text="query || kind ? '调整关键词或数据源类型后重试。' : '上传一个文件，或连接数据库和 API。也可以在工作台一键载入演示数据。'" />
      <div v-else class="grid grid--3">
        <article v-for="item in filtered" :key="item.id" class="card card--interactive file-tile"
                 @click="open(item)">
          <div class="row row--between">
            <span class="agent-card__mark" style="width:34px;height:34px"><Icon name="database" :size="17" /></span>
            <Status :status="item.status" />
          </div>
          <div>
            <b class="truncate" style="display:block">{{ item.name }}</b>
            <p class="small muted" style="margin-top:4px;line-height:1.6">
              {{ item.description || kindLabel(item) }}
            </p>
          </div>
          <div class="tag-row">
            <span class="badge">{{ item.kind }}</span>
            <span v-for="table in (item.tables || []).slice(0, 2)" :key="table.name" class="badge">
              {{ table.name }} · {{ tableSize(table) }}
            </span>
          </div>
          <div class="row" style="margin-top:auto">
            <button class="btn btn--sm" @click.stop="open(item)">查看</button>
            <span class="grow"></span>
            <button class="icon-btn icon-btn--danger" aria-label="删除" @click.stop="remove(item)">
              <Icon name="trash" :size="15" />
            </button>
          </div>
        </article>
      </div>
    </div>

    <Drawer :open="!!active" :title="active?.name || ''" :subtitle="active?.description" :width="800"
            @close="closeDetail">
      <div v-if="active">
        <Tabs v-model="detailTab" :items="detailTabs" />

        <div v-if="detailTab === 'overview'" class="stack" style="margin-top:16px">
          <dl class="definition">
            <dt>类型</dt><dd>{{ kindLabel(active) }}</dd>
            <dt>状态</dt><dd><Status :status="active.status" /></dd>
            <dt>文件</dt><dd class="mono xs">{{ active.filename || '—' }}</dd>
            <dt>数据表</dt>
            <dd>
              <p v-if="active.kind === 'database'" class="small">{{ active.catalog_total ?? active.tables?.length ?? 0 }} 个表或视图 · 按需读取结构</p>
              <div v-for="table in (active.tables || []).slice(0, active.kind === 'database' ? 5 : 100)" :key="table.name" class="small">
                {{ table.name }} · {{ tableSize(table) }}<span v-if="table.columns != null"> · {{ table.columns }} 列</span>
              </div>
              <button v-if="active.kind === 'database'" class="btn btn--sm" @click="detailTab = 'schema'">搜索与查看数据表</button>
              <span v-if="!(active.tables || []).length" class="faint">尚未解析</span>
            </dd>
            <dt>最近刷新</dt><dd>{{ active.last_refreshed_at || '—' }}</dd>
            <dt>敏感级别</dt><dd>{{ active.sensitivity || 'internal' }}</dd>
          </dl>
          <button v-if="active.kind === 'database'" class="btn btn--sm" :disabled="refreshing" @click="refreshCatalog">{{ refreshing ? '刷新中…' : '刷新目录' }}</button>
        </div>

        <div v-else-if="detailTab === 'schema'" style="margin-top:16px">
          <div v-if="active.kind === 'database'" class="stack" style="margin-bottom:16px">
            <div class="row">
              <input v-model="catalogQuery" class="input grow" placeholder="搜索表名或业务说明" aria-label="搜索数据库数据表" @keyup.enter="loadCatalog()" />
              <button class="btn btn--sm" :disabled="catalogLoading" @click="loadCatalog()">搜索</button>
            </div>
            <p v-if="catalogLoading" class="small faint" role="status">正在读取目录…</p>
            <div v-if="catalogError" class="row"><p class="small" role="alert">{{ catalogError }}</p><button class="btn btn--sm" @click="loadCatalog()">重试目录</button></div>
            <div v-if="catalog" class="stack" style="gap:6px">
              <p class="xs muted">找到 {{ catalog.total }} 个表或视图</p>
              <div class="database-table-list">
              <button v-for="table in catalog.items || []" :key="table.name" class="card" :aria-pressed="schemaTable === table.name"
                      style="text-align:left;padding:10px" @click="selectTable(table.name)">
                <b class="small">{{ table.name }}</b><span class="xs muted" style="display:block">{{ table.comment || table.object_type }} · {{ tableSize(table) }}</span>
              </button>
              </div>
              <p v-if="!catalog.items?.length" class="small muted">没有匹配的数据表，请调整搜索词。</p>
              <div class="row"><button class="btn btn--sm" :disabled="catalogPages.length < 2 || catalogLoading" @click="previousCatalogPage">上一页数据表</button>
                <button class="btn btn--sm" :disabled="!catalog.next_cursor || catalogLoading" @click="loadCatalog(catalog.next_cursor, false)">下一页数据表</button></div>
            </div>
          </div>
          <p v-if="schemaLoading" class="small faint">正在读取结构…</p>
          <div v-else-if="schemaError" class="stack">
            <p class="small" style="color:var(--danger)">{{ schemaError }}</p>
            <button class="btn btn--sm" @click="loadSchema">重试</button>
          </div>
          <EmptyState v-else-if="schema && !(schema.tables || []).length"
                      icon="table" title="没有可显示的数据表" text="请确认数据源可访问，并包含可读取的数据表或有效的表头。" />
          <div v-else-if="schema">
            <div v-for="table in schema.tables || []" :key="table.name" class="card" style="margin-bottom:12px">
              <h3 class="card__title" style="margin-bottom:8px">{{ table.name }}</h3>
              <p v-if="table.comment" class="small muted">{{ table.comment }}</p>
              <p v-if="table.schema_error" class="small" role="alert">{{ table.schema_error }}</p>
              <DataTable :rows="schemaRows(table)" :columns="['字段', '类型', '说明', '允许空值']" max-height="360px" />
              <div v-if="active.kind === 'database'" class="row" style="margin-top:10px">
                <span class="xs muted">共 {{ table.column_count }} 个字段</span>
                <button class="btn btn--sm" :disabled="schemaOffset === 0 || schemaLoading" @click="schemaOffset = Math.max(0, schemaOffset - 50); loadSchema()">上一页字段</button>
                <button class="btn btn--sm" :disabled="table.next_column_offset == null || schemaLoading" @click="schemaOffset = table.next_column_offset; loadSchema()">下一页字段</button>
                <button class="btn btn--sm" :disabled="!!table.schema_error" @click="detailTab = 'preview'">预览此表</button>
                <button class="btn btn--sm" :disabled="!!table.schema_error" @click="detailTab = 'quality'">探查此表</button>
              </div>
            </div>
          </div>
          <button v-else class="btn btn--sm" @click="loadSchema">读取结构</button>
        </div>

        <div v-else-if="detailTab === 'preview'" style="margin-top:16px">
          <div class="row" style="margin-bottom:10px">
            <select v-if="availableTables.length > 1" v-model="previewTable" class="select input--sm" style="width:200px"
                    aria-label="预览数据表" @change="loadPreview">
              <option v-for="table in availableTables" :key="table.name" :value="table.name">{{ table.name }}</option>
            </select>
            <button class="btn btn--sm" :disabled="previewLoading" @click="loadPreview">{{ previewLoading ? '加载中…' : preview ? '重新加载预览' : '加载预览' }}</button>
          </div>
          <p v-if="previewError" class="small" role="alert" style="color:var(--danger);margin-bottom:10px">{{ previewError }}，可以重新加载。</p>
          <p v-if="previewLoading" class="small muted" role="status">正在读取当前数据表…</p>
          <template v-else-if="preview">
            <p class="xs muted" style="margin-bottom:10px">{{ previewTable || '当前数据表' }} · 展示前 {{ (preview.data || preview.rows || []).length }} 行（最多 50 行）</p>
            <DataTable :rows="preview.data || preview.rows || []" :columns="preview.columns || []" max-height="min(60vh, 560px)" />
          </template>
          <EmptyState v-else-if="!previewError" icon="table" title="还没有预览" text="点击「加载预览」查看前 50 行数据。" />
        </div>

        <div v-else style="margin-top:16px">
          <div v-if="availableTables.length > 1" class="row" style="margin-bottom:10px">
            <select v-model="profileTable" class="select input--sm" style="width:200px"
                    aria-label="探查数据表" @change="active.kind === 'database' ? selectTable(profileTable) : loadProfile()">
              <option v-for="table in availableTables" :key="table.name" :value="table.name">{{ table.name }}</option>
            </select>
          </div>
          <div v-if="active.kind === 'database'" class="stack" style="margin-bottom:14px">
            <label class="field"><span>探查方式</span><select v-model="profileMode" class="select" :disabled="profileLoading" aria-label="探查方式" @change="changeProfileMode">
              <option value="metadata">结构与规模</option><option value="sample">样本检查</option><option value="aggregate">范围汇总</option>
            </select></label>
            <p class="small muted">结构与规模读取数据库统计信息；样本检查描述有限记录；范围汇总在数据库内计算所选字段。</p>
            <div v-if="profileMode !== 'metadata' && knownColumns.length" class="stack" style="gap:8px">
              <div class="row row--between"><b class="small">探查字段 · 已选 {{ profileColumns.length }} / {{ profileMode === 'aggregate' ? 10 : 50 }}</b>
                <button class="btn btn--sm" :disabled="profileLoading" @click="profileColumns = []">清空选择</button></div>
              <div class="row"><input v-model="fieldQuery" class="input grow" placeholder="搜索字段名称或说明" aria-label="搜索探查字段" :disabled="profileLoading" @keyup.enter="loadFieldOptions()" />
                <button class="btn btn--sm" :disabled="fieldLoading || profileLoading" @click="loadFieldOptions()">查找字段</button></div>
              <div class="database-field-list" role="group" aria-label="探查字段">
                <label v-for="column in fieldOptions" :key="column.name" class="database-field-choice">
                  <input type="checkbox" :checked="profileColumns.includes(column.name)" :disabled="profileLoading" :aria-label="'选择字段 ' + column.name" @change="toggleProfileColumn(column.name)" />
                  <span><b>{{ column.comment || column.name }}</b><small>{{ column.name }} · {{ column.type }}</small></span>
                </label>
                <p v-if="!fieldOptions.length && !fieldLoading" class="small muted">没有匹配的字段，请调整搜索词。</p>
              </div>
              <p v-if="fieldLoading" class="xs muted" role="status">正在查找字段…</p>
              <p v-if="fieldError" class="small" role="alert">{{ fieldError }}</p>
              <div class="row"><button class="btn btn--sm" :disabled="fieldOffset === 0 || fieldLoading || profileLoading" @click="loadFieldOptions(Math.max(0, fieldOffset - 50))">上一页候选字段</button>
                <button class="btn btn--sm" :disabled="fieldNext == null || fieldLoading || profileLoading" @click="loadFieldOptions(fieldNext)">下一页候选字段</button></div>
              <div class="tag-row"><button v-for="name in profileColumns" :key="name" class="badge" :disabled="profileLoading" :aria-label="'取消字段 ' + name" @click="toggleProfileColumn(name)">{{ name }} ×</button></div>
            </div>
            <label v-if="profileMode === 'sample'" class="field"><span>最多检查行数</span><select v-model.number="profileSampleRows" class="select" :disabled="profileLoading" aria-label="最多检查行数"><option :value="500">500</option><option :value="1000">1000</option><option :value="5000">5000</option></select></label>
            <div v-if="profileMode !== 'metadata'" class="grid grid--2" style="gap:10px">
              <label class="field"><span>时间字段（可选）</span><input v-model.trim="profileTimeColumn" class="input" list="database-time-fields" :disabled="profileLoading" placeholder="如 statistical_date" aria-label="探查时间字段" /><datalist id="database-time-fields"><option v-for="column in knownColumns" :key="column.name" :value="column.name">{{ column.comment }}</option></datalist></label>
              <label class="field"><span>时间字段格式</span><select v-model="profileTimeFormat" class="select" :disabled="profileLoading" aria-label="时间字段格式"><option value="iso">日期或日期时间（2026-10-12）</option><option value="yyyyMMdd">紧凑日期（20261012）</option><option value="yyyyMMddHH">紧凑小时（2026101214）</option><option value="unix_seconds">Unix 时间戳（秒，Asia/Shanghai）</option><option value="unix_milliseconds">Unix 时间戳（毫秒，Asia/Shanghai）</option></select></label>
              <label class="field"><span>开始日期（包含）</span><input v-model="profileTimeStart" type="date" class="input" :disabled="profileLoading" aria-label="探查开始日期" /></label>
              <label class="field"><span>结束日期（不包含）</span><input v-model="profileTimeEnd" type="date" class="input" :disabled="profileLoading" aria-label="探查结束日期" /></label>
            </div>
          </div>
          <p v-if="active.kind === 'database' && schema?.tables?.[0]?.schema_error" class="small" role="alert">{{ schema.tables[0].schema_error }}</p>
          <button class="btn btn--sm" :disabled="profileLoading || (active.kind === 'database' && (schemaLoading || !knownColumns.length))" @click="loadProfile">
            {{ profileLoading ? '检查中…' : active.kind === 'database' ? profile ? '重新探查' : '开始探查' : profile ? '重新检查' : '运行数据质量检查' }}
          </button>
          <button v-if="profileJobId" class="btn btn--sm" :disabled="profileCancelling" @click="cancelProfile">{{ profileCancelling ? '正在取消…' : '取消探查' }}</button>
          <p v-if="profileLoading && profileMessage" class="small muted" role="status" style="margin-top:10px">{{ profileMessage }}</p>
          <p v-if="profileError" class="small" style="color:var(--danger);margin-top:10px">{{ profileError }}</p>
          <div v-if="profile" class="stack" style="margin-top:12px">
            <p v-if="profile.sampled" class="small muted">
              {{ profile.sampling_note || '统计仅描述当前样本，不能代表全表质量。' }} 本次检查 {{ profile.sample_rows || profile.rows }} 行。
            </p>
            <p v-if="profile.note" class="small muted">{{ profile.note }}</p>
            <p v-if="profile.scope" class="xs muted">范围：{{ profile.scope.table }} · {{ profile.scope.columns?.length || 0 }} 个字段 · {{ profile.scope.time_start || '不限开始日期' }} 至 {{ profile.scope.time_end || '不限结束日期' }}<span v-if="profile.cached"> · 使用两分钟内的检查结果</span></p>
            <div class="metric-strip">
              <div v-for="item in qualityStats" :key="item.label" class="metric-strip__item">
                <div class="metric-strip__label">{{ item.label }}</div>
                <div class="metric-strip__value">{{ item.value }}</div>
              </div>
            </div>
            <DataTable v-if="profile.columns" :rows="active.kind === 'database' ? profileRows() : profile.columns" max-height="360px" />
          </div>
        </div>
      </div>
    </Drawer>

    <Modal :open="httpOpen" title="接入 API 数据源" @close="!busy && (httpOpen = false)">
      <div class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="form.name" class="input" placeholder="订单接口" /></label>
        <label class="field"><span>地址<em> *</em></span>
          <input v-model.trim="form.url" class="input" placeholder="https://api.example.com/orders" /></label>
        <label class="field"><span>说明</span>
          <input v-model.trim="form.description" class="input" /></label>
        <p class="xs faint">出站域名必须已在部署白名单内，否则会被拒绝。</p>
      </div>
      <template #footer>
        <button class="btn" :disabled="busy" @click="httpOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="busy || !form.name || !form.url" @click="connectHttp">{{ busy ? '接入中…' : '接入' }}</button>
      </template>
    </Modal>

    <Modal :open="dbOpen" title="连接数据库" :wide="true" @close="!busy && (dbOpen = false)">
      <div class="stack">
        <p class="xs faint" v-if="dbForm.driver === 'mysql' && dbForm.host === '127.0.0.1'">
          已填入本地 MySQL 示例配置；连接其他数据库时请修改主机、库名和账号。数擎登录密码不能用于连接 MySQL。
        </p>
        <div class="grid grid--2" style="gap:12px">
          <label class="field"><span>名称<em> *</em></span>
            <input v-model.trim="dbForm.name" class="input" /></label>
          <label class="field"><span>类型</span>
            <select :value="dbForm.driver" class="select" @change="changeDatabaseDriver">
              <option value="mysql">MySQL</option>
              <option value="doris">Apache Doris</option>
              <option value="starrocks">StarRocks</option>
              <option value="postgresql">PostgreSQL</option>
              <option value="sqlserver">SQL Server</option>
            </select></label>
          <label class="field"><span>主机<em> *</em></span>
            <input v-model.trim="dbForm.host" class="input" placeholder="127.0.0.1" /></label>
          <label class="field"><span>端口</span>
            <input v-model.trim="dbForm.port" class="input" /></label>
          <label class="field"><span>数据库<em> *</em></span>
            <input v-model.trim="dbForm.database" class="input" /></label>
          <label class="field"><span>用户名</span>
            <input v-model.trim="dbForm.username" class="input" /></label>
          <label class="field"><span>密码</span>
            <input type="password" v-model="dbForm.password" class="input" autocomplete="new-password" /></label>
        </div>
        <p class="xs faint">凭据会加密存储，查询始终只读。</p>
      </div>
      <template #footer>
        <button class="btn" :disabled="busy" @click="dbOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="busy"
                @click="connectDatabase">{{ busy ? '连接中…' : '连接' }}</button>
      </template>
    </Modal>`,
};
