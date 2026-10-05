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
      query: '',
      kind: '',
      active: null,
      detailTab: 'overview',
      schema: null,
      schemaLoading: false,
      schemaError: '',
      preview: null,
      profile: null,
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
      return DETAIL_TABS;
    },
    qualityStats() {
      const profile = this.profile || {};
      return [
        { label: '行数', value: profile.rows ?? '—' },
        { label: '列数', value: profile.column_count ?? (this.active?.tables?.[0]?.columns ?? '—') },
        { label: '缺失单元格', value: profile.missing_cells ?? '—' },
        { label: '重复行', value: profile.duplicate_rows ?? '—' },
      ];
    },
  },
  watch: {
    detailTab(tab) {
      if (tab === 'schema' && this.active && !this.schema && !this.schemaLoading) {
        this.loadSchema();
      }
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    kindLabel(item) {
      return `${item.kind} 数据源`;
    },
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/sources');
        this.sources = response.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    async open(source) {
      this.active = source;
      this.detailTab = 'overview';
      this.schema = null;
      this.schemaLoading = false;
      this.schemaError = '';
      this.preview = null;
      this.profile = null;
      if (source.kind !== 'file' && source.kind !== 'http') await this.loadSchema();
    },
    async loadSchema() {
      if (!this.active || this.schemaLoading) return;
      const sourceId = this.active.id;
      this.schemaLoading = true;
      this.schemaError = '';
      try {
        const response = await actions.get(`/api/sources/${sourceId}/schema`);
        if (this.active?.id === sourceId) this.schema = response.schema || response;
      } catch (error) {
        if (this.active?.id === sourceId) {
          this.schemaError = error.message;
          toast(error.message, '读取结构失败', 'error');
        }
      } finally {
        if (this.active?.id === sourceId) this.schemaLoading = false;
      }
    },
    async loadPreview(table) {
      try {
        const response = await actions.get(
          `/api/sources/${this.active.id}/preview?limit=50${table ? `&table=${encodeURIComponent(table)}` : ''}`,
        );
        this.preview = response.preview || response;
      } catch (error) {
        toast(error.message, '预览失败', 'error');
      }
    },
    async loadProfile(table) {
      try {
        const query = table ? `?table=${encodeURIComponent(table)}` : '';
        const response = await actions.get(`/api/sources/${this.active.id}/profile${query}`);
        this.profile = response.profile || response;
      } catch (error) {
        toast(error.message, '画像失败', 'error');
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
      if (!file) return;
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
      const required = [
        ['名称', this.dbForm.name],
        ['主机', this.dbForm.host],
        ['数据库', this.dbForm.database],
      ];
      const missing = required.filter(([, value]) => !value).map(([label]) => label);
      if (missing.length) {
        toast(`请填写${missing.join('、')}`, '信息未填完整', 'error');
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
          <p class="page-head__desc">文件、数据库、API 与数据集统一接入。Agent 只会使用当前用户有权限的数据。</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--sm" @click="httpOpen = true"><Icon name="link" :size="14" />API 数据源</button>
          <button class="btn btn--sm" @click="dbOpen = true"><Icon name="database" :size="14" />连接数据库</button>
          <label class="btn btn--primary btn--sm">
            <Icon name="upload" :size="14" />上传文件
            <input type="file" hidden accept=".csv,.tsv,.xlsx,.xls,.json,.parquet" @change="upload" />
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
        <span class="small faint">{{ filtered.length }} 个数据源</span>
      </div>

      <div v-if="loading" class="grid grid--3">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:120px"></div>
      </div>

      <EmptyState v-else-if="!filtered.length" icon="database" title="还没有数据源"
                  text="上传一个文件，或连接数据库和 API。也可以在工作台一键载入演示数据。" />

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
              {{ table.name }} · {{ table.rows }} 行
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

    <Drawer :open="!!active" :title="active?.name || ''" :subtitle="active?.description" width="640"
            @close="active = null">
      <div v-if="active">
        <Tabs v-model="detailTab" :items="detailTabs" />

        <div v-if="detailTab === 'overview'" class="stack" style="margin-top:16px">
          <dl class="definition">
            <dt>类型</dt><dd>{{ active.kind }}</dd>
            <dt>状态</dt><dd><Status :status="active.status" /></dd>
            <dt>文件</dt><dd class="mono xs">{{ active.filename || '—' }}</dd>
            <dt>数据表</dt>
            <dd>
              <div v-for="table in active.tables || []" :key="table.name" class="small">
                {{ table.name }} · {{ table.rows }} 行 · {{ table.columns }} 列
              </div>
              <span v-if="!(active.tables || []).length" class="faint">尚未解析</span>
            </dd>
            <dt>最近刷新</dt><dd>{{ active.last_refreshed_at || '—' }}</dd>
            <dt>敏感级别</dt><dd>{{ active.sensitivity || 'internal' }}</dd>
          </dl>
        </div>

        <div v-else-if="detailTab === 'schema'" style="margin-top:16px">
          <p v-if="schemaLoading" class="small faint">正在读取结构…</p>
          <div v-else-if="schemaError" class="stack">
            <p class="small" style="color:var(--danger)">{{ schemaError }}</p>
            <button class="btn btn--sm" @click="loadSchema">重试</button>
          </div>
          <EmptyState v-else-if="schema && !(schema.tables || []).length"
                      icon="table" title="没有可显示的数据表" text="请检查文件是否包含有效的表头和数据。" />
          <div v-else-if="schema">
            <div v-for="table in schema.tables || []" :key="table.name" class="card" style="margin-bottom:12px">
              <h3 class="card__title" style="margin-bottom:8px">{{ table.name }}</h3>
              <DataTable :rows="table.columns || []" max-height="360px" />
            </div>
          </div>
          <button v-else class="btn btn--sm" @click="loadSchema">读取结构</button>
        </div>

        <div v-else-if="detailTab === 'preview'" style="margin-top:16px">
          <div class="row" style="margin-bottom:10px">
            <select v-if="(active.tables || []).length > 1" class="select input--sm" style="width:200px"
                    @change="loadPreview($event.target.value)">
              <option v-for="table in active.tables" :key="table.name" :value="table.name">{{ table.name }}</option>
            </select>
            <button class="btn btn--sm" @click="loadPreview((active.tables || [])[0]?.name)">加载预览</button>
          </div>
          <DataTable v-if="preview" :rows="preview.data || preview.rows || []" max-height="420px" />
          <EmptyState v-else icon="table" title="还没有预览" text="点击「加载预览」查看前 50 行数据。" />
        </div>

        <div v-else style="margin-top:16px">
          <div v-if="(active.tables || []).length > 1" class="row" style="margin-bottom:10px">
            <select class="select input--sm" style="width:200px"
                    @change="profile = null; loadProfile($event.target.value)">
              <option v-for="table in active.tables" :key="table.name" :value="table.name">{{ table.name }}</option>
            </select>
          </div>
          <button v-if="!profile" class="btn btn--sm" @click="loadProfile()">运行数据质量检查</button>
          <div v-else class="stack">
            <p v-if="profile.sampled" class="small muted">
              数据库数据按最多 {{ profile.sample_rows || profile.rows }} 行样本检查
              <span v-if="profile.sample_truncated">（数据量较大，指标为样本结果）</span>。
            </p>
            <div class="metric-strip">
              <div v-for="item in qualityStats" :key="item.label" class="metric-strip__item">
                <div class="metric-strip__label">{{ item.label }}</div>
                <div class="metric-strip__value">{{ item.value }}</div>
              </div>
            </div>
            <DataTable v-if="profile.columns" :rows="profile.columns" max-height="360px" />
          </div>
        </div>
      </div>
    </Drawer>

    <Modal :open="httpOpen" title="接入 API 数据源" @close="httpOpen = false">
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
        <button class="btn" @click="httpOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="busy || !form.name || !form.url" @click="connectHttp">接入</button>
      </template>
    </Modal>

    <Modal :open="dbOpen" title="连接数据库" :wide="true" @close="dbOpen = false">
      <div class="stack">
        <p class="xs faint" v-if="dbForm.driver === 'mysql' && dbForm.host === '127.0.0.1'">
          已填入当前服务器的 MySQL 连接信息。数擎登录账号与数据库账号不同，请在下方填写 dataagent 数据库账号的密码。
        </p>
        <div class="grid grid--2" style="gap:12px">
          <label class="field"><span>名称<em> *</em></span>
            <input v-model.trim="dbForm.name" class="input" /></label>
          <label class="field"><span>类型</span>
            <select v-model="dbForm.driver" class="select">
              <option value="mysql">MySQL</option>
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
        <button class="btn" @click="dbOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="busy"
                @click="connectDatabase">连接</button>
      </template>
    </Modal>`,
};
