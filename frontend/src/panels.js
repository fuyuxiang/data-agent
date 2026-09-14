import { api, withWorkspace } from './api.js';
import { DataTable, EmptyState, Icon, Modal, StatusPill } from './components.js';

export { AnalysisPanel as ChatPanel } from './analysis-panel.js';

export const SourcesPanel = {
  components: { DataTable, EmptyState, Icon, Modal, StatusPill },
  props: { ctx: Object },
  data: () => ({
    connectOpen: false, connectMode: 'database',
    dbForm: { name: '', driver: 'sqlite', database: '', host: '127.0.0.1', port: '', username: '', password: '', ssl_mode: 'preferred' },
    httpForm: { name: '', url: '', json_path: '' },
    sheetForm: { name: 'Google Sheet', url: '', gid: '0' },
    larkForm: { name: '飞书多维表格', app_id: '', app_secret: '', app_token: '', table_id: '' },
    activeId: '', preview: null, profile: null, detailTab: 'preview', previewTable: '', selectedTables: [],
    cleanOps: { drop_duplicates: true, trim_text: true, fill_missing: false, winsorize: false },
    sourceSets: [], selectedSet: '',
    members: [], governance: { name: '', description: '', classification: 'internal', sensitivity: 'internal', retention_policy: '', restricted: false, authorized_user_ids: [] },
  }),
  computed: {
    state() { return this.ctx.state; },
    active() { return this.state.sources.find(item => item.id === this.activeId) || null; },
    hasDemoSource() { return this.state.sources.some(item => item.sample_seed?.id === 'instant_retail_city_pack'); },
    isWorkspaceOwner() { return !this.state.user || this.state.workspaceRole === 'owner'; },
  },
  watch: { 'state.sources': { handler(items) { if (!this.activeId && items.length) this.select(items[0].id); }, immediate: true } },
  mounted() { this.loadSets(); this.loadMembers(); },
  methods: {
    syncDatabaseDefaults() {
      const ports = { postgresql: '5432', mysql: '3306', sqlserver: '1433' };
      this.dbForm.port = ports[this.dbForm.driver] || '';
      if (this.dbForm.driver !== 'sqlite' && !this.dbForm.host) this.dbForm.host = '127.0.0.1';
      this.dbForm.ssl_mode = this.dbForm.driver === 'mysql' ? 'preferred' : this.dbForm.driver === 'postgresql' ? 'prefer' : '';
    },
    async loadSets() { this.sourceSets = (await api(withWorkspace('/api/source-sets', this.state.workspaceId))).items; },
    async loadMembers() { this.members = (await api(`/api/workspaces/${this.state.workspaceId}/members`)).items; },
    async saveSet() { const ids=this.ctx.activeSession()?.source_ids||[];if(!ids.length)return this.ctx.fail(new Error('请先为当前会话选择数据源'));const name=prompt('数据组合名称：','常用分析数据')||'';if(!name)return;await api('/api/source-sets',{method:'POST',body:{name,source_ids:ids,workspace_id:this.state.workspaceId}});await this.loadSets();this.ctx.toast('当前数据源组合已保存','保存成功'); },
    async applySet() { if(!this.selectedSet)return;const session=this.ctx.activeSession();if(!session)return;const result=await api(`/api/source-sets/${this.selectedSet}/apply`,{method:'POST',body:{session_id:session.id}});Object.assign(session,result.session);this.ctx.toast('当前会话的数据源已切换','组合已应用'); },
    async attachToCurrentSession(sources) {
      const session = this.ctx.activeSession();
      if (!session || !sources?.length) return;
      const sourceIds = [...new Set([...(session.source_ids || []), ...sources.map(item => item.id)])];
      const response = await api(`/api/sessions/${session.id}`, { method: 'PATCH', body: { source_ids: sourceIds } });
      Object.assign(session, response.item);
    },
    async upload(event) {
      const files = [...event.target.files];
      if (!files.length) return;
      const form = new FormData(); files.forEach(file => form.append('files', file)); form.append('workspace_id', this.state.workspaceId);
      await this.ctx.run('正在读取数据文件', async () => {
        const result = await api('/api/sources/upload', { method: 'POST', body: form });
        this.state.sources.unshift(...result.items);
        await this.attachToCurrentSession(result.items);
        this.activeId = result.items[0].id; await this.select(this.activeId);
        this.ctx.toast('上传的数据已自动加入当前分析', '数据范围已更新');
      });
      event.target.value = '';
    },
    async loadDemo() {
      await this.ctx.run('正在载入完整演示环境', async () => {
        const result = await api('/api/demo/seed', { method: 'POST', body: { workspace_id: this.state.workspaceId } });
        await this.ctx.bootstrap();
        this.activeId = result.source.id;
        await this.select(result.source.id);
        this.ctx.toast('已加入演示数据、正式指标、业务知识和推荐问题，可直接进入智能分析', result.created.length ? '演示环境已就绪' : '演示环境已存在');
      }, false);
    },
    async connect() {
      if (this.connectMode === 'database') {
        if (!this.dbForm.database.trim()) return this.ctx.fail(new Error(this.dbForm.driver === 'sqlite' ? '请输入 SQLite 文件路径' : '请输入数据库名称'));
        if (this.dbForm.driver !== 'sqlite' && !this.dbForm.host.trim()) return this.ctx.fail(new Error('请输入数据库主机'));
      }
      await this.ctx.run('正在验证数据连接', async () => {
        const paths = { database:'/api/sources/database', http:'/api/sources/http', sheets:'/api/sources/google-sheets', lark:'/api/sources/lark-table' };
        const forms = { database:this.dbForm, http:this.httpForm, sheets:this.sheetForm, lark:this.larkForm };
        const path = paths[this.connectMode];
        const form = forms[this.connectMode];
        const result = await api(path, { method: 'POST', body: { ...form, workspace_id: this.state.workspaceId } });
        this.state.sources.unshift(result.item);
        await this.attachToCurrentSession([result.item]);
        this.connectOpen = false; await this.select(result.item.id);
        this.ctx.toast('新连接已自动加入当前分析', '数据范围已更新');
      });
    },
    async select(id) {
      this.activeId = id; this.preview = this.profile = null; this.detailTab = 'preview'; this.previewTable = ''; this.selectedTables = [];
      await this.ctx.run('', async () => {
        const source = (await api(`/api/sources/${id}`)).item;
        const index = this.state.sources.findIndex(item => item.id === id); if (index >= 0) this.state.sources[index] = source;
        if (source.kind === 'database') {
          const allTables = (source.tables || []).map(table => table.source_name || table.name);
          this.selectedTables = Array.isArray(source.analysis_tables) ? [...source.analysis_tables] : allTables;
          this.previewTable = this.selectedTables[0] || allTables[0] || '';
        }
        this.governance = { name: source.name || '', description: source.description || '', classification: source.classification || 'internal', sensitivity: source.sensitivity || source.classification || 'internal', retention_policy: source.retention_policy || '', restricted: Array.isArray(source.authorized_user_ids), authorized_user_ids: [...(source.authorized_user_ids || [])] };
        await this.loadPreview(this.previewTable);
      }, false);
    },
    async loadPreview(tableName = '') {
      if (!this.activeId) return;
      this.previewTable = tableName || this.previewTable;
      const table = this.previewTable ? `&table=${encodeURIComponent(this.previewTable)}` : '';
      const result = await api(`/api/sources/${this.activeId}/preview?limit=100${table}`);
      this.preview = result.preview; this.detailTab = 'preview';
    },
    async saveTableSelection() {
      if (!this.active || this.active.kind !== 'database') return;
      const result = await api(`/api/sources/${this.active.id}`, { method: 'PATCH', body: { analysis_tables: this.selectedTables } });
      const index = this.state.sources.findIndex(item => item.id === this.active.id);
      if (index >= 0) this.state.sources[index] = result.item;
      this.ctx.toast(`${this.selectedTables.length} 张表可用于当前数据源的 Agent 查询`, '表范围已更新');
    },
    async toggleAllTables() {
      const allTables = (this.active?.tables || []).map(table => table.source_name || table.name);
      this.selectedTables = this.selectedTables.length === allTables.length ? [] : allTables;
      await this.saveTableSelection();
    },
    async toggleUse(source) {
      const session = this.ctx.activeSession(); if (!session) return;
      const ids = new Set(session.source_ids || []); ids.has(source.id) ? ids.delete(source.id) : ids.add(source.id);
      try {
        const response = await api(`/api/sessions/${session.id}`, { method: 'PATCH', body: { source_ids: [...ids] } });
        Object.assign(session, response.item);
        this.ctx.toast(ids.has(source.id) ? `已将“${source.name}”加入当前分析` : `已将“${source.name}”移出当前分析`, '分析范围已更新');
      } catch (error) { this.ctx.fail(error); }
    },
    async loadProfile() { const result = await api(`/api/sources/${this.activeId}/profile`); this.profile = result.profile; this.detailTab = 'profile'; },
    async applyClean() {
      const operations = Object.entries(this.cleanOps).filter(([, enabled]) => enabled).map(([type]) => ({ type, strategy: 'median' }));
      await this.ctx.run('正在生成清洗后的派生数据集', async () => {
        const result = await api(`/api/sources/${this.activeId}/clean/apply`, { method: 'POST', body: { operations, workspace_id: this.state.workspaceId } });
        this.state.sources.unshift(result.item); await this.attachToCurrentSession([result.item]);
        this.ctx.toast('已保留原始数据，清洗版已加入当前分析', '数据处理完成');
      });
    },
    async saveGovernance() {
      const payload = { name: this.governance.name, description: this.governance.description, classification: this.governance.classification, sensitivity: this.governance.sensitivity, retention_policy: this.governance.retention_policy };
      if (this.isWorkspaceOwner) payload.authorized_user_ids = this.governance.restricted ? this.governance.authorized_user_ids : null;
      const result = await api(`/api/sources/${this.activeId}`, { method: 'PATCH', body: payload });
      const index = this.state.sources.findIndex(item => item.id === this.activeId); if (index >= 0) this.state.sources[index] = result.item;
      this.ctx.toast('权限变更会立即作用于查询、Agent、结果和导出','治理策略已保存');
    },
    async remove(source) {
      if (!confirm(`归档数据源“${source.name}”？原始记录可从回收站恢复。`)) return;
      await api(`/api/sources/${source.id}`, { method: 'DELETE' }); this.state.sources = this.state.sources.filter(item => item.id !== source.id); if (this.activeId === source.id) this.activeId = '';
    },
  },
  template: `
    <section class="workspace-page">
      <header class="surface-header page-heading enterprise-hero"><div><span class="eyebrow">DATA ASSETS</span><h1>数据资产</h1><p>统一接入数据库、文件与业务接口，明确可分析表范围、访问权限和数据预览证据。</p></div><div class="header-cluster"><button v-if="!hasDemoSource" class="button button--quiet" @click="loadDemo"><Icon name="play"/>载入演示数据</button><button class="button button--primary" @click="connectOpen=true"><Icon name="plus"/>新建连接</button><label class="button"><Icon name="upload"/>上传文件<input hidden multiple type="file" accept=".csv,.tsv,.xlsx,.xls,.json,.parquet" @change="upload"></label></div></header>
      <section class="enterprise-kpis">
        <article><small>数据源</small><b>{{ state.sources.length }}</b><span>已接入资产</span></article>
        <article><small>当前分析范围</small><b>{{ ctx.activeSession()?.source_ids?.length || 0 }}</b><span>已授权给本会话</span></article>
        <article><small>数据库连接</small><b>{{ state.sources.filter(item=>item.kind==='database').length }}</b><span>支持表级范围控制</span></article>
        <article><small>文件数据集</small><b>{{ state.sources.filter(item=>item.kind==='file').length }}</b><span>自动生成预览与画像</span></article>
      </section>
      <div class="catalog-layout">
        <aside class="record-rail">
          <div class="rail-heading"><b>数据源</b><span>{{ state.sources.length }}</span></div><div class="source-set-bar"><select v-model="selectedSet" @change="applySet"><option value="">已保存组合</option><option v-for="item in sourceSets" :key="item.id" :value="item.id">{{ item.name }}</option></select><button @click="saveSet" title="保存当前组合"><Icon name="plus"/></button></div>
          <button v-for="source in state.sources" :key="source.id" class="record-row" :class="{ active: source.id === activeId }" @click="select(source.id)">
            <span class="record-icon"><Icon :name="source.kind === 'database' ? 'database' : 'table'"/></span><span><b>{{ source.name }}</b><small>{{ source.kind }} · {{ source.tables?.length || 0 }} 张表</small></span><StatusPill :status="source.status"/>
          </button>
          <EmptyState v-if="!state.sources.length" icon="database" title="还没有数据源" text="上传文件或连接数据库、业务 API。"/>
        </aside>
        <main v-if="active" class="detail-pane">
          <div class="detail-title asset-title"><div><span class="eyebrow">{{ {file:'文件',database:'数据库',http:'业务接口',sheets:'在线表格',lark:'飞书表格'}[active.kind] || active.kind }}资产</span><h2>{{ active.name }}</h2><p>{{ active.filename || active.endpoint || '派生数据集' }}</p></div><div class="header-cluster"><label class="check-control source-scope-control"><input type="checkbox" :checked="ctx.activeSession()?.source_ids?.includes(active.id)" @change="toggleUse(active)">{{ ctx.activeSession()?.source_ids?.includes(active.id) ? '已加入当前分析' : '加入当前分析' }}</label><button class="icon-button danger" @click="remove(active)" title="归档"><Icon name="close"/></button></div></div>
          <section v-if="active.kind==='database'" class="database-table-scope"><header><div><b>数据表范围</b><small>勾选 Agent 可以查询的表，点击表名切换预览</small></div><button class="button button--quiet" @click="toggleAllTables">{{ selectedTables.length === (active.tables || []).length ? '清空' : '全选' }}</button></header><div class="database-table-list"><div v-for="table in active.tables" :key="table.source_name || table.name" class="database-table-item" :class="{active:previewTable===(table.source_name || table.name)}"><label><input type="checkbox" :value="table.source_name || table.name" v-model="selectedTables" @change="saveTableSelection"><span><b>{{ table.name }}</b><small>{{ table.object_type==='view' ? '视图' : '数据表' }} · {{ table.columns || table.schema?.length || 0 }} 个字段</small></span></label><button @click="loadPreview(table.source_name || table.name)">预览</button></div></div></section>
          <nav class="tab-bar"><button :class="{active:detailTab==='preview'}" @click="loadPreview(previewTable)">数据预览</button><button v-if="active.kind!=='database'" :class="{active:detailTab==='profile'}" @click="loadProfile">数据质量</button><button v-if="active.kind!=='database'" :class="{active:detailTab==='clean'}" @click="detailTab='clean'">数据处理</button><button :class="{active:detailTab==='governance'}" @click="detailTab='governance'">访问权限</button></nav>
          <div v-if="detailTab==='preview' && preview" class="panel-stack"><div class="metric-strip"><div><small>{{ preview.sampled ? '预览行数' : '记录数' }}</small><b>{{ ctx.number(preview.rows) }}</b></div><div><small>字段数</small><b>{{ preview.columns.length }}</b></div><div><small>数据表</small><b>{{ preview.table }}</b></div></div><p v-if="preview.sampled" class="form-hint">远程数据库仅执行有上限的只读预览，不会将整张表加载到应用内存。</p><DataTable :rows="preview.data" :columns="preview.columns"/></div>
          <div v-if="detailTab==='profile'" class="panel-stack"><div v-if="profile" class="metric-strip"><div class="score"><small>质量评分</small><b>{{ profile.quality_score }}</b><em>/100</em></div><div><small>缺失单元格</small><b>{{ ctx.number(profile.missing_cells) }}</b></div><div><small>重复记录</small><b>{{ ctx.number(profile.duplicate_rows) }}</b></div><div><small>数值字段</small><b>{{ profile.numeric_columns.length }}</b></div></div><DataTable v-if="profile" :rows="profile.columns"/><EmptyState v-else icon="chart" title="尚未生成画像" text="点击“质量画像”即可检查缺失、重复、分布和异常值。"/></div>
          <div v-if="detailTab==='clean'" class="panel-stack"><div class="settings-card"><h3>非破坏性数据处理</h3><p>处理结果会保存为新的派生数据集，原始数据保持不变。</p><div class="option-grid"><label><input v-model="cleanOps.drop_duplicates" type="checkbox">删除重复记录</label><label><input v-model="cleanOps.trim_text" type="checkbox">清理文本空白</label><label><input v-model="cleanOps.fill_missing" type="checkbox">用中位数/众数填补缺失</label><label><input v-model="cleanOps.winsorize" type="checkbox">1%–99% 缩尾处理</label></div><button class="button button--primary" @click="applyClean">生成派生数据集</button></div></div>
          <div v-if="detailTab==='governance'" class="panel-stack"><div class="settings-card"><h3>数据资产信息</h3><p>这些策略会贯穿预览、查询、Agent、派生表、看板与导出。</p><div class="form-grid"><label><span>名称</span><input v-model.trim="governance.name"></label><label><span>分类</span><select v-model="governance.classification"><option value="public">公开</option><option value="internal">内部</option><option value="confidential">机密</option><option value="restricted">严格受限</option></select></label><label><span>敏感级别</span><select v-model="governance.sensitivity"><option value="public">公开</option><option value="internal">内部</option><option value="confidential">机密</option><option value="restricted">严格受限</option></select></label><label><span>保留策略</span><input v-model="governance.retention_policy" placeholder="例如：financial-7y"></label><label class="span-2"><span>资产说明</span><textarea v-model="governance.description"></textarea></label></div></div><div class="settings-card"><h3>细粒度访问范围</h3><p v-if="isWorkspaceOwner">留空表示工作空间成员按角色访问；开启后只有勾选成员可看到并使用。当前所有者必须保留访问权。</p><p v-else>仅工作空间所有者可修改成员白名单；你仍可维护非权限类资产信息。</p><label class="check-control"><input type="checkbox" v-model="governance.restricted" :disabled="!isWorkspaceOwner">启用数据源成员白名单</label><div v-if="governance.restricted" class="option-grid governance-members"><label v-for="member in members" :key="member.user_id"><input type="checkbox" :value="member.user_id" v-model="governance.authorized_user_ids" :disabled="!isWorkspaceOwner">{{ member.name || member.email }} <small>{{ member.role }}</small></label></div><button class="button button--primary" @click="saveGovernance">保存治理策略</button></div></div>
        </main>
        <main v-else class="detail-pane detail-pane--empty"><EmptyState icon="database" title="选择一个数据源" text="查看结构、执行只读查询、维护治理策略。"/></main>
      </div>
      <Modal :open="connectOpen" title="连接外部数据" @close="connectOpen=false">
        <nav class="segmented"><button :class="{active:connectMode==='database'}" @click="connectMode='database'">SQL</button><button :class="{active:connectMode==='http'}" @click="connectMode='http'">HTTP</button><button :class="{active:connectMode==='sheets'}" @click="connectMode='sheets'">Sheets</button><button :class="{active:connectMode==='lark'}" @click="connectMode='lark'">飞书表格</button></nav>
        <div v-if="connectMode==='database'" class="form-grid"><label><span>连接名称</span><input v-model="dbForm.name" placeholder="生产经营库"></label><label><span>数据库类型</span><select v-model="dbForm.driver" @change="syncDatabaseDefaults"><option value="sqlite">SQLite</option><option value="postgresql">PostgreSQL</option><option value="mysql">MySQL</option><option value="sqlserver">SQL Server</option></select></label><label class="span-2"><span>{{ dbForm.driver==='sqlite' ? 'SQLite 文件路径' : '数据库名称' }}</span><input v-model="dbForm.database" :placeholder="dbForm.driver==='sqlite' ? '例如 C:\\data\\sales.sqlite' : '例如 sales' "></label><template v-if="dbForm.driver!=='sqlite'"><label><span>主机</span><input v-model="dbForm.host" placeholder="127.0.0.1"></label><label><span>端口</span><input v-model="dbForm.port" inputmode="numeric"></label><label><span>用户名</span><input v-model="dbForm.username" autocomplete="username"></label><label><span>密码</span><input v-model="dbForm.password" type="password" autocomplete="current-password"></label><label v-if="dbForm.driver==='mysql'"><span>SSL 模式</span><select v-model="dbForm.ssl_mode"><option value="preferred">优先使用（推荐）</option><option value="disabled">关闭（仅本地开发）</option><option value="required">必须使用</option><option value="verify-ca">验证 CA</option><option value="verify-identity">验证 CA 与主机名</option></select></label><p v-if="dbForm.driver==='mysql'" class="form-hint span-2">连接当前服务器上的 MySQL 时，主机填写 127.0.0.1、端口 3306；不建议填写服务器公网 IP，也不需要开放公网 3306。账号至少需要目标库的 SELECT 和 SHOW VIEW 权限。</p></template></div>
        <div v-else-if="connectMode==='http'" class="form-grid"><label><span>连接名称</span><input v-model="httpForm.name" placeholder="订单服务"></label><label class="span-2"><span>JSON 地址</span><input v-model="httpForm.url" placeholder="https://api.example.com/orders"></label><label class="span-2"><span>数据路径（可选）</span><input v-model="httpForm.json_path" placeholder="data.items"></label></div>
        <div v-else-if="connectMode==='sheets'" class="form-grid"><label><span>连接名称</span><input v-model="sheetForm.name"></label><label class="span-2"><span>公开 Google Sheets 链接或 ID</span><input v-model="sheetForm.url" placeholder="https://docs.google.com/spreadsheets/d/…"></label><label><span>工作表 GID</span><input v-model="sheetForm.gid"></label></div>
        <div v-else class="form-grid"><label><span>连接名称</span><input v-model="larkForm.name"></label><label><span>App ID</span><input v-model="larkForm.app_id"></label><label><span>App Secret</span><input type="password" v-model="larkForm.app_secret"></label><label><span>App Token</span><input v-model="larkForm.app_token"></label><label><span>Table ID</span><input v-model="larkForm.table_id"></label></div>
        <template #footer><button class="button" @click="connectOpen=false">取消</button><button class="button button--primary" @click="connect">验证并连接</button></template>
      </Modal>
    </section>`,
};

export const KnowledgePanel = {
  components: { EmptyState, Icon, StatusPill }, props: { ctx: Object },
  data: () => ({
    tab: 'documents', documents: [], entries: [], categories: [], query: '', results: [],
    preview: [], importFilename: '', importFormat: '', parsing: false,
    promptState: { temp_prompt: '', enabled: false, max_chars: 4000 },
  }),
  computed: {
    metrics() { return this.entries.filter(item => item.type === 'metric'); },
    rules() { return this.entries.filter(item => item.type === 'business_rule'); },
    notes() { return this.entries.filter(item => item.type === 'context_note'); },
  },
  mounted() { this.load(); },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const [documents, entries, categories] = await Promise.all([
        api(withWorkspace('/api/knowledge/documents', wid)), api(withWorkspace('/api/knowledge/entries', wid)),
        api(withWorkspace('/api/knowledge/categories', wid)),
      ]);
      this.documents = documents.items; this.entries = entries.items; this.categories = categories.items;
      const session = this.ctx.activeSession();
      if (session) this.promptState = await api(withWorkspace(`/api/sessions/${session.id}/temp-prompt`, wid));
    },
    async uploadDocument(event) {
      const file = event.target.files[0]; if (!file) return;
      const form = new FormData(); form.append('file', file); form.append('workspace_id', this.ctx.state.workspaceId);
      await this.ctx.run('正在建立知识索引', async () => { await api('/api/knowledge/documents', { method: 'POST', body: form }); await this.load(); }); event.target.value = '';
    },
    async parseImport(event) {
      const file = event.target.files[0]; if (!file) return;
      const form = new FormData(); form.append('file', file); form.append('workspace_id', this.ctx.state.workspaceId);
      const provider = this.ctx.activeSession()?.provider_id; if (provider) form.append('provider', provider);
      this.parsing = true;
      try {
        const result = await api('/api/knowledge/parse', { method: 'POST', body: form });
        this.preview = result.preview || []; this.importFilename = result.filename; this.importFormat = result.format; this.tab = 'import';
      } finally { this.parsing = false; event.target.value = ''; }
    },
    async confirmImport() {
      await this.ctx.run('正在确认并建立知识索引', async () => {
        await api(withWorkspace('/api/knowledge/confirm', this.ctx.state.workspaceId), { method: 'POST', body: { filename: this.importFilename, records: this.preview } });
        this.preview = []; this.importFilename = ''; this.tab = 'structured'; await this.load();
      });
    },
    fields(item) {
      if (item.table === 'metrics') return ['name', 'alias', 'definition', 'sql_template', 'notes'];
      if (item.table === 'business_rules') return ['rule_id', 'description', 'condition', 'severity'];
      return ['topic', 'content', 'tags'];
    },
    removePreview(index) { this.preview.splice(index, 1); },
    async search() { if (!this.query.trim()) return; this.results = (await api('/api/knowledge/search', { method: 'POST', body: { query: this.query, workspace_id: this.ctx.state.workspaceId } })).items; },
    async toggleDocument(item) { item.enabled = !item.enabled; await api(`/api/knowledge/documents/${item.id}`, { method: 'PATCH', body: { enabled: item.enabled } }); },
    async removeDocument(item) { if (!confirm(`归档知识文档“${item.name}”？`)) return; await api(`/api/knowledge/documents/${item.id}`, { method: 'DELETE' }); await this.load(); },
    async toggleEntry(item) {
      const path = item.type === 'metric' ? 'metrics' : item.type === 'business_rule' ? 'rules' : 'notes';
      const updated = await api(withWorkspace(`/api/knowledge/${path}/${item.id}/toggle`, this.ctx.state.workspaceId), { method: 'POST' });
      Object.assign(item, updated);
    },
    async addEntry(type) {
      const name = prompt(type === 'metric' ? '指标名称' : type === 'business_rule' ? '规则 ID' : '知识主题'); if (!name) return;
      const detail = prompt(type === 'metric' ? '指标定义' : type === 'business_rule' ? '规则描述' : '知识内容') || '';
      const path = type === 'metric' ? 'metrics' : type === 'business_rule' ? 'rules' : 'notes';
      const payload = type === 'metric' ? { name, definition: detail } : type === 'business_rule' ? { rule_id: name, description: detail } : { topic: name, content: detail };
      await api(withWorkspace(`/api/knowledge/${path}`, this.ctx.state.workspaceId), { method: 'POST', body: payload }); await this.load();
    },
    async savePrompt(raw) {
      const session = this.ctx.activeSession(); if (!session) return;
      this.promptState = await api(withWorkspace(`/api/sessions/${session.id}/temp-prompt`, this.ctx.state.workspaceId), { method: 'POST', body: { text: this.promptState.temp_prompt, raw, provider: session.provider_id } });
      this.ctx.toast(this.promptState.warning || '', this.promptState.enabled ? '临时指令已启用' : '临时指令已清空');
    },
    async togglePrompt() {
      const session = this.ctx.activeSession(); if (!session) return;
      this.promptState = await api(withWorkspace(`/api/sessions/${session.id}/temp-prompt/toggle`, this.ctx.state.workspaceId), { method: 'POST' });
    },
  },
  template: `<section class="workspace-page"><header class="surface-header page-heading enterprise-hero"><div><span class="eyebrow">KNOWLEDGE BASE</span><h1>知识库</h1><p>沉淀业务口径、背景材料和分析规则，让 Agent 按需检索、引用来源并保留可追溯证据。</p></div><div class="header-cluster"><label class="button"><Icon name="upload"/>导入知识文档<input hidden type="file" accept=".txt,.md,.html,.csv,.json,.pdf,.docx,.xlsx,.xls" @change="uploadDocument"></label><label class="button button--primary"><Icon name="brain"/>{{ parsing?'正在解析':'解析结构化知识' }}<input hidden type="file" accept=".docx,.xlsx,.xls" @change="parseImport"></label></div></header>
    <section class="enterprise-kpis">
      <article><small>索引文档</small><b>{{ documents.length }}</b><span>可检索引用</span></article>
      <article><small>业务指标</small><b>{{ metrics.length }}</b><span>结构化口径</span></article>
      <article><small>业务规则</small><b>{{ rules.length }}</b><span>解释与限制</span></article>
      <article><small>背景知识</small><b>{{ notes.length }}</b><span>分析上下文</span></article>
    </section>
    <nav class="page-tabs"><button :class="{active:tab==='documents'}" @click="tab='documents'">文档索引</button><button :class="{active:tab==='structured'}" @click="tab='structured'">指标·规则·背景</button><button :class="{active:tab==='search'}" @click="tab='search'">检索验收</button><button :class="{active:tab==='prompt'}" @click="tab='prompt'">会话临时指令</button><button v-if="preview.length" :class="{active:tab==='import'}" @click="tab='import'">待确认 <span>{{ preview.length }}</span></button></nav>
    <div v-if="tab==='documents'" class="knowledge-grid"><section class="content-card"><div class="card-heading"><div><h2>知识文档</h2><p>{{ documents.length }} 份已索引材料</p></div></div><div class="document-list"><article v-for="item in documents" :key="item.id"><span class="record-icon"><Icon name="book"/></span><div><b>{{ item.name }}</b><small>{{ item.format.toUpperCase() }} · {{ ctx.number(item.characters) }} 字符 · {{ item.chunk_count || 0 }} 片段</small><div class="tag-row"><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div></div><button class="switch" :class="{on:item.enabled}" @click="toggleDocument(item)"><i></i></button><button class="icon-button danger" @click="removeDocument(item)"><Icon name="close"/></button></article><EmptyState v-if="!documents.length" icon="book" title="知识库还是空的" text="可直接索引文档，也可先解析出指标与规则再确认。"/></div></section><section class="content-card"><div class="card-heading"><div><h2>导入原则</h2><p>可回溯、可停用、不整库注入</p></div></div><div class="settings-card"><h3>按需检索</h3><p>Agent 仅在需要解释业务口径时调用知识检索，并在结果中保留来源引用。</p></div><div class="settings-card"><h3>确认前不生效</h3><p>Excel/Word 结构化解析结果允许逐条修改或删除，只有点击确认后才会入库。</p></div></section></div>
    <div v-else-if="tab==='structured'" class="structured-grid structured-knowledge-grid"><section v-for="group in [{title:'业务指标',type:'metric',items:metrics},{title:'业务规则',type:'business_rule',items:rules},{title:'背景知识',type:'context_note',items:notes}]" :key="group.type" class="content-card knowledge-column"><div class="card-heading"><div><h2>{{ group.title }}</h2><p>{{ group.items.length }} 条 · 可被 Agent 检索引用</p></div><button class="button button--small" @click="addEntry(group.type)"><Icon name="plus"/>新增</button></div><div class="structured-entry-list"><article v-for="item in group.items" :key="item.id" class="structured-entry-card"><div class="structured-entry-card__main"><b :title="item.name">{{ item.name }}</b><small :title="item.definition || item.description || item.content">{{ item.definition || item.description || item.content || '暂无描述' }}</small></div><button class="switch" :class="{on:item.enabled}" @click="toggleEntry(item)" :aria-label="item.enabled?'停用':'启用'"><i></i></button></article><EmptyState v-if="!group.items.length" icon="book" title="暂无条目" text="手动新增，或从知识文件解析导入。"/></div></section></div>
    <div v-else-if="tab==='import'" class="content-card import-preview"><div class="card-heading"><div><h2>导入预览</h2><p>{{ importFormat }} · {{ importFilename }} · 确认前可编辑</p></div><button class="button button--primary" @click="confirmImport"><Icon name="check"/>确认 {{ preview.length }} 条并建立索引</button></div><div class="preview-records"><article v-for="(item,index) in preview" :key="index" class="settings-card"><header><b>{{ item.table }}</b><button class="icon-button danger" @click="removePreview(index)"><Icon name="close"/></button></header>
<div class="form-grid"><label v-for="field in fields(item)" :key="field" :class="{ 'span-2':['definition','sql_template','notes','description','condition','content'].includes(field) }"><span>{{ field }}</span><textarea v-if="['definition','sql_template','notes','description','condition','content'].includes(field)" v-model="item[field]"></textarea><input v-else v-model="item[field]"></label></div></article></div></div>
    <div v-else-if="tab==='prompt'" class="content-card prompt-settings"><div class="card-heading"><div><h2>本会话临时指令</h2><p>仅对当前会话每一轮生效，最多 {{ promptState.max_chars }} 字</p></div><StatusPill :status="promptState.enabled?'active':'disabled'"/></div><textarea class="prompt-textarea" v-model="promptState.temp_prompt" :maxlength="promptState.max_chars" placeholder="例如：所有金额换算为万元，结论先行，并单独列出假设。"></textarea><div class="prompt-footer"><small>{{ promptState.temp_prompt.length }} / {{ promptState.max_chars }}</small><div class="row-actions"><button class="button" :disabled="!promptState.temp_prompt" @click="togglePrompt">{{ promptState.enabled?'停用':'启用' }}</button><button class="button" @click="savePrompt(true)">按原文保存</button><button class="button button--primary" @click="savePrompt(false)">用模型整理并保存</button></div></div></div>
    <div v-else class="knowledge-grid"><section class="content-card"><div class="card-heading"><div><h2>检索验收</h2><p>检查 Agent 能否召回正确口径</p></div></div><div class="search-box"><Icon name="search"/><input v-model="query" @keyup.enter="search" placeholder="例如：GMV 的计算口径是什么？"><button @click="search">检索</button></div><div class="search-results"><article v-for="item in results" :key="item.document_id+'-'+item.chunk"><div><b>{{ item.document_name }}</b><span>相关度 {{ Math.round(item.score*100) }}%</span></div><p>{{ item.text }}</p></article><EmptyState v-if="!results.length" icon="search" title="输入问题测试召回" text="结果会显示来源、相关度与原始片段。"/></div></section></div></section>`,
};

export const SemanticPanel = {
  components: { DataTable, EmptyState, Icon, Modal, StatusPill }, props: { ctx: Object },
  data: () => ({
    models: [], metrics: [], schema: null, open: false, editor: 'model', saving: false,
    modelForm: { name: '', description: '', source_id: '', table: '', grain: '', default_time_dimension: '', dimensionsText: '[]', measuresText: '[]' },
    metricForm: { name: '', label: '', description: '', model_id: '', metric_type: 'atomic', measure: '', expression: '', aliases: '', unit: '', format: '', business_object: '', business_event: '', grain: '', time_semantics: '', deduplication: '', business_owner: '', technical_owner: '', certification_note: '', status: 'draft' },
    test: { metric: '', group_by: '', filtersText: '[]', limit: 100 }, testResult: null, testPlan: null,
  }),
  computed: {
    selectedSource() { return this.ctx.state.sources.find(item => item.id === this.modelForm.source_id); },
    selectedModel() { return this.models.find(item => item.id === this.metricForm.model_id); },
    modelMeasures() { return this.selectedModel?.measures || []; },
    dependencyMetrics() { return this.metrics.filter(item => item.model_id === this.metricForm.model_id); },
    approvedMetrics() { return this.metrics.filter(item => item.status === 'approved'); },
    isWorkspaceOwner() { return !this.ctx.state.user || this.ctx.state.workspaceRole === 'owner'; },
  },
  mounted() { this.load(); },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const [models, metrics] = await Promise.all([
        api(withWorkspace('/api/semantic/models', wid)), api(withWorkspace('/api/semantic/metrics', wid)),
      ]);
      this.models = models.items; this.metrics = metrics.items;
      if (!this.test.metric && this.approvedMetrics.length) this.test.metric = this.approvedMetrics[0].id;
    },
    async openModel() {
      const source = this.ctx.selectedSources()[0] || this.ctx.state.sources[0];
      this.modelForm = { name: '', description: '', source_id: source?.id || '', table: '', grain: '', default_time_dimension: '', dimensionsText: '[]', measuresText: '[]' };
      this.editor = 'model'; this.open = true;
      if (source) await this.loadSchema();
    },
    async loadSchema() {
      if (!this.modelForm.source_id) { this.schema = null; return; }
      this.schema = (await api(`/api/sources/${this.modelForm.source_id}/schema`)).schema;
      const first = this.schema.tables?.[0];
      if (!first) return;
      this.modelForm.table = first.name;
      const dimensions = [], measures = [];
      for (const column of first.columns || []) {
        const type = String(column.type || '').toLowerCase();
        if (/int|decimal|numeric|double|float|real/.test(type)) measures.push({ name: column.name, column: column.name, aggregation: 'sum', label: column.name });
        else dimensions.push({ name: column.name, column: column.name, type: /date|time/.test(type) ? 'time' : 'categorical', label: column.name });
      }
      this.modelForm.dimensionsText = JSON.stringify(dimensions, null, 2);
      this.modelForm.measuresText = JSON.stringify(measures, null, 2);
    },
    useTable() {
      const table = this.schema?.tables?.find(item => item.name === this.modelForm.table);
      if (!table) return;
      const dimensions = [], measures = [];
      for (const column of table.columns || []) {
        const type = String(column.type || '').toLowerCase();
        (/int|decimal|numeric|double|float|real/.test(type) ? measures : dimensions).push(
          /int|decimal|numeric|double|float|real/.test(type)
            ? { name: column.name, column: column.name, aggregation: 'sum', label: column.name }
            : { name: column.name, column: column.name, type: /date|time/.test(type) ? 'time' : 'categorical', label: column.name },
        );
      }
      this.modelForm.dimensionsText = JSON.stringify(dimensions, null, 2); this.modelForm.measuresText = JSON.stringify(measures, null, 2);
    },
    openMetric() {
      this.metricForm = { name: '', label: '', description: '', model_id: this.models[0]?.id || '', metric_type: 'atomic', measure: '', expression: '', aliases: '', unit: '', format: '', business_object: '', business_event: '', grain: this.models[0]?.grain || '', time_semantics: '', deduplication: '', business_owner: '', technical_owner: '', certification_note: '', status: 'draft' };
      this.metricForm.measure = this.modelMeasures[0]?.name || ''; this.editor = 'metric'; this.open = true;
    },
    syncMeasure() { this.metricForm.measure = this.modelMeasures[0]?.name || ''; this.metricForm.grain = this.selectedModel?.grain || ''; },
    async save() {
      this.saving = true;
      try {
        if (this.editor === 'model') {
          let dimensions, measures;
          try { dimensions = JSON.parse(this.modelForm.dimensionsText); measures = JSON.parse(this.modelForm.measuresText); }
          catch { throw new Error('维度和度量必须是合法 JSON 数组'); }
          await api('/api/semantic/models', { method: 'POST', body: { ...this.modelForm, dimensions, measures, workspace_id: this.ctx.state.workspaceId } });
        } else {
          if (!this.isWorkspaceOwner && this.metricForm.status === 'approved') throw new Error('只有工作空间所有者可以直接审批指标，请先保存为草稿');
          await api('/api/semantic/metrics', { method: 'POST', body: { ...this.metricForm, aliases: this.metricForm.aliases.split(/[,，\n]/).map(value => value.trim()).filter(Boolean), workspace_id: this.ctx.state.workspaceId } });
        }
        this.open = false; await this.load(); this.ctx.toast('', this.editor === 'model' ? '语义模型已保存' : '指标已保存');
      } catch (error) { this.ctx.fail(error); } finally { this.saving = false; }
    },
    async approve(metric) { if (!this.isWorkspaceOwner) return this.ctx.fail(new Error('仅工作空间所有者可审批正式指标')); await this.ctx.run('正在审批指标',async()=>{await api(`/api/semantic/metrics/${metric.id}`, { method: 'PATCH', body: { status: 'approved' } }); await this.load();}); },
    async remove(kind, item) { if(kind==='metrics'&&!this.isWorkspaceOwner)return this.ctx.fail(new Error('仅工作空间所有者可删除指标'));if (!confirm(`删除“${item.label || item.name}”？`)) return; await this.ctx.run('正在删除语义对象',async()=>{await api(`/api/semantic/${kind}/${item.id}`, { method: 'DELETE' }); await this.load();}); },
    async testMetric() {
      let filters; try { filters = JSON.parse(this.test.filtersText || '[]'); } catch { return this.ctx.fail(new Error('过滤条件必须是合法 JSON 数组')); }
      await this.ctx.run('正在执行受治理指标查询', async () => {
        const result = await api('/api/semantic/query', { method: 'POST', body: { metric: this.test.metric, group_by: this.test.group_by.split(/[,，\n]/).map(value => value.trim()).filter(Boolean), filters, limit: this.test.limit, workspace_id: this.ctx.state.workspaceId } });
        this.testResult = result.result; this.testPlan = result.plan;
      });
    },
  },
  template: `<section class="workspace-page"><header class="surface-header page-heading enterprise-hero"><div><span class="eyebrow">METRICS CENTER</span><h1>指标中心</h1><p>统一管理语义模型、指标口径、审批版本和验收结果，正式分析优先使用已认证指标。</p></div><div class="header-cluster"><button class="button" :disabled="!models.length" @click="openMetric"><Icon name="plus"/>新建指标</button><button class="button button--primary" :disabled="!ctx.state.sources.length" @click="openModel"><Icon name="database"/>新建语义模型</button></div></header>
    <section class="enterprise-kpis">
      <article><small>语义模型</small><b>{{ models.length }}</b><span>已建模事实表</span></article>
      <article><small>已认证指标</small><b>{{ approvedMetrics.length }}</b><span>可用于正式分析</span></article>
      <article><small>草稿指标</small><b>{{ metrics.filter(item=>item.status==='draft').length }}</b><span>待审批发布</span></article>
      <article><small>可用数据资产</small><b>{{ ctx.state.sources.length }}</b><span>支持建模</span></article>
    </section>
    <div class="semantic-studio"><section class="content-card"><div class="card-heading"><div><h2>语义模型</h2><p>{{ models.length }} 个已校验模型</p></div></div><div class="document-list"><article v-for="item in models" :key="item.id"><span class="record-icon"><Icon name="database"/></span><div><b>{{ item.name }} <small>v{{ item.version }}</small></b><small>{{ item.source_table }} · {{ item.dimensions?.length || 0 }} 维度 · {{ item.measures?.length || 0 }} 度量</small></div><StatusPill :status="item.enabled?'ready':'disabled'"/><button class="icon-button danger" @click="remove('models',item)"><Icon name="close"/></button></article><EmptyState v-if="!models.length" icon="database" title="尚未建立语义模型" text="选择数据源和事实表，系统会按字段类型生成可编辑的维度、度量草稿。"/></div></section>
      <section class="content-card"><div class="card-heading"><div><h2>业务指标</h2><p>{{ approvedMetrics.length }} 个已审批 · {{ metrics.length }} 个定义</p></div></div><div class="document-list"><article v-for="item in metrics" :key="item.id"><span class="record-icon"><Icon name="chart"/></span><div><b>{{ item.label || item.name }}</b><small>{{ item.metric_type==='derived'?'派生指标':item.metric_type==='composite'?'复合指标':'原子指标' }} · {{ item.expression || item.measure }} · {{ item.unit || '无单位' }} · v{{ item.version }}</small><div class="tag-row"><span v-if="item.business_owner">业务：{{ item.business_owner }}</span><span v-if="item.technical_owner">技术：{{ item.technical_owner }}</span><span v-for="alias in item.aliases" :key="alias">{{ alias }}</span></div></div><StatusPill :status="item.status"/><button v-if="item.status==='draft'" class="button button--small" @click="approve(item)">审批</button><button class="icon-button danger" @click="remove('metrics',item)"><Icon name="close"/></button></article><EmptyState v-if="!metrics.length" icon="chart" title="尚未定义正式指标" text="先定义原子指标，再用指标公式形成派生和复合指标。"/></div></section>
      <section class="content-card semantic-test"><div class="card-heading"><div><h2>口径验收</h2><p>用结构化维度和过滤条件验证指标结果</p></div></div><div class="form-grid"><label><span>已审批指标</span><select v-model="test.metric"><option value="">请选择</option><option v-for="item in approvedMetrics" :key="item.id" :value="item.id">{{ item.label || item.name }}</option></select></label><label><span>分组维度（逗号分隔）</span><input v-model="test.group_by" placeholder="region, month"></label><label class="span-2"><span>过滤条件 JSON</span><textarea v-model="test.filtersText" placeholder='[{"dimension":"region","op":"=","value":"华东"}]'></textarea></label><label><span>结果上限</span><input type="number" min="1" max="5000" v-model.number="test.limit"></label><button class="button button--primary align-end" :disabled="!test.metric" @click="testMetric"><Icon name="play"/>执行验收</button></div><div v-if="testResult" class="result-block"><div class="block-heading"><div><b>结果可追溯</b><small>{{ testResult.rows }} 行 · metric@version 已写入证据</small></div></div><DataTable :rows="testResult.data" :columns="testResult.columns"/><details><summary>查看编译计划与 SQL</summary><pre class="json-result">{{ JSON.stringify(testPlan, null, 2) }}</pre></details></div></section></div>
    <Modal :open="open" :title="editor==='model'?'新建语义模型':'新建业务指标'" wide @close="open=false"><div v-if="editor==='model'" class="form-grid"><label><span>模型名称</span><input v-model.trim="modelForm.name" placeholder="订单事实模型"></label><label><span>数据源</span><select v-model="modelForm.source_id" @change="loadSchema"><option value="">请选择</option><option v-for="item in ctx.state.sources" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label><span>事实表</span><select v-model="modelForm.table" @change="useTable"><option v-for="item in schema?.tables || []" :key="item.name" :value="item.name">{{ item.name }}</option></select></label><label><span>数据粒度</span><input v-model="modelForm.grain" placeholder="一行一笔订单"></label><label class="span-2"><span>说明</span><input v-model="modelForm.description"></label><label class="span-2"><span>维度 JSON</span><textarea class="code-input semantic-code" v-model="modelForm.dimensionsText"></textarea></label><label class="span-2"><span>度量 JSON（至少一项）</span><textarea class="code-input semantic-code" v-model="modelForm.measuresText"></textarea></label><label><span>默认时间维度</span><input v-model="modelForm.default_time_dimension" placeholder="可选"></label></div><div v-else class="form-grid"><label><span>技术名称</span><input v-model.trim="metricForm.name" placeholder="net_revenue"></label><label><span>业务名称</span><input v-model.trim="metricForm.label" placeholder="净收入"></label><label><span>语义模型</span><select v-model="metricForm.model_id" @change="syncMeasure"><option v-for="item in models" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label><span>指标类型</span><select v-model="metricForm.metric_type"><option value="atomic">原子指标</option><option value="derived">派生指标</option><option value="composite">复合指标</option></select></label><label v-if="metricForm.metric_type==='atomic'"><span>聚合度量</span><select v-model="metricForm.measure"><option v-for="item in modelMeasures" :key="item.name" :value="item.name">{{ item.label || item.name }}</option></select></label><label v-else class="span-2"><span>指标公式</span><input v-model.trim="metricForm.expression" placeholder="gross_sales - refunds"><small>只允许引用同一模型下的指标技术名称及 + - * / %；依赖指标必须先审批。</small><span class="tag-row"><button type="button" v-for="item in dependencyMetrics" :key="item.id" @click="metricForm.expression+=(metricForm.expression?' ':'')+item.name">{{ item.name }}</button></span></label><label><span>业务对象</span><input v-model="metricForm.business_object" placeholder="订单、客户、区域"></label><label><span>业务事件</span><input v-model="metricForm.business_event" placeholder="支付成功、收入确认"></label><label><span>统计粒度</span><input v-model="metricForm.grain" placeholder="一行一笔已支付订单"></label><label><span>时间口径</span><input v-model="metricForm.time_semantics" placeholder="按支付完成时间，自然月"></label><label><span>去重规则</span><input v-model="metricForm.deduplication" placeholder="按订单号去重"></label><label><span>别名</span><input v-model="metricForm.aliases" placeholder="营收, 收入"></label><label><span>单位</span><input v-model="metricForm.unit" placeholder="元"></label><label><span>格式</span><input v-model="metricForm.format" placeholder=",.2f"></label><label><span>业务负责人</span><input v-model="metricForm.business_owner" placeholder="经营管理部 / 张三"></label><label><span>技术负责人</span><input v-model="metricForm.technical_owner" placeholder="数据中心 / 李四"></label><label><span>初始状态</span><select v-model="metricForm.status"><option value="draft">保存草稿</option><option value="approved">审批发布（所有者）</option></select></label><label class="span-2"><span>业务口径说明</span><textarea v-model="metricForm.description"></textarea></label><label class="span-2"><span>认证说明</span><textarea v-model="metricForm.certification_note" placeholder="说明审批依据、适用范围和已知限制"></textarea></label></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="saving" @click="save">{{ saving?'保存中…':'保存并校验' }}</button></template></Modal></section>`,
};
