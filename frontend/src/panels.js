import { api, withWorkspace } from './api.js';
import { ChartView, DataTable, EmptyState, Icon, Modal, StatusPill, renderMarkdown } from './components.js';

const { nextTick } = Vue;

export const ChatPanel = {
  components: { ChartView, DataTable, EmptyState, Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({ prompt: '', selectedSkill: '', showTrace: true }),
  computed: {
    state() { return this.ctx.state; },
    session() { return this.ctx.activeSession(); },
    selectedSources() { return this.ctx.selectedSources(); },
    canSend() { return this.prompt.trim() && this.session && !this.state.chatRunning; },
  },
  methods: {
    md: renderMarkdown,
    async send() {
      const value = this.prompt.trim();
      if (!value) return;
      this.prompt = '';
      if (value.startsWith('/')) return this.ctx.command(value);
      await this.ctx.sendMessage(value, this.selectedSkill);
      await nextTick();
      this.$refs.feed?.scrollTo({ top: this.$refs.feed.scrollHeight, behavior: 'smooth' });
    },
    async setProvider(event) {
      if (!this.session) return;
      this.session.provider_id = event.target.value || null;
      await api(`/api/sessions/${this.session.id}`, { method: 'PATCH', body: { provider_id: this.session.provider_id } });
    },
    usePrompt(value) { this.prompt = value; nextTick(() => this.$refs.composer?.focus()); },
    keydown(event) {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.send(); }
    },
  },
  template: `
    <section class="chat-surface">
      <header class="surface-header chat-header">
        <div><span class="eyebrow">当前分析</span><h1>{{ session?.name || '分析会话' }}</h1></div>
        <div class="header-cluster">
          <span class="source-chip"><i :class="{ on: selectedSources.length }"></i>{{ selectedSources.length ? selectedSources.map(s => s.name).join('、') : '尚未选择数据' }}</span>
          <button class="button button--quiet" @click="ctx.state.jobsOpen = true"><Icon name="workflow"/>任务 <b v-if="state.activeJobs">{{ state.activeJobs }}</b></button>
        </div>
      </header>
      <div ref="feed" class="chat-feed" :class="{ 'chat-feed--empty': !state.messages.length }">
        <div v-if="!state.messages.length" class="welcome-block">
          <div class="welcome-glyph"><span></span><Icon name="brain" :size="32"/></div>
          <span class="eyebrow">从证据到决策</span>
          <h2>今天要从数据中确认什么？</h2>
          <p>选择数据源后直接提问。系统会展示查询口径、执行过程、结果表和图表，所有结论都可回溯。</p>
          <div class="prompt-grid">
            <button @click="usePrompt('概览这份数据，指出最重要的三个发现和数据质量风险')"><Icon name="table"/><span><b>快速概览</b><small>结构、分布与质量风险</small></span></button>
            <button @click="usePrompt('按主要维度比较核心数值指标，找出差异最大的部分')"><Icon name="chart"/><span><b>对比表现</b><small>分组、排名与差异</small></span></button>
            <button @click="usePrompt('分析时间趋势，并标记明显的异常变化')"><Icon name="workflow"/><span><b>诊断趋势</b><small>趋势、波动与异常点</small></span></button>
          </div>
        </div>
        <article v-for="message in state.messages" :key="message.id" class="message" :class="'message--' + message.role">
          <div class="message__meta"><span>{{ message.role === 'user' ? '你' : message.role === 'system' ? '上下文摘要' : '分析助手' }}</span><time>{{ ctx.time(message.created_at) }}</time></div>
          <div class="message__body markdown" v-html="md(message.content)"></div>
          <details v-if="message.metadata?.sql" class="evidence-card">
            <summary><Icon name="table"/> 查询口径 <span>展开 SQL</span></summary>
            <pre><code>{{ message.metadata.sql }}</code></pre>
          </details>
          <ChartView v-if="message.metadata?.chart" :spec="message.metadata.chart" />
          <div v-if="message.metadata?.knowledge_references?.length" class="reference-row"><span>引用</span><button v-for="ref in message.metadata.knowledge_references" :key="ref.document_id">知识片段 {{ ref.chunk + 1 }}</button></div>
          <div v-if="message.metadata?.choices?.length" class="reference-row"><span>请选择</span><button v-for="choice in message.metadata.choices" :key="choice" @click="usePrompt(choice)">{{ choice }}</button></div>
          <div v-if="message.metadata?.artifacts?.length" class="artifact-list"><a v-for="item in message.metadata.artifacts" :key="item.id" class="button button--small" :href="item.download_url"><Icon name="download"/>下载 {{ item.title || item.filename }}</a></div>
          <details v-for="(outline,index) in message.metadata?.outlines||[]" :key="index" class="evidence-card"><summary><Icon name="workflow"/> 交付大纲</summary><pre><code>{{ JSON.stringify(outline, null, 2) }}</code></pre></details>
          <div v-if="message.metadata?.dashboard_ids?.length || message.metadata?.diagram_ids?.length" class="artifact-list"><button v-if="message.metadata.dashboard_ids?.length" class="button button--small" @click="ctx.go('dashboards')"><Icon name="dashboard"/>打开分析看板</button><button v-if="message.metadata.diagram_ids?.length" class="button button--small" @click="ctx.go('maps')"><Icon name="map"/>打开决策图</button></div>
          <div v-if="message.metadata?.tool_result_ids?.length" class="artifact-list"><a v-for="item in message.metadata.tool_result_ids" :key="item" class="button button--small" target="_blank" :href="'/api/sessions/'+session.id+'/tool-results/'+item"><Icon name="table"/>查看完整工具结果</a></div>
        </article>
        <article v-if="state.chatRunning || state.chatDraft" class="message message--assistant message--live">
          <div class="message__meta"><span>分析助手</span><span class="live-dot">正在工作</span></div>
          <ol v-if="state.chatStages.length" class="stage-list">
            <li v-for="stage in state.chatStages" :key="stage.id" :data-status="stage.status"><i></i><span>{{ stage.label }}</span><small>{{ stage.status === 'completed' ? '完成' : '处理中' }}</small></li>
          </ol>
          <details v-if="state.chatDraft.sql" class="evidence-card" open><summary><Icon name="table"/> 即将执行的查询</summary><pre><code>{{ state.chatDraft.sql }}</code></pre></details>
          <DataTable v-if="state.chatDraft.table" :rows="state.chatDraft.table.data" :columns="state.chatDraft.table.columns" max-height="300px"/>
          <ChartView v-if="state.chatDraft.chart" :spec="state.chatDraft.chart" />
          <div v-if="state.chatDraft.artifacts?.length" class="artifact-list"><a v-for="item in state.chatDraft.artifacts" :key="item.id" class="button button--small" :href="item.download_url"><Icon name="download"/>下载 {{ item.title || item.filename }}</a></div>
          <details v-for="(outline,index) in state.chatDraft.outlines||[]" :key="index" class="evidence-card" open><summary><Icon name="workflow"/> 待确认交付大纲</summary><pre><code>{{ JSON.stringify(outline, null, 2) }}</code></pre></details>
          <div v-if="state.chatDraft.question?.choices?.length" class="reference-row"><span>{{ state.chatDraft.question.question }}</span><button v-for="choice in state.chatDraft.question.choices" :key="choice" @click="usePrompt(choice)">{{ choice }}</button></div>
          <div v-if="state.chatDraft.dashboards?.length || state.chatDraft.diagrams?.length" class="artifact-list"><button v-if="state.chatDraft.dashboards?.length" class="button button--small" @click="ctx.go('dashboards')"><Icon name="dashboard"/>打开新看板</button><button v-if="state.chatDraft.diagrams?.length" class="button button--small" @click="ctx.go('maps')"><Icon name="map"/>打开新决策图</button></div>
          <div v-if="state.chatDraft.content" class="message__body markdown" v-html="md(state.chatDraft.content)"></div>
        </article>
      </div>
      <footer class="composer-wrap">
        <div v-if="!selectedSources.length" class="composer-notice"><Icon name="warning"/>尚未选择数据源；仍可提问，但分析助手会先提示连接数据。</div>
        <div class="composer">
          <textarea ref="composer" v-model="prompt" @keydown="keydown" rows="1" placeholder="描述你要验证的问题，或输入 / 查看命令…" aria-label="分析问题"></textarea>
          <div class="composer__bar">
            <div class="composer__tools">
              <select v-model="selectedSkill" aria-label="分析技能"><option value="">通用分析</option><option v-for="skill in state.skills" :key="skill.id" :value="skill.id">{{ skill.name }}</option></select>
              <select :value="session?.provider_id || ''" @change="setProvider" aria-label="模型服务"><option value="">默认模型</option><option v-for="provider in state.providers" :key="provider.id" :value="provider.id">{{ provider.name }}</option></select>
              <button class="icon-button" @click="ctx.go('sources')" title="选择数据源"><Icon name="database"/></button>
            </div>
            <button v-if="state.chatRunning" class="send-button send-button--stop" @click="ctx.stopMessage" aria-label="停止"><Icon name="stop"/></button>
            <button v-else class="send-button" :disabled="!canSend" @click="send" aria-label="发送"><Icon name="play"/></button>
          </div>
        </div>
        <p class="composer-hint">Enter 发送 · Shift + Enter 换行 · 生成内容应结合业务口径复核</p>
      </footer>
    </section>`,
};

export const SourcesPanel = {
  components: { ChartView, DataTable, EmptyState, Icon, Modal, StatusPill },
  props: { ctx: Object },
  data: () => ({
    connectOpen: false, connectMode: 'database',
    dbForm: { name: '', driver: 'sqlite', database: '', host: '', port: '', username: '', password: '' },
    httpForm: { name: '', url: '', json_path: '' },
    sheetForm: { name: 'Google Sheet', url: '', gid: '0' },
    larkForm: { name: '飞书多维表格', app_id: '', app_secret: '', app_token: '', table_id: '' },
    activeId: '', preview: null, profile: null, detailTab: 'preview',
    sql: '', queryResult: null, chart: null, analysisMethod: 'profile', analysisParams: '{}', analysisResult: null,
    cleanOps: { drop_duplicates: true, trim_text: true, fill_missing: false, winsorize: false },
    sourceSets: [], selectedSet: '',
  }),
  computed: {
    state() { return this.ctx.state; },
    active() { return this.state.sources.find(item => item.id === this.activeId) || null; },
  },
  watch: { 'state.sources': { handler(items) { if (!this.activeId && items.length) this.select(items[0].id); }, immediate: true } },
  mounted() { this.loadSets(); },
  methods: {
    async loadSets() { this.sourceSets = (await api(withWorkspace('/api/source-sets', this.state.workspaceId))).items; },
    async saveSet() { const ids=this.ctx.activeSession()?.source_ids||[];if(!ids.length)return this.ctx.fail(new Error('请先为当前会话选择数据源'));const name=prompt('数据组合名称：','常用分析数据')||'';if(!name)return;await api('/api/source-sets',{method:'POST',body:{name,source_ids:ids,workspace_id:this.state.workspaceId}});await this.loadSets();this.ctx.toast('当前数据源组合已保存','保存成功'); },
    async applySet() { if(!this.selectedSet)return;const session=this.ctx.activeSession();if(!session)return;const result=await api(`/api/source-sets/${this.selectedSet}/apply`,{method:'POST',body:{session_id:session.id}});Object.assign(session,result.session);this.ctx.toast('当前会话的数据源已切换','组合已应用'); },
    async upload(event) {
      const files = [...event.target.files];
      if (!files.length) return;
      const form = new FormData(); files.forEach(file => form.append('files', file)); form.append('workspace_id', this.state.workspaceId);
      await this.ctx.run('正在读取数据文件', async () => {
        const result = await api('/api/sources/upload', { method: 'POST', body: form });
        this.state.sources.unshift(...result.items); this.activeId = result.items[0].id; await this.select(this.activeId);
      });
      event.target.value = '';
    },
    async connect() {
      await this.ctx.run('正在验证数据连接', async () => {
        const paths = { database:'/api/sources/database', http:'/api/sources/http', sheets:'/api/sources/google-sheets', lark:'/api/sources/lark-table' };
        const forms = { database:this.dbForm, http:this.httpForm, sheets:this.sheetForm, lark:this.larkForm };
        const path = paths[this.connectMode];
        const form = forms[this.connectMode];
        const result = await api(path, { method: 'POST', body: { ...form, workspace_id: this.state.workspaceId } });
        this.state.sources.unshift(result.item); this.connectOpen = false; await this.select(result.item.id);
      });
    },
    async select(id) {
      this.activeId = id; this.preview = this.profile = this.queryResult = this.chart = this.analysisResult = null;
      await this.ctx.run('', async () => {
        const result = await api(`/api/sources/${id}/preview?limit=100`); this.preview = result.preview;
        this.sql = `SELECT * FROM "${this.preview.table}" LIMIT 200`;
      }, false);
    },
    toggleUse(source) {
      const session = this.ctx.activeSession(); if (!session) return;
      const ids = new Set(session.source_ids || []); ids.has(source.id) ? ids.delete(source.id) : ids.add(source.id);
      session.source_ids = [...ids]; api(`/api/sessions/${session.id}`, { method: 'PATCH', body: { source_ids: session.source_ids } }).catch(this.ctx.fail);
    },
    async loadProfile() { const result = await api(`/api/sources/${this.activeId}/profile`); this.profile = result.profile; this.detailTab = 'profile'; },
    async runQuery() {
      await this.ctx.run('正在执行只读查询', async () => {
        const result = await api('/api/query', { method: 'POST', body: { source_ids: [this.activeId], sql: this.sql, workspace_id: this.state.workspaceId } });
        this.queryResult = result.result; this.detailTab = 'query';
      });
    },
    async createChart() {
      if (!this.queryResult) return;
      const result = await api('/api/charts/spec', { method: 'POST', body: { result_id: this.queryResult.id, title: this.active.name, workspace_id: this.state.workspaceId } });
      this.chart = result.item.spec;
    },
    async runAnalysis() {
      let params = {}; try { params = JSON.parse(this.analysisParams || '{}'); } catch { return this.ctx.fail(new Error('分析参数必须是 JSON')); }
      await this.ctx.run('正在执行统计分析', async () => {
        const result = await api('/api/analysis/run', { method: 'POST', body: { source_id: this.activeId, method: this.analysisMethod, params, workspace_id: this.state.workspaceId } });
        this.analysisResult = result.run.result; this.detailTab = 'analysis';
      });
    },
    async applyClean() {
      const operations = Object.entries(this.cleanOps).filter(([, enabled]) => enabled).map(([type]) => ({ type, strategy: 'median' }));
      await this.ctx.run('正在生成清洗后的派生数据集', async () => {
        const result = await api(`/api/sources/${this.activeId}/clean/apply`, { method: 'POST', body: { operations, workspace_id: this.state.workspaceId } });
        this.state.sources.unshift(result.item); this.ctx.toast('已保留原始数据并生成清洗版', '数据处理完成');
      });
    },
    async remove(source) {
      if (!confirm(`归档数据源“${source.name}”？原始记录可从回收站恢复。`)) return;
      await api(`/api/sources/${source.id}`, { method: 'DELETE' }); this.state.sources = this.state.sources.filter(item => item.id !== source.id); if (this.activeId === source.id) this.activeId = '';
    },
  },
  template: `
    <section class="workspace-page">
      <header class="surface-header"><div><span class="eyebrow">Data catalog</span><h1>数据目录</h1><p>接入、检查、查询和处理分析数据；所有 SQL 默认只读。</p></div><div class="header-cluster"><label class="button button--primary"><Icon name="upload"/>上传文件<input hidden multiple type="file" accept=".csv,.tsv,.xlsx,.xls,.json,.parquet" @change="upload"></label><button class="button" @click="connectOpen=true"><Icon name="plus"/>连接数据</button></div></header>
      <div class="catalog-layout">
        <aside class="record-rail">
          <div class="rail-heading"><b>数据源</b><span>{{ state.sources.length }}</span></div><div class="source-set-bar"><select v-model="selectedSet" @change="applySet"><option value="">已保存组合</option><option v-for="item in sourceSets" :key="item.id" :value="item.id">{{ item.name }}</option></select><button @click="saveSet" title="保存当前组合"><Icon name="plus"/></button></div>
          <button v-for="source in state.sources" :key="source.id" class="record-row" :class="{ active: source.id === activeId }" @click="select(source.id)">
            <span class="record-icon"><Icon :name="source.kind === 'database' ? 'database' : 'table'"/></span><span><b>{{ source.name }}</b><small>{{ source.kind }} · {{ source.tables?.length || 0 }} 张表</small></span><StatusPill :status="source.status"/>
          </button>
          <EmptyState v-if="!state.sources.length" icon="database" title="还没有数据源" text="上传文件或连接数据库、业务 API。"/>
        </aside>
        <main v-if="active" class="detail-pane">
          <div class="detail-title"><div><span class="eyebrow">{{ active.kind }} source</span><h2>{{ active.name }}</h2><p>{{ active.filename || active.endpoint || '派生数据集' }}</p></div><div class="header-cluster"><label class="check-control"><input type="checkbox" :checked="ctx.activeSession()?.source_ids?.includes(active.id)" @change="toggleUse(active)">用于当前会话</label><button class="icon-button danger" @click="remove(active)" title="归档"><Icon name="close"/></button></div></div>
          <nav class="tab-bar"><button :class="{active:detailTab==='preview'}" @click="detailTab='preview'">数据预览</button><button :class="{active:detailTab==='profile'}" @click="loadProfile">质量画像</button><button :class="{active:detailTab==='query'}" @click="detailTab='query'">SQL 控制台</button><button :class="{active:detailTab==='analysis'}" @click="detailTab='analysis'">分析实验室</button><button :class="{active:detailTab==='clean'}" @click="detailTab='clean'">数据处理</button></nav>
          <div v-if="detailTab==='preview' && preview" class="panel-stack"><div class="metric-strip"><div><small>记录数</small><b>{{ ctx.number(preview.rows) }}</b></div><div><small>字段数</small><b>{{ preview.columns.length }}</b></div><div><small>数据表</small><b>{{ preview.table }}</b></div></div><DataTable :rows="preview.data" :columns="preview.columns"/></div>
          <div v-if="detailTab==='profile'" class="panel-stack"><div v-if="profile" class="metric-strip"><div class="score"><small>质量评分</small><b>{{ profile.quality_score }}</b><em>/100</em></div><div><small>缺失单元格</small><b>{{ ctx.number(profile.missing_cells) }}</b></div><div><small>重复记录</small><b>{{ ctx.number(profile.duplicate_rows) }}</b></div><div><small>数值字段</small><b>{{ profile.numeric_columns.length }}</b></div></div><DataTable v-if="profile" :rows="profile.columns"/><EmptyState v-else icon="chart" title="尚未生成画像" text="点击“质量画像”即可检查缺失、重复、分布和异常值。"/></div>
          <div v-if="detailTab==='query'" class="panel-stack"><div class="sql-editor"><header><span>只读 SQL</span><button class="button button--small button--primary" @click="runQuery"><Icon name="play"/>运行</button></header><textarea v-model="sql" spellcheck="false"></textarea></div><div v-if="queryResult" class="result-block"><div class="block-heading"><div><b>查询结果</b><small>{{ queryResult.rows }} 行 · {{ queryResult.columns.length }} 列</small></div><button class="button button--small" @click="createChart"><Icon name="chart"/>生成图表</button></div><DataTable :rows="queryResult.data" :columns="queryResult.columns"/><ChartView v-if="chart" :spec="chart"/></div></div>
          <div v-if="detailTab==='analysis'" class="panel-stack"><div class="form-grid form-grid--inline"><label><span>分析方法</span><select v-model="analysisMethod"><option v-for="method in state.analysisMethods" :key="method.id" :value="method.id">{{ method.name }}</option></select></label><label class="grow"><span>参数 JSON</span><input v-model="analysisParams" placeholder='{"columns":["sales"]}'></label><button class="button button--primary align-end" @click="runAnalysis"><Icon name="play"/>执行分析</button></div><pre v-if="analysisResult" class="json-result">{{ JSON.stringify(analysisResult, null, 2) }}</pre><EmptyState v-else icon="brain" title="选择方法开始分析" text="支持质量画像、相关性、分层、聚类、显著性检验、回归、随机森林、预测和异常检测。"/></div>
          <div v-if="detailTab==='clean'" class="panel-stack"><div class="settings-card"><h3>非破坏性数据处理</h3><p>处理结果会保存为新的派生数据集，原始数据保持不变。</p><div class="option-grid"><label><input v-model="cleanOps.drop_duplicates" type="checkbox">删除重复记录</label><label><input v-model="cleanOps.trim_text" type="checkbox">清理文本空白</label><label><input v-model="cleanOps.fill_missing" type="checkbox">用中位数/众数填补缺失</label><label><input v-model="cleanOps.winsorize" type="checkbox">1%–99% 缩尾处理</label></div><button class="button button--primary" @click="applyClean">生成派生数据集</button></div></div>
        </main>
        <main v-else class="detail-pane detail-pane--empty"><EmptyState icon="database" title="选择一个数据源" text="查看结构、执行只读查询或进入分析实验室。"/></main>
      </div>
      <Modal :open="connectOpen" title="连接外部数据" @close="connectOpen=false">
        <nav class="segmented"><button :class="{active:connectMode==='database'}" @click="connectMode='database'">SQL</button><button :class="{active:connectMode==='http'}" @click="connectMode='http'">HTTP</button><button :class="{active:connectMode==='sheets'}" @click="connectMode='sheets'">Sheets</button><button :class="{active:connectMode==='lark'}" @click="connectMode='lark'">飞书表格</button></nav>
        <div v-if="connectMode==='database'" class="form-grid"><label><span>连接名称</span><input v-model="dbForm.name" placeholder="生产经营库"></label><label><span>数据库类型</span><select v-model="dbForm.driver"><option value="sqlite">SQLite</option><option value="postgresql">PostgreSQL</option><option value="mysql">MySQL</option><option value="sqlserver">SQL Server</option></select></label><label class="span-2"><span>数据库 / SQLite 文件路径</span><input v-model="dbForm.database" placeholder="database 或 /path/to/file.sqlite"></label><template v-if="dbForm.driver!=='sqlite'"><label><span>主机</span><input v-model="dbForm.host"></label><label><span>端口</span><input v-model="dbForm.port"></label><label><span>用户名</span><input v-model="dbForm.username"></label><label><span>密码</span><input v-model="dbForm.password" type="password"></label></template></div>
        <div v-else-if="connectMode==='http'" class="form-grid"><label><span>连接名称</span><input v-model="httpForm.name" placeholder="订单服务"></label><label class="span-2"><span>JSON 地址</span><input v-model="httpForm.url" placeholder="https://api.example.com/orders"></label><label class="span-2"><span>数据路径（可选）</span><input v-model="httpForm.json_path" placeholder="data.items"></label></div>
        <div v-else-if="connectMode==='sheets'" class="form-grid"><label><span>连接名称</span><input v-model="sheetForm.name"></label><label class="span-2"><span>公开 Google Sheets 链接或 ID</span><input v-model="sheetForm.url" placeholder="https://docs.google.com/spreadsheets/d/…"></label><label><span>工作表 GID</span><input v-model="sheetForm.gid"></label></div>
        <div v-else class="form-grid"><label><span>连接名称</span><input v-model="larkForm.name"></label><label><span>App ID</span><input v-model="larkForm.app_id"></label><label><span>App Secret</span><input type="password" v-model="larkForm.app_secret"></label><label><span>App Token</span><input v-model="larkForm.app_token"></label><label><span>Table ID</span><input v-model="larkForm.table_id"></label></div>
        <template #footer><button class="button" @click="connectOpen=false">取消</button><button class="button button--primary" @click="connect">验证并连接</button></template>
      </Modal>
    </section>`,
};

export const KnowledgePanelLegacy = {
  components: { EmptyState, Icon, StatusPill }, props: { ctx: Object },
  data: () => ({ items: [], query: '', results: [], loading: false }),
  mounted() { this.load(); },
  methods: {
    async load() { this.items = (await api(withWorkspace('/api/knowledge/documents', this.ctx.state.workspaceId))).items; },
    async upload(event) { const file = event.target.files[0]; if (!file) return; const form = new FormData(); form.append('file', file); form.append('workspace_id', this.ctx.state.workspaceId); await this.ctx.run('正在建立知识索引', async () => { await api('/api/knowledge/documents', { method:'POST', body:form }); await this.load(); }); event.target.value=''; },
    async search() { if (!this.query.trim()) return; this.results = (await api('/api/knowledge/search', { method:'POST', body:{ query:this.query, workspace_id:this.ctx.state.workspaceId } })).items; },
    async toggle(item) { item.enabled = !item.enabled; await api(`/api/knowledge/documents/${item.id}`, { method:'PATCH', body:{enabled:item.enabled} }); },
    async remove(item) { if (!confirm(`归档知识文档“${item.name}”？`)) return; await api(`/api/knowledge/documents/${item.id}`, {method:'DELETE'}); await this.load(); },
  },
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Grounded context</span><h1>业务知识</h1><p>把指标口径、制度、研究材料和业务规则变成可引用的分析上下文。</p></div><label class="button button--primary"><Icon name="upload"/>添加文档<input hidden type="file" accept=".txt,.md,.html,.csv,.json,.pdf,.docx,.xlsx,.xls" @change="upload"></label></header><div class="knowledge-grid"><section class="content-card"><div class="card-heading"><div><h2>知识文档</h2><p>{{ items.length }} 份已索引材料</p></div></div><div class="document-list"><article v-for="item in items" :key="item.id"><span class="record-icon"><Icon name="book"/></span><div><b>{{ item.name }}</b><small>{{ item.format.toUpperCase() }} · {{ ctx.number(item.characters) }} 字符 · {{ item.chunks?.length || 0 }} 片段</small><div class="tag-row"><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div></div><button class="switch" :class="{on:item.enabled}" @click="toggle(item)" :aria-label="item.enabled?'停用':'启用'"><i></i></button><button class="icon-button danger" @click="remove(item)"><Icon name="close"/></button></article><EmptyState v-if="!items.length" icon="book" title="知识库还是空的" text="添加指标字典、业务规则或报告，让回答有组织语境。"/></div></section><section class="content-card search-card"><div class="card-heading"><div><h2>检索测试</h2><p>检查分析时能否召回正确口径</p></div></div><div class="search-box"><Icon name="search"/><input v-model="query" @keyup.enter="search" placeholder="例如：GMV 的计算口径是什么？"><button @click="search">检索</button></div><div class="search-results"><article v-for="item in results" :key="item.document_id+'-'+item.chunk"><div><b>{{ item.document_name }}</b><span>相关度 {{ Math.round(item.score*100) }}%</span></div><p>{{ item.text }}</p></article><EmptyState v-if="!results.length" icon="search" title="输入问题测试召回" text="结果会显示来源、相关度与原始片段。"/></div></section></div></section>`,
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
      if (session) this.promptState = await api(withWorkspace(`/api/session/${session.id}/temp-prompt`, wid));
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
      this.promptState = await api(withWorkspace(`/api/session/${session.id}/temp-prompt`, this.ctx.state.workspaceId), { method: 'POST', body: { text: this.promptState.temp_prompt, raw, provider: session.provider_id } });
      this.ctx.toast(this.promptState.warning || '', this.promptState.enabled ? '临时指令已启用' : '临时指令已清空');
    },
    async togglePrompt() {
      const session = this.ctx.activeSession(); if (!session) return;
      this.promptState = await api(withWorkspace(`/api/session/${session.id}/temp-prompt/toggle`, this.ctx.state.workspaceId), { method: 'POST' });
    },
  },
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Grounded context</span><h1>业务知识</h1><p>先预览和校对结构化知识，确认后才进入 Agent 可检索上下文。</p></div><div class="header-cluster"><label class="button"><Icon name="upload"/>直接索引文档<input hidden type="file" accept=".txt,.md,.html,.csv,.json,.pdf,.docx,.xlsx,.xls" @change="uploadDocument"></label><label class="button button--primary"><Icon name="brain"/>{{ parsing?'正在解析':'解析并预览' }}<input hidden type="file" accept=".docx,.xlsx,.xls" @change="parseImport"></label></div></header>
    <nav class="page-tabs"><button :class="{active:tab==='documents'}" @click="tab='documents'">文档索引</button><button :class="{active:tab==='structured'}" @click="tab='structured'">指标·规则·背景</button><button :class="{active:tab==='search'}" @click="tab='search'">检索验收</button><button :class="{active:tab==='prompt'}" @click="tab='prompt'">会话临时指令</button><button v-if="preview.length" :class="{active:tab==='import'}" @click="tab='import'">待确认 <span>{{ preview.length }}</span></button></nav>
    <div v-if="tab==='documents'" class="knowledge-grid"><section class="content-card"><div class="card-heading"><div><h2>知识文档</h2><p>{{ documents.length }} 份已索引材料</p></div></div><div class="document-list"><article v-for="item in documents" :key="item.id"><span class="record-icon"><Icon name="book"/></span><div><b>{{ item.name }}</b><small>{{ item.format.toUpperCase() }} · {{ ctx.number(item.characters) }} 字符 · {{ item.chunk_count || 0 }} 片段</small><div class="tag-row"><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div></div><button class="switch" :class="{on:item.enabled}" @click="toggleDocument(item)"><i></i></button><button class="icon-button danger" @click="removeDocument(item)"><Icon name="close"/></button></article><EmptyState v-if="!documents.length" icon="book" title="知识库还是空的" text="可直接索引文档，也可先解析出指标与规则再确认。"/></div></section><section class="content-card"><div class="card-heading"><div><h2>导入原则</h2><p>可回溯、可停用、不整库注入</p></div></div><div class="settings-card"><h3>按需检索</h3><p>Agent 仅在需要解释业务口径时调用知识检索，并在结果中保留来源引用。</p></div><div class="settings-card"><h3>确认前不生效</h3><p>Excel/Word 结构化解析结果允许逐条修改或删除，只有点击确认后才会入库。</p></div></section></div>
    <div v-else-if="tab==='structured'" class="structured-grid"><section v-for="group in [{title:'业务指标',type:'metric',items:metrics},{title:'业务规则',type:'business_rule',items:rules},{title:'背景知识',type:'context_note',items:notes}]" :key="group.type" class="content-card"><div class="card-heading"><div><h2>{{ group.title }}</h2><p>{{ group.items.length }} 条</p></div><button class="button button--small" @click="addEntry(group.type)"><Icon name="plus"/>新增</button></div><div class="document-list"><article v-for="item in group.items" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.definition || item.description || item.content }}</small></div><button class="switch" :class="{on:item.enabled}" @click="toggleEntry(item)"><i></i></button></article><EmptyState v-if="!group.items.length" icon="book" title="暂无条目" text="手动新增，或从知识文件解析导入。"/></div></section></div>
    <div v-else-if="tab==='import'" class="content-card import-preview"><div class="card-heading"><div><h2>导入预览</h2><p>{{ importFormat }} · {{ importFilename }} · 确认前可编辑</p></div><button class="button button--primary" @click="confirmImport"><Icon name="check"/>确认 {{ preview.length }} 条并建立索引</button></div><div class="preview-records"><article v-for="(item,index) in preview" :key="index" class="settings-card"><header><b>{{ item.table }}</b><button class="icon-button danger" @click="removePreview(index)"><Icon name="close"/></button></header><div class="form-grid"><label v-for="field in fields(item)" :key="field" :class="{ 'span-2':['definition','sql_template','notes','description','condition','content'].includes(field) }"><span>{{ field }}</span><textarea v-if="['definition','sql_template','notes','description','condition','content'].includes(field)" v-model="item[field]"></textarea><input v-else v-model="item[field]"></label></div></article></div></div>
    <div v-else-if="tab==='prompt'" class="content-card prompt-settings"><div class="card-heading"><div><h2>本会话临时指令</h2><p>仅对当前会话每一轮生效，最多 {{ promptState.max_chars }} 字</p></div><StatusPill :status="promptState.enabled?'active':'disabled'"/></div><textarea class="prompt-textarea" v-model="promptState.temp_prompt" :maxlength="promptState.max_chars" placeholder="例如：所有金额换算为万元，结论先行，并单独列出假设。"></textarea><div class="prompt-footer"><small>{{ promptState.temp_prompt.length }} / {{ promptState.max_chars }}</small><div class="row-actions"><button class="button" :disabled="!promptState.temp_prompt" @click="togglePrompt">{{ promptState.enabled?'停用':'启用' }}</button><button class="button" @click="savePrompt(true)">按原文保存</button><button class="button button--primary" @click="savePrompt(false)">用模型整理并保存</button></div></div></div>
    <div v-else class="knowledge-grid"><section class="content-card"><div class="card-heading"><div><h2>检索验收</h2><p>检查 Agent 能否召回正确口径</p></div></div><div class="search-box"><Icon name="search"/><input v-model="query" @keyup.enter="search" placeholder="例如：GMV 的计算口径是什么？"><button @click="search">检索</button></div><div class="search-results"><article v-for="item in results" :key="item.document_id+'-'+item.chunk"><div><b>{{ item.document_name }}</b><span>相关度 {{ Math.round(item.score*100) }}%</span></div><p>{{ item.text }}</p></article><EmptyState v-if="!results.length" icon="search" title="输入问题测试召回" text="结果会显示来源、相关度与原始片段。"/></div></section></div></section>`,
};

export const AutomationPanel = {
  components: { DataTable, EmptyState, Icon, Modal, StatusPill }, props: { ctx: Object },
  data: () => ({ tab:localStorage.getItem('meridian-automation-tab')||'workflows', workflows:[], runs:[], teams:[], teamRuns:[], hooks:[], schedules:[], jobs:[], open:false, editorMode:'workflow', form:{name:'月度经营复盘',description:'查询核心数据，经审批后交付结果'}, teamForm:{name:'经营诊断小组',objective:'从数据、统计、业务与证据四个角度完成诊断'}, hookForm:{name:'分析完成通知',event:'analysis.completed',actionType:'webhook',url:''}, scheduleForm:{name:'每日分析',workflow_id:'',cron:'0 9 * * *',timezone:'Asia/Shanghai'}, definitionText:'' }),
  mounted() { this.load(); },
  methods: {
    async load() {
      const wid=this.ctx.state.workspaceId;
      const [flows,runs,teams,teamRuns,hooks,schedules,jobs]=await Promise.all([
        api(withWorkspace('/api/workflows',wid)),api(withWorkspace('/api/workflow-runs',wid)),api(withWorkspace('/api/teams',wid)),api(withWorkspace('/api/team-runs',wid)),api(withWorkspace('/api/hooks',wid)),api(withWorkspace('/api/schedules',wid)),api(withWorkspace('/api/jobs',wid))]);
      this.workflows=flows.items;this.runs=runs.items;this.teams=teams.items;this.teamRuns=teamRuns.items;this.hooks=hooks.items;this.schedules=schedules.items;this.jobs=jobs.items;
    },
    newWorkflow(){const source=this.ctx.selectedSources()[0];const table=source?.tables?.[0]?.name||'data';this.definitionText=JSON.stringify({steps:[{id:'query',name:'获取分析数据',type:'query',depends_on:[],config:{source_ids:source?[source.id]:[],sql:source?`SELECT * FROM "${table}" LIMIT 500`:'SELECT 1 AS value'}},{id:'review',name:'人工复核',type:'approval',depends_on:['query'],config:{}},{id:'deliver',name:'交付数据',type:'export_data',depends_on:['review'],config:{format:'xlsx'}}]},null,2);this.editorMode='workflow';this.open=true;},
    async saveWorkflow(){let definition;try{definition=JSON.parse(this.definitionText)}catch{return this.ctx.fail(new Error('工作流定义必须是 JSON'));}await this.ctx.run('正在保存并校验工作流',async()=>{const created=await api('/api/workflows',{method:'POST',body:{...this.form,definition,workspace_id:this.ctx.state.workspaceId}});const validation=await api(`/api/workflows/${created.item.id}/validate`,{method:'POST'});if(!validation.validation.valid)throw new Error(validation.validation.errors.join('；'));await api(`/api/workflows/${created.item.id}/publish`,{method:'POST'});this.open=false;await this.load();});},
    async runFlow(flow){await api(`/api/workflows/${flow.id}/runs`,{method:'POST',body:{inputs:{session_id:this.ctx.activeSession()?.id},workspace_id:this.ctx.state.workspaceId}});this.ctx.toast('工作流已进入后台任务队列','已启动');setTimeout(()=>this.load(),500);},
    async approve(run,decision){await api(`/api/workflow-runs/${run.id}/${decision}`,{method:'POST',body:{comment:'在自动化中心完成复核'}});await this.load();},
    async createTeam(){const profiles=this.ctx.state.agentProfiles.slice(0,4);const members=profiles.map(p=>({profile_id:p.id,name:p.name,role:p.role}));await api('/api/teams',{method:'POST',body:{...this.teamForm,members,workspace_id:this.ctx.state.workspaceId}});this.open=false;await this.load();},
    async runTeam(team){const task=prompt('输入本次协作分析任务：',team.objective)||'';if(!task)return;await api(`/api/teams/${team.id}/runs`,{method:'POST',body:{task,session_id:this.ctx.activeSession()?.id}});this.ctx.toast('多顾问协作已在后台开始','已启动');setTimeout(()=>this.load(),500);},
    async createHook(){const action=this.hookForm.actionType==='webhook'?{type:'webhook',url:this.hookForm.url}:{type:'workflow',workflow_id:this.workflows[0]?.id};await api('/api/hooks',{method:'POST',body:{name:this.hookForm.name,event:this.hookForm.event,action,workspace_id:this.ctx.state.workspaceId}});this.open=false;await this.load();},
    async createSchedule(){await api('/api/schedules',{method:'POST',body:{...this.scheduleForm,workspace_id:this.ctx.state.workspaceId}});this.open=false;await this.load();},
    async runSchedule(item){await api(`/api/schedules/${item.id}/run`,{method:'POST'});this.ctx.toast('计划任务已立即启动','已进入队列');setTimeout(()=>this.load(),500);},
    openCreate(mode){this.editorMode=mode;this.open=true;},
    async cancelJob(job){await api(`/api/jobs/${job.id}/cancel`,{method:'POST'});await this.load();},
  },
  template:`<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Orchestration</span><h1>自动化中心</h1><p>把可复用分析固化为流程、协作组、事件触发器与后台任务。</p></div><button v-if="!['runs','jobs'].includes(tab)" class="button button--primary" @click="tab==='workflows'?newWorkflow():openCreate(tab==='teams'?'team':tab==='schedules'?'schedule':'hook')"><Icon name="plus"/>新建{{ tab==='workflows'?'工作流':tab==='teams'?'协作组':tab==='schedules'?'计划':'触发器' }}</button></header><nav class="page-tabs"><button :class="{active:tab==='workflows'}" @click="tab='workflows'">工作流</button><button :class="{active:tab==='runs'}" @click="tab='runs'">运行与审批 <span>{{ runs.filter(r=>r.status==='waiting_approval').length }}</span></button><button :class="{active:tab==='teams'}" @click="tab='teams'">多顾问协作</button><button :class="{active:tab==='schedules'}" @click="tab='schedules'">定时计划</button><button :class="{active:tab==='hooks'}" @click="tab='hooks'">事件 Hook</button><button :class="{active:tab==='jobs'}" @click="tab='jobs';load()">后台任务</button></nav>
    <div v-if="tab==='workflows'" class="card-grid"><article v-for="flow in workflows" :key="flow.id" class="entity-card"><div class="entity-card__top"><span class="record-icon"><Icon name="workflow"/></span><StatusPill :status="flow.status"/></div><h2>{{ flow.name }}</h2><p>{{ flow.description || '无说明' }}</p><div class="mini-metrics"><span><b>{{ flow.definition.steps.length }}</b>步骤</span><span><b>v{{ flow.version }}</b>版本</span></div><footer><button class="button button--small button--primary" @click="runFlow(flow)"><Icon name="play"/>运行</button></footer></article><EmptyState v-if="!workflows.length" icon="workflow" title="还没有工作流" text="把查询、分析、审批与交付组合成可重复执行的流程。"><button class="button button--primary" @click="newWorkflow">创建首个工作流</button></EmptyState></div>
    <div v-if="tab==='runs'" class="list-stack"><article v-for="run in runs" :key="run.id" class="run-row"><span class="record-icon"><Icon name="workflow"/></span><div><b>运行 {{ run.id.slice(-7) }}</b><small>{{ run.workflow_id }} · {{ ctx.time(run.created_at) }}</small><div v-if="run.current_step_id" class="muted-line">当前步骤：{{ run.current_step_id }}</div></div><StatusPill :status="run.status"/><div v-if="run.status==='waiting_approval'" class="row-actions"><button class="button button--small" @click="approve(run,'reject')">拒绝</button><button class="button button--small button--primary" @click="approve(run,'approve')">批准继续</button></div></article><EmptyState v-if="!runs.length" icon="play" title="暂无运行记录" text="从已发布的工作流发起一次运行。"/></div>
    <div v-if="tab==='teams'" class="card-grid"><article v-for="team in teams" :key="team.id" class="entity-card"><div class="entity-card__top"><span class="record-icon"><Icon name="users"/></span><StatusPill :status="team.status"/></div><h2>{{ team.name }}</h2><p>{{ team.objective }}</p><div class="avatar-stack"><span v-for="member in team.members" :key="member.name" :title="member.role">{{ member.name.slice(0,1) }}</span></div><footer><button class="button button--small button--primary" @click="runTeam(team)"><Icon name="play"/>发起协作</button></footer></article><EmptyState v-if="!teams.length" icon="users" title="尚未配置协作组" text="为复杂问题组织数据、量化、业务和复核顾问。"/></div>
    <div v-if="tab==='hooks'" class="list-stack"><article v-for="hook in hooks" :key="hook.id" class="run-row"><span class="record-icon"><Icon name="bolt"/></span><div><b>{{ hook.name }}</b><small>{{ hook.event }} → {{ hook.action.type }}</small></div><StatusPill :status="hook.enabled?'ready':'disabled'" :label="hook.enabled?'启用':'停用'"/><span class="count-badge">触发 {{ hook.run_count || 0 }} 次</span></article><EmptyState v-if="!hooks.length" icon="bolt" title="暂无事件触发器" text="在分析完成、数据刷新或任务失败时自动启动流程或调用 Webhook。"/></div>
    <div v-if="tab==='schedules'" class="list-stack"><article v-for="item in schedules" :key="item.id" class="run-row"><span class="record-icon"><Icon name="workflow"/></span><div><b>{{ item.name }}</b><small>{{ item.cron }} · {{ item.timezone }} · {{ item.workflow_id }}</small></div><StatusPill :status="item.enabled?'ready':'disabled'" :label="item.enabled?'启用':'停用'"/><button class="button button--small" @click="runSchedule(item)"><Icon name="play"/>立即运行</button></article><EmptyState v-if="!schedules.length" icon="workflow" title="暂无定时计划" text="使用标准五段 Cron 表达式周期运行已发布工作流。"/></div>
    <div v-if="tab==='jobs'" class="list-stack"><article v-for="job in jobs" :key="job.id" class="job-row"><div class="job-progress"><i :style="{width:job.progress+'%'}"></i></div><span class="record-icon"><Icon name="play"/></span><div><b>{{ job.title }}</b><small>{{ job.message }} · {{ ctx.time(job.updated_at) }}</small></div><StatusPill :status="job.status"/><b>{{ Math.round(job.progress || 0) }}%</b><button v-if="['queued','running'].includes(job.status)" class="button button--small" @click="cancelJob(job)">取消</button></article><EmptyState v-if="!jobs.length" icon="workflow" title="后台队列为空" text="工作流、协作分析和大任务会在这里持续执行并保留事件记录。"/></div>
    <Modal :open="open" :title="editorMode==='workflow'?'新建工作流':editorMode==='team'?'新建协作组':editorMode==='schedule'?'新建定时计划':'新建事件 Hook'" wide @close="open=false"><div v-if="editorMode==='workflow'" class="form-grid"><label><span>名称</span><input v-model="form.name"></label><label class="span-2"><span>说明</span><input v-model="form.description"></label><label class="span-2"><span>流程定义（DAG JSON）</span><textarea class="code-input" v-model="definitionText"></textarea></label></div><div v-if="editorMode==='team'" class="form-grid"><label><span>协作组名称</span><input v-model="teamForm.name"></label><label class="span-2"><span>长期目标</span><textarea v-model="teamForm.objective"></textarea></label><p class="span-2 form-note">默认启用数据工程、量化分析、经营策略和证据复核四种专业角色。</p></div><div v-if="editorMode==='hook'" class="form-grid"><label><span>名称</span><input v-model="hookForm.name"></label><label><span>事件</span><input v-model="hookForm.event"></label><label><span>动作</span><select v-model="hookForm.actionType"><option value="webhook">调用 Webhook</option><option value="workflow">启动首个工作流</option></select></label><label v-if="hookForm.actionType==='webhook'" class="span-2"><span>Webhook URL</span><input v-model="hookForm.url"></label></div><div v-if="editorMode==='schedule'" class="form-grid"><label><span>名称</span><input v-model="scheduleForm.name"></label><label><span>工作流</span><select v-model="scheduleForm.workflow_id"><option value="">选择已发布工作流</option><option v-for="flow in workflows.filter(f=>f.status==='published')" :key="flow.id" :value="flow.id">{{ flow.name }}</option></select></label><label><span>Cron</span><input v-model="scheduleForm.cron" placeholder="0 9 * * *"></label><label><span>时区</span><input v-model="scheduleForm.timezone"></label></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" @click="editorMode==='workflow'?saveWorkflow():editorMode==='team'?createTeam():editorMode==='schedule'?createSchedule():createHook()">保存</button></template></Modal></section>`,
};

export const DashboardsPanel = {
  components:{ChartView,EmptyState,Icon,Modal},props:{ctx:Object},data:()=>({items:[],open:false,form:{name:'经营监测看板',description:'核心指标与趋势'},active:null}),mounted(){this.load()},methods:{async load(){this.items=(await api(withWorkspace('/api/dashboards',this.ctx.state.workspaceId))).items},async create(){const charts=(await api(withWorkspace('/api/charts',this.ctx.state.workspaceId))).items;const widgets=charts.slice(0,6).map((item,index)=>({id:`widget-${index}`,title:item.name,chart:item.spec,result_id:item.result_id,source_id:item.source_id}));const result=await api('/api/dashboards',{method:'POST',body:{...this.form,widgets,workspace_id:this.ctx.state.workspaceId}});this.open=false;this.items.unshift(result.item);this.active=result.item},async refresh(item){const result=await api(withWorkspace(`/api/dashboards/${item.id}/refresh`,this.ctx.state.workspaceId),{method:'POST'});this.active=result.item;this.ctx.toast('图表数据已重新计算','看板已刷新')},async exportHtml(item){const result=await api(withWorkspace(`/api/dashboards/${item.id}/export`,this.ctx.state.workspaceId),{method:'POST'});location.href=result.artifact.download_url},async remove(item){if(!confirm(`归档看板“${item.name}”？`))return;await api(withWorkspace(`/api/dashboards/${item.id}`,this.ctx.state.workspaceId),{method:'DELETE'});await this.load()}},template:`<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Delivery surface</span><h1>分析看板</h1><p>把已保存图表组织为可刷新、可导出的决策视图。</p></div><button class="button button--primary" @click="open=true"><Icon name="plus"/>创建看板</button></header><div v-if="!active" class="card-grid"><article v-for="item in items" :key="item.id" class="entity-card dashboard-card" @click="active=item"><div class="dashboard-thumb"><div v-for="n in Math.min(item.widgets.length,4)" :key="n"></div></div><h2>{{ item.name }}</h2><p>{{ item.description }}</p><footer><span>{{ item.widgets.length }} 个组件</span><div><button class="icon-button" @click.stop="exportHtml(item)"><Icon name="download"/></button><button class="icon-button danger" @click.stop="remove(item)"><Icon name="close"/></button></div></footer></article><EmptyState v-if="!items.length" icon="dashboard" title="还没有分析看板" text="先在数据目录运行查询并保存图表，再创建看板。"/></div><div v-else class="dashboard-detail"><div class="detail-title"><div><button class="text-button" @click="active=null">← 返回看板列表</button><h2>{{ active.name }}</h2><p>{{ active.description }}</p></div><div class="header-cluster"><button class="button" @click="refresh(active)"><Icon name="refresh"/>刷新数据</button><button class="button" @click="exportHtml(active)"><Icon name="download"/>导出 HTML</button></div></div><div class="widget-grid"><article v-for="widget in active.widgets" :key="widget.id"><h3>{{ widget.title }}</h3><ChartView :spec="widget.chart"/></article><EmptyState v-if="!active.widgets.length" icon="chart" title="看板中没有图表" text="从数据目录生成图表后重新创建看板。"/></div></div><Modal :open="open" title="创建分析看板" @close="open=false"><div class="form-grid"><label><span>名称</span><input v-model="form.name"></label><label class="span-2"><span>说明</span><textarea v-model="form.description"></textarea></label><p class="form-note span-2">将自动加入当前空间最近保存的 6 个图表。</p></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" @click="create">创建</button></template></Modal></section>`};

export const MapsPanel = {
  components: { EmptyState, Icon, Modal }, props: { ctx: Object },
  data: () => ({
    items: [], templates: [], active: null, original: null, revisions: [], open: false,
    form: { title: '新建商业画布', template_id: 'business_model_canvas' },
    drawioReady: false, autosaveTimer: null, pendingExport: '',
  }),
  mounted() { window.addEventListener('message', this.onDrawioMessage); this.load(); },
  beforeUnmount() { window.removeEventListener('message', this.onDrawioMessage); clearTimeout(this.autosaveTimer); },
  methods: {
    base() { return `/api/session/${encodeURIComponent(this.ctx.activeSession()?.id || '')}/business-canvas`; },
    async load() {
      if (!this.ctx.activeSession()) return;
      const [projects, templates] = await Promise.all([
        api(withWorkspace(`${this.base()}/projects`, this.ctx.state.workspaceId)),
        api(withWorkspace(`${this.base()}/templates`, this.ctx.state.workspaceId)),
      ]);
      this.items = projects.projects; this.templates = templates.templates;
    },
    async create() {
      const result = await api(this.base() + '/projects', { method: 'POST', body: this.form });
      this.open = false; await this.load(); await this.select(result.project.id);
    },
    async select(id) {
      const [detail, history] = await Promise.all([
        api(withWorkspace(`${this.base()}/projects/${id}`, this.ctx.state.workspaceId)),
        api(withWorkspace(`${this.base()}/projects/${id}/revisions`, this.ctx.state.workspaceId)),
      ]);
      this.active = detail.project; this.original = JSON.parse(JSON.stringify(detail.project));
      this.revisions = history.revisions; this.drawioReady = false;
      await nextTick();
    },
    close() { this.active = null; this.original = null; this.revisions = []; this.drawioReady = false; },
    listText(block, key) { return (block.content?.[key] || []).join('\n'); },
    setList(block, key, value) { block.content[key] = value.split('\n').map(v => v.trim()).filter(Boolean); },
    async save() {
      const id = this.active.id;
      if (this.active.title !== this.original.title) {
        const result = await api(`${this.base()}/projects/${id}`, { method: 'PATCH', body: { title: this.active.title } });
        this.active = result.project;
      }
      for (const block of this.active.blocks || []) {
        const before = this.original.blocks.find(item => item.key === block.key)?.content;
        if (JSON.stringify(before) !== JSON.stringify(block.content)) {
          const result = await api(`${this.base()}/projects/${id}/blocks/${block.key}`, {
            method: 'PATCH', body: { content: block.content, reason: '用户在商业画布编辑器中保存' },
          });
          this.active = result.project;
        }
      }
      await this.select(id); await this.load(); this.ctx.toast('模块内容与修订历史已持久化', '画布已保存');
    },
    async changeMode() {
      const result = await api(`${this.base()}/projects/${this.active.id}/rendering-mode`, {
        method: 'PATCH', body: { rendering_mode: this.active.rendering_mode },
      });
      this.active = result.project; this.original = JSON.parse(JSON.stringify(result.project)); await nextTick();
    },
    async remove(item) {
      if (!confirm(`归档画布“${item.title}”？`)) return;
      await api(`${this.base()}/projects/${item.id}`, { method: 'DELETE' });
      if (this.active?.id === item.id) this.close(); await this.load();
    },
    onDrawioMessage(event) {
      const frame = this.$refs.drawioFrame;
      if (!frame || event.source !== frame.contentWindow) return;
      let message = event.data;
      if (typeof message === 'string') { try { message = JSON.parse(message); } catch { return; } }
      if (!message || typeof message !== 'object') return;
      if (message.event === 'init') {
        this.drawioReady = true;
        frame.contentWindow.postMessage(JSON.stringify({ action: 'load', autosave: 1, xml: this.active.diagram_xml }), '*');
      } else if (message.event === 'autosave' && message.xml) {
        clearTimeout(this.autosaveTimer);
        this.autosaveTimer = setTimeout(() => this.saveDiagram(message.xml), 650);
      } else if (message.action === 'export' && message.data) {
        const link = document.createElement('a'); link.href = message.data;
        link.download = `${this.active.title || '商业画布'}.${message.format || this.pendingExport || 'png'}`;
        document.body.appendChild(link); link.click(); link.remove(); this.pendingExport = '';
      }
    },
    async saveDiagram(xml) {
      if (!this.active || !xml) return;
      try {
        const result = await api(`${this.base()}/projects/${this.active.id}/diagram`, {
          method: 'PATCH', body: { diagram_xml: xml, actor_type: 'user', reason: 'draw.io 自动保存' },
        });
        this.active.diagram_xml = result.project.diagram_xml;
      } catch (error) { this.ctx.fail(error); }
    },
    exportDiagram(format) {
      if (format === 'drawio') {
        const blob = new Blob([this.active.diagram_xml], { type: 'application/xml' });
        const url = URL.createObjectURL(blob); const link = document.createElement('a');
        link.href = url; link.download = `${this.active.title || '商业画布'}.drawio`;
        document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 500); return;
      }
      if (!this.drawioReady) return this.ctx.fail(new Error('图表编辑器尚未就绪'));
      this.pendingExport = format;
      this.$refs.drawioFrame.contentWindow.postMessage(JSON.stringify({ action: 'export', format, spin: '正在导出…' }), '*');
    },
  },
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Business canvas</span><h1>商业画布</h1><p>用可编辑 draw.io 图表、结构化方法块和不可变修订链沉淀决策模型。</p></div><button class="button button--primary" @click="open=true"><Icon name="plus"/>新建画布</button></header>
    <div v-if="!active" class="card-grid"><article v-for="item in items" :key="item.id" class="entity-card" @click="select(item.id)"><div class="entity-card__top"><span class="record-icon"><Icon name="map"/></span><span class="count-badge">修订 {{ item.revision }}</span></div><h2>{{ item.title }}</h2><p>{{ templates.find(t=>t.id===item.template_id)?.description || item.template_id }}</p><div class="mini-metrics"><span><b>{{ item.blocks?.length||0 }}</b>模块</span><span><b>{{ item.rendering_mode }}</b>视图</span></div><footer><span>{{ ctx.time(item.updated_at) }}</span><button class="icon-button danger" @click.stop="remove(item)"><Icon name="close"/></button></footer></article><EmptyState v-if="!items.length" icon="map" title="还没有商业画布" text="可从商业模式、BCG、SWOT、价值主张或空白画布开始。"/></div>
    <div v-else class="canvas-editor"><div class="detail-title"><div><button class="text-button" @click="close">← 返回画布列表</button><input class="title-input" v-model="active.title"><p>{{ active.template?.name }} · 修订 {{ active.revision }}</p></div><div class="header-cluster"><select v-model="active.rendering_mode" @change="changeMode"><option value="card">模块</option><option value="diagram">图表</option><option value="both">并排</option></select><button class="button" @click="exportDiagram('drawio')">导出 .drawio</button><button v-if="active.rendering_mode!=='card'" class="button" @click="exportDiagram('png')">导出 PNG</button><button class="button button--primary" @click="save">保存修订</button></div></div>
      <div class="canvas-workbench" :class="'canvas-workbench--'+active.rendering_mode"><section v-if="active.rendering_mode!=='diagram'" class="canvas-blocks"><article v-for="block in active.blocks" :key="block.key"><h3>{{ block.title }}</h3><label><span>摘要</span><textarea v-model="block.content.summary" placeholder="记录核心判断…"></textarea></label><label v-for="field in ['assumptions','evidence_refs','risks','next_actions']" :key="field"><span>{{ {assumptions:'假设',evidence_refs:'证据引用',risks:'风险',next_actions:'后续行动'}[field] }}</span><textarea :value="listText(block,field)" @input="setList(block,field,$event.target.value)" placeholder="每行一项"></textarea></label></article></section><section v-if="active.rendering_mode!=='card'" class="drawio-shell"><div v-if="!drawioReady" class="drawio-loading">正在加载本地 draw.io 编辑器…</div><iframe ref="drawioFrame" title="draw.io 商业画布" src="/static/drawio/index.html?embed=1&proto=json&spin=1&libraries=1&pwa=0&offline=0&lang=zh" allow="clipboard-read; clipboard-write"></iframe></section></div>
      <details class="canvas-history"><summary>修订历史（{{ revisions.length }}）</summary><article v-for="item in revisions" :key="item.id"><b>{{ item.kind }} {{ item.block_key }}</b><span>{{ item.actor_type }} · {{ ctx.time(item.created_at) }}</span><small>{{ item.reason }}</small></article></details>
    </div>
    <Modal :open="open" title="新建商业画布" @close="open=false"><div class="form-grid"><label><span>名称</span><input v-model="form.title"></label><label><span>模板</span><select v-model="form.template_id"><option v-for="item in templates" :value="item.id" :key="item.id">{{ item.name }}</option></select></label><p class="form-note span-2">{{ templates.find(t=>t.id===form.template_id)?.description }}</p></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" @click="create">创建</button></template></Modal></section>`,
};

export const GpuPanel = {
  components: { Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    status: null, connections: [], hostKeys: {}, remoteModels: {}, selectedModels: {},
    form: { name: '', connection_type: 'ssh', host: '', port: 22, username: '', target_host: '127.0.0.1', target_port: 8000, auth_method: 'agent', password: '', key_file: '', base_url: '' },
  }),
  mounted() { this.load(); },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const [status, connections] = await Promise.all([
        api(withWorkspace('/api/gpu/status', wid)), api(withWorkspace('/api/gpu/connections', wid)),
      ]);
      this.status = status; this.connections = connections.connections || [];
    },
    async toggleEnabled() {
      await api(withWorkspace('/api/gpu/enabled', this.ctx.state.workspaceId), { method: 'POST', body: { enabled: !this.status.enabled } });
      await this.load();
    },
    async create() {
      await this.ctx.run('正在保存远程算力连接', async () => {
        await api(withWorkspace('/api/gpu/connections', this.ctx.state.workspaceId), { method: 'POST', body: this.form });
        this.form.password = ''; await this.load();
      });
    },
    async inspect(item) {
      const result = await api(withWorkspace(`/api/gpu/connections/${item.id}/host-key`, this.ctx.state.workspaceId), { method: 'POST' });
      this.hostKeys[item.id] = result.host_key;
    },
    async trust(item) {
      const key = this.hostKeys[item.id]; if (!key) return;
      await api(withWorkspace(`/api/gpu/connections/${item.id}/trust-host-key`, this.ctx.state.workspaceId), { method: 'POST', body: { key_type: key.type, key_base64: key.base64 } });
      this.ctx.toast(key.fingerprint, '主机指纹已信任');
    },
    async connect(item) {
      await this.ctx.run('正在建立安全连接', async () => {
        await api(withWorkspace(`/api/gpu/connections/${item.id}/connect`, this.ctx.state.workspaceId), { method: 'POST' }); await this.load();
      });
    },
    async disconnect(item) { await api(withWorkspace(`/api/gpu/connections/${item.id}/disconnect`, this.ctx.state.workspaceId), { method: 'POST' }); await this.load(); },
    async discover(item) {
      const result = await api(withWorkspace(`/api/gpu/connections/${item.id}/models`, this.ctx.state.workspaceId));
      this.remoteModels[item.id] = result.models || []; this.selectedModels[item.id] ||= result.models?.[0] || '';
    },
    async register(item) {
      const result = await api(withWorkspace(`/api/gpu/connections/${item.id}/models/register`, this.ctx.state.workspaceId), { method: 'POST', body: { model: this.selectedModels[item.id] } });
      this.ctx.toast(result.provider?.name || '', '模型已注册');
    },
    async testModel(item) {
      const result = await api(withWorkspace(`/api/gpu/connections/${item.id}/models/test`, this.ctx.state.workspaceId), { method: 'POST', body: { model: this.selectedModels[item.id] } });
      this.ctx.toast(result.reply || '', '真实推理测试通过');
    },
    async preflight(item) {
      const result = await api(withWorkspace(`/api/gpu/connections/${item.id}/training/preflight`, this.ctx.state.workspaceId), { method: 'POST' });
      this.ctx.toast(`${result.training_runner.gpu_name} · Python ${result.training_runner.python}`, '远程训练器就绪'); await this.load();
    },
    async remove(item) { if (!confirm(`删除算力连接“${item.name}”？`)) return; await api(withWorkspace(`/api/gpu/connections/${item.id}`, this.ctx.state.workspaceId), { method: 'DELETE' }); await this.load(); },
  },
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Compute Fabric</span><h1>GPU 与远程算力</h1><p>检测本机 CUDA/Ollama，或通过指纹信任的 SSH 隧道接入 OpenAI 兼容推理服务。</p></div><button v-if="status" class="button" @click="toggleEnabled"><span class="switch" :class="{on:status.enabled}"><i></i></span>{{ status.enabled?'算力已启用':'算力已关闭' }}</button></header>
    <div v-if="status" class="metric-strip compute-strip"><article><small>显卡类型</small><b>{{ status.gpu.kind }}</b><span>{{ status.gpu.gpus?.length || 0 }} 个设备</span></article><article><small>CUDA</small><b>{{ status.cuda.available?'可用':'不可用' }}</b><span>{{ status.cuda.message }}</span></article><article><small>Ollama</small><b>{{ status.ollama.online?'在线':'离线' }}</b><span>{{ status.ollama.models?.length || 0 }} 个模型</span></article></div>
    <div class="compute-grid"><section class="settings-content"><div class="section-heading"><h2>新建远程连接</h2><p>SSH 凭据加密保存；首次连接必须手动核对主机指纹。</p></div><div class="form-grid"><label><span>名称</span><input v-model.trim="form.name" placeholder="A100 推理节点"></label><label><span>连接方式</span><select v-model="form.connection_type"><option value="ssh">SSH 隧道</option><option value="direct">HTTPS 直连</option></select></label><template v-if="form.connection_type==='direct'"><label class="span-2"><span>服务根地址</span><input v-model.trim="form.base_url" placeholder="https://gpu.example.com:8000"></label></template><template v-else><label><span>SSH 主机</span><input v-model.trim="form.host" placeholder="gpu.example.com"></label><label><span>SSH 端口</span><input type="number" v-model.number="form.port"></label><label><span>用户名</span><input v-model.trim="form.username"></label><label><span>认证方式</span><select v-model="form.auth_method"><option value="agent">SSH Agent</option><option value="password">密码</option><option value="key_file">私钥文件</option></select></label><label><span>推理服务主机</span><input v-model.trim="form.target_host"></label><label><span>推理服务端口</span><input type="number" v-model.number="form.target_port"></label><label v-if="form.auth_method==='password'" class="span-2"><span>SSH 密码</span><input type="password" v-model="form.password"></label><label v-if="form.auth_method==='key_file'" class="span-2"><span>私钥路径</span><input v-model.trim="form.key_file" placeholder="/path/to/id_ed25519"></label></template></div><button class="button button--primary" @click="create"><Icon name="plus"/>保存连接</button></section>
    <section class="settings-content"><div class="section-heading"><h2>已保存连接</h2><p>连接后可发现、验收并注册远端模型。</p></div><div class="compute-list"><article v-for="item in connections" :key="item.id" class="settings-card"><header><div><h3>{{ item.name }}</h3><small>{{ item.connection_type==='ssh' ? item.username+'@'+item.host+':'+item.port : item.base_url }}</small></div><StatusPill :status="item.connected?'online':'configured'"/></header><div class="row-actions"><template v-if="item.connection_type==='ssh' && !item.connected"><button class="button button--small" @click="inspect(item)">读取指纹</button><button v-if="hostKeys[item.id]" class="button button--small" @click="trust(item)">确认信任</button></template><button v-if="!item.connected" class="button button--small button--primary" @click="connect(item)">连接</button><button v-else class="button button--small" @click="disconnect(item)">断开</button><button class="button button--small" @click="remove(item)">删除</button></div><p v-if="hostKeys[item.id]" class="fingerprint"><b>{{ hostKeys[item.id].type }}</b><code>{{ hostKeys[item.id].fingerprint }}</code></p><div v-if="item.connected || item.connection_type==='direct'" class="model-actions"><button class="button button--small" @click="discover(item)">发现模型</button><select v-if="remoteModels[item.id]?.length" v-model="selectedModels[item.id]"><option v-for="model in remoteModels[item.id]" :key="model">{{ model }}</option></select><button v-if="selectedModels[item.id]" class="button button--small" @click="testModel(item)">真实测试</button><button v-if="selectedModels[item.id]" class="button button--small" @click="register(item)">注册到模型服务</button><button v-if="item.connection_type==='ssh'" class="button button--small" @click="preflight(item)">训练器预检</button></div><p v-if="item.training_runner?.runner_ready" class="form-note">训练器已就绪：{{ item.training_runner.gpu_name }} · {{ item.training_runner.python }}</p></article><p v-if="!connections.length" class="form-note">还没有远程算力连接。</p></div></section></div></section>`,
};

export const FeishuBotPanel = {
  components: { Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    status: null, sessionLink: null, chats: [], loadingChats: false,
    form: { enabled: false, app_id: '', app_secret: '', event_verification_token: '', inbound_transport: 'long_connection', receive_id_type: 'chat_id', receive_id: '' },
  }),
  computed: {
    session() { return this.ctx.activeSession(); },
    connectionState() {
      if (!this.status?.configured) return 'idle';
      if (!this.status.enabled) return 'disabled';
      if (this.status.inbound_transport === 'long_connection') return this.status.long_connection_status || 'starting';
      return 'connected';
    },
  },
  mounted() { this.load(); },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const result = await api(withWorkspace('/api/feishu-bot', wid));
      this.status = result.connection;
      this.form = { ...this.form, ...result.connection, app_secret: '', event_verification_token: '' };
      if (this.session) this.sessionLink = await api(withWorkspace(`/api/session/${this.session.id}/feishu-bot`, wid));
    },
    async save() {
      await this.ctx.run('正在保存飞书机器人', async () => {
        const payload = { ...this.form };
        if (!payload.app_secret) delete payload.app_secret;
        if (!payload.event_verification_token) delete payload.event_verification_token;
        await api(withWorkspace('/api/feishu-bot', this.ctx.state.workspaceId), { method: 'PUT', body: payload });
        await this.load();
      });
    },
    async loadChats() {
      this.loadingChats = true;
      try {
        const result = await api(withWorkspace('/api/feishu-bot/chats', this.ctx.state.workspaceId));
        this.chats = result.chats || [];
        if (!this.form.receive_id && this.chats.length) this.form.receive_id = this.chats[0].chat_id;
      } finally { this.loadingChats = false; }
    },
    async link(enabled) {
      if (!this.session) return this.ctx.fail(new Error('请先创建或选择会话'));
      this.sessionLink = await api(withWorkspace(`/api/session/${this.session.id}/feishu-bot`, this.ctx.state.workspaceId), {
        method: 'PUT', body: { enabled, chat_id: this.form.receive_id },
      });
      this.ctx.toast('', enabled ? '当前会话已连接飞书群' : '当前会话已断开飞书群');
    },
    async test() {
      await this.ctx.run('正在发送飞书测试消息', () => api(withWorkspace('/api/feishu-bot/test', this.ctx.state.workspaceId), { method: 'POST' }));
    },
  },
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Feishu Agent</span><h1>飞书对话机器人</h1><p>将当前分析会话绑定到飞书群，群内 @ 机器人可直接运行同一个 Agent。</p></div><StatusPill v-if="status" :status="connectionState"/></header>
    <div class="bot-grid"><section class="settings-content"><div class="section-heading"><h2>应用连接</h2><p>App Secret 与事件 Token 只会加密保存，不会回显。</p></div><div class="form-grid"><label><span>App ID</span><input v-model.trim="form.app_id" placeholder="cli_xxx"></label><label><span>App Secret</span><input v-model="form.app_secret" type="password" :placeholder="status?.app_secret_configured?'已配置，留空保持不变':'必填'"></label><label><span>入站方式</span><select v-model="form.inbound_transport"><option value="long_connection">长连接（推荐）</option><option value="webhook">Webhook</option></select></label><label><span>事件验证 Token</span><input v-model="form.event_verification_token" type="password" :placeholder="status?.event_verification_token_configured?'已配置，留空保持不变':'Webhook 模式必填'"></label><label><span>默认接收群 ID</span><input v-model.trim="form.receive_id" placeholder="oc_xxx"></label><label class="check-control align-end"><input type="checkbox" v-model="form.enabled">启用机器人</label></div><div class="row-actions bot-actions"><button class="button button--primary" @click="save"><Icon name="check"/>保存配置</button><button class="button" :disabled="!status?.enabled" @click="loadChats"><Icon name="refresh"/>{{ loadingChats?'正在读取':'读取可见群' }}</button><button class="button" :disabled="!status?.enabled" @click="test">发送测试</button></div><p v-if="status?.long_connection_error" class="form-note">长连接状态：{{ status.long_connection_status }}（{{ status.long_connection_error }}）</p></section>
    <aside class="settings-content"><div class="section-heading"><h2>会话绑定</h2><p>{{ session ? session.name : '当前没有可用会话' }}</p></div><div v-if="session" class="form-grid bot-link-form"><label class="span-2"><span>目标飞书群</span><select v-model="form.receive_id"><option value="">请选择</option><option v-for="chat in chats" :key="chat.chat_id" :value="chat.chat_id">{{ chat.name }} · {{ chat.chat_id }}</option></select></label></div><div v-if="sessionLink" class="settings-card"><h3>{{ sessionLink.connected?'已连接':'未连接' }}</h3><p>{{ sessionLink.connected ? '已绑定 '+(sessionLink.chat_name || sessionLink.chat_id) : '绑定后，Web 与飞书的消息会同步到同一会话。' }}</p><div class="row-actions"><button v-if="!sessionLink.connected" class="button button--primary" :disabled="!form.receive_id" @click="link(true)">连接当前会话</button><button v-else class="button" @click="link(false)">断开连接</button></div></div><p v-if="!chats.length" class="form-note">保存并启用配置后，点击“读取可见群”。</p></aside></div></section>`,
};

export const SettingsPanel={components:{DataTable,EmptyState,Icon,StatusPill},props:{ctx:Object},data:()=>({tab:localStorage.getItem('meridian-settings-tab')||'models',providers:[],mcp:[],connectors:[],memories:[],audit:[],compute:null,skills:[],feishuBot:null,feishuChats:[],sessionBot:null,providerForm:{name:'OpenAI Compatible',base_url:'https://api.openai.com/v1',model:'gpt-4.1-mini',api_key:'',temperature:0.2},mcpForm:{name:'工具服务',transport:'streamable-http',url:'',command:'',argsText:'[]',headersText:'{}',envText:'{}'},connectorForm:{name:'团队通知',type:'webhook',url:'',host:'',port:587,username:'',password:'',sender:'',recipient:'',use_tls:true,app_id:'',app_secret:'',receive_id:'',receive_id_type:'chat_id',verification_token:''},botForm:{enabled:false,app_id:'',app_secret:'',event_verification_token:'',inbound_transport:'long_connection',receive_id_type:'chat_id',receive_id:''},memoryForm:{title:'',content:'',scope:'workspace'},skillForm:{name:'',description:'',instruction:''}}),mounted(){this.load()},methods:{async load(){const wid=this.ctx.state.workspaceId;const [p,m,c,mem,a,comp,s]=await Promise.all([api('/api/providers'),api(withWorkspace('/api/mcp/servers',wid)),api(withWorkspace('/api/connectors',wid)),api(withWorkspace('/api/memories',wid)),api(withWorkspace('/api/audit?limit=100',wid)),api(withWorkspace('/api/compute/status',wid)),api(withWorkspace('/api/skills',wid))]);this.providers=p.items;this.mcp=m.items;this.connectors=c.items;this.memories=mem.items;this.audit=a.items;this.compute=comp;this.skills=s.items;try{const bot=await api(withWorkspace('/api/feishu-bot',wid));this.feishuBot=bot.connection;this.botForm={...this.botForm,...bot.connection,app_secret:'',event_verification_token:''};const sid=this.ctx.activeSession()?.id;if(sid)this.sessionBot=await api(withWorkspace(`/api/session/${sid}/feishu-bot`,wid));}catch{}},async saveProvider(){await this.ctx.run('正在保存模型配置',async()=>{await api('/api/providers',{method:'POST',body:this.providerForm});this.providerForm.api_key='';await this.load()})},async testProvider(item){await this.ctx.run('正在进行真实模型请求',async()=>{const result=await api(`/api/providers/${item.id}/test`,{method:'POST'});this.ctx.toast(`${result.result.model} · ${result.result.latency_ms} ms`,'模型连接正常')})},async saveMcp(){let headers={},env={},args=[];try{headers=JSON.parse(this.mcpForm.headersText||'{}');env=JSON.parse(this.mcpForm.envText||'{}');args=JSON.parse(this.mcpForm.argsText||'[]')}catch{return this.ctx.fail(new Error('请求头、环境变量和参数必须是 JSON'));}await api('/api/mcp/servers',{method:'POST',body:{...this.mcpForm,headers,env,args,workspace_id:this.ctx.state.workspaceId}});await this.load()},async testMcp(item){await this.ctx.run('正在握手并发现工具',async()=>{const result=await api(`/api/mcp/servers/${item.id}/test`,{method:'POST'});this.ctx.toast(`发现 ${result.result.tools.length} 个工具`,'MCP 已连接');await this.load()})},async saveConnector(){await api('/api/connectors',{method:'POST',body:{...this.connectorForm,workspace_id:this.ctx.state.workspaceId}});this.connectorForm.password='';this.connectorForm.app_secret='';await this.load()},async testConnector(item){await this.ctx.run('正在发送测试消息',async()=>{await api(`/api/connectors/${item.id}/test`,{method:'POST'});this.ctx.toast('测试消息已送达','通知连接正常')})},async saveFeishuBot(){await api('/api/feishu-bot',{method:'PUT',body:this.botForm});this.botForm.app_secret='';this.botForm.event_verification_token='';await this.load()},async loadFeishuChats(){const result=await api('/api/feishu-bot/chats');this.feishuChats=result.chats;if(!this.botForm.receive_id&&result.chats.length)this.botForm.receive_id=result.chats[0].chat_id},async linkFeishu(enabled){const sid=this.ctx.activeSession()?.id;if(!sid)return;this.sessionBot=await api(`/api/session/${sid}/feishu-bot`,{method:'PUT',body:{enabled,chat_id:this.botForm.receive_id}});this.ctx.toast('',enabled?'当前会话已连接飞书':'当前会话已断开飞书')},async addMemory(){if(!this.memoryForm.title.trim())return;await api('/api/memories',{method:'POST',body:{...this.memoryForm,workspace_id:this.ctx.state.workspaceId}});this.memoryForm={title:'',content:'',scope:'workspace'};await this.load()},async removeMemory(item){await api(`/api/memories/${item.id}`,{method:'DELETE',body:{confirm:true}});await this.load()},async addSkill(){await api('/api/skills',{method:'POST',body:{...this.skillForm,workspace_id:this.ctx.state.workspaceId}});this.skillForm={name:'',description:'',instruction:''};await this.load()}},template:`<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">Configuration</span><h1>系统设置</h1><p>管理模型、工具协议、通知、记忆、技能、算力和审计记录。</p></div></header><div class="settings-layout"><nav class="settings-nav"><button :class="{active:tab==='models'}" @click="tab='models'"><Icon name="brain"/>模型服务</button><button :class="{active:tab==='mcp'}" @click="tab='mcp'"><Icon name="bolt"/>MCP 工具</button><button :class="{active:tab==='connectors'}" @click="tab='connectors'"><Icon name="workflow"/>通知连接</button><button :class="{active:tab==='memory'}" @click="tab='memory'"><Icon name="book"/>长期记忆</button><button :class="{active:tab==='skills'}" @click="tab='skills'"><Icon name="chart"/>分析技能</button><button :class="{active:tab==='compute'}" @click="tab='compute'"><Icon name="database"/>计算资源</button><button :class="{active:tab==='audit'}" @click="tab='audit'"><Icon name="table"/>审计日志</button></nav><main class="settings-content">
    <section v-if="tab==='models'"><div class="section-heading"><h2>模型服务</h2><p>兼容 OpenAI Chat Completions 协议；密钥加密保存在本机。</p></div><div class="setting-list"><article v-for="item in providers" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.model || '继承环境变量' }} · {{ item.base_url || '环境默认地址' }}</small></div><StatusPill :status="item.has_api_key?'ready':'configured'" :label="item.has_api_key?'密钥就绪':'待配置密钥'"/><button class="button button--small" @click="testProvider(item)">测试</button></article></div><div class="settings-card"><h3>添加 OpenAI-Compatible 服务</h3><div class="form-grid"><label><span>名称</span><input v-model="providerForm.name"></label><label><span>模型 ID</span><input v-model="providerForm.model"></label><label class="span-2"><span>Base URL</span><input v-model="providerForm.base_url"></label><label><span>API Key</span><input type="password" v-model="providerForm.api_key"></label><label><span>Temperature</span><input type="number" min="0" max="2" step="0.1" v-model.number="providerForm.temperature"></label></div><button class="button button--primary" @click="saveProvider">保存模型</button></div></section>
    <section v-if="tab==='mcp'"><div class="section-heading"><h2>MCP 工具服务</h2><p>支持 Streamable HTTP、SSE、HTTP 与 stdio，提供真实握手、发现和工具调用。</p></div><div class="setting-list"><article v-for="item in mcp" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.transport }} · {{ item.url || item.command }} · {{ item.tools?.length||0 }} 个工具</small></div><StatusPill :status="item.status"/><button class="button button--small" @click="testMcp(item)">连接并发现</button></article></div><div class="settings-card"><h3>添加工具服务</h3><div class="form-grid"><label><span>名称</span><input v-model="mcpForm.name"></label><label><span>传输</span><select v-model="mcpForm.transport"><option value="streamable-http">Streamable HTTP</option><option value="sse">SSE</option><option value="http">HTTP</option><option value="stdio">stdio</option></select></label><template v-if="mcpForm.transport==='stdio'"><label><span>命令</span><input v-model="mcpForm.command" placeholder="npx"></label><label><span>参数 JSON</span><input v-model="mcpForm.argsText" placeholder='["-y","server-package"]'></label><label class="span-2"><span>环境变量 JSON（加密保存）</span><input v-model="mcpForm.envText" placeholder='{"TOKEN":"…"}'></label></template><template v-else><label class="span-2"><span>服务 URL</span><input v-model="mcpForm.url" placeholder="http://127.0.0.1:3000/mcp"></label><label class="span-2"><span>请求头 JSON</span><input v-model="mcpForm.headersText" placeholder='{"Authorization":"Bearer …"}'></label></template></div><button class="button button--primary" @click="saveMcp">保存服务</button></div></section>
    <section v-if="tab==='connectors'"><div class="section-heading"><h2>通知与协作连接</h2><p>分析结果可发送到通用 Webhook、飞书、钉钉、Slack 或 SMTP 邮件。</p></div><div class="setting-list"><article v-for="item in connectors" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.type }} · 凭据已保护</small></div><StatusPill :status="item.status"/><button class="button button--small" @click="testConnector(item)">发送测试</button></article></div><div class="settings-card"><h3>添加通知连接</h3><div class="form-grid"><label><span>名称</span><input v-model="connectorForm.name"></label><label><span>类型</span><select v-model="connectorForm.type"><option value="webhook">通用 Webhook</option><option value="lark">飞书 Webhook</option><option value="lark_app">飞书应用机器人</option><option value="dingtalk">钉钉</option><option value="slack">Slack</option><option value="email">SMTP 邮件</option></select></label><template v-if="connectorForm.type==='email'"><label><span>SMTP 主机</span><input v-model="connectorForm.host"></label><label><span>端口</span><input type="number" v-model.number="connectorForm.port"></label><label><span>用户名</span><input v-model="connectorForm.username"></label><label><span>密码</span><input type="password" v-model="connectorForm.password"></label><label><span>发件人</span><input v-model="connectorForm.sender"></label><label><span>收件人</span><input v-model="connectorForm.recipient"></label><label class="check-control"><input type="checkbox" v-model="connectorForm.use_tls">启用 STARTTLS</label></template><template v-else-if="connectorForm.type==='lark_app'"><label><span>App ID</span><input v-model="connectorForm.app_id"></label><label><span>App Secret</span><input type="password" v-model="connectorForm.app_secret"></label><label><span>接收 ID</span><input v-model="connectorForm.receive_id"></label><label><span>ID 类型</span><select v-model="connectorForm.receive_id_type"><option value="chat_id">群聊</option><option value="open_id">Open ID</option><option value="user_id">User ID</option><option value="email">邮箱</option></select></label><label class="span-2"><span>事件校验 Token</span><input type="password" v-model="connectorForm.verification_token"></label></template><label v-else class="span-2"><span>Webhook URL</span><input v-model="connectorForm.url"></label></div><button class="button button--primary" @click="saveConnector">保存连接</button></div></section>
    <section v-if="tab==='memory'"><div class="section-heading"><h2>长期记忆</h2><p>保存偏好、指标口径和稳定事实；每条记录都可编辑或归档。</p></div><div class="setting-list"><article v-for="item in memories" :key="item.id"><div><b>{{ item.title }}</b><small>{{ item.content }}</small></div><span class="count-badge">{{ item.scope }}</span><button class="icon-button danger" @click="removeMemory(item)"><Icon name="close"/></button></article></div><div class="settings-card"><h3>添加记忆</h3><div class="form-grid"><label><span>标题</span><input v-model="memoryForm.title"></label><label><span>范围</span><select v-model="memoryForm.scope"><option value="workspace">当前工作空间</option><option value="user">个人全局</option></select></label><label class="span-2"><span>内容</span><textarea v-model="memoryForm.content"></textarea></label></div><button class="button button--primary" @click="addMemory">保存记忆</button></div></section>
    <section v-if="tab==='skills'"><div class="section-heading"><h2>分析技能</h2><p>用结构化执行指令沉淀可复用的分析方法。</p></div><div class="setting-list"><article v-for="item in skills" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.description }}</small></div><StatusPill :status="item.enabled?'ready':'disabled'" :label="item.enabled?'可用':'停用'"/></article></div><div class="settings-card"><h3>添加技能</h3><div class="form-grid"><label><span>名称</span><input v-model="skillForm.name"></label><label><span>说明</span><input v-model="skillForm.description"></label><label class="span-2"><span>执行指令</span><textarea v-model="skillForm.instruction"></textarea></label></div><button class="button button--primary" @click="addSkill">保存技能</button></div></section>
    <section v-if="tab==='compute' && compute"><div class="section-heading"><h2>计算资源</h2><p>检查本机 CPU、GPU 后端和已登记的远程计算节点。</p></div><div class="metric-strip"><div><small>CPU 逻辑核心</small><b>{{ compute.local.cpu_count }}</b></div><div><small>GPU 后端</small><b>{{ compute.local.gpu.backend || 'CPU' }}</b></div><div><small>Python</small><b>{{ compute.local.python }}</b></div></div><div class="settings-card"><h3>运行平台</h3><p>{{ compute.local.platform }}</p><p v-if="compute.local.gpu.devices.length">{{ compute.local.gpu.devices.join('、') }}</p></div><div class="setting-list"><article v-for="item in compute.nodes" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.username }}@{{ item.host }}:{{ item.port }}</small></div><StatusPill :status="item.status"/></article></div></section>
    <section v-if="tab==='audit'"><div class="section-heading"><h2>审计日志</h2><p>查询、分析、MCP 调用、快照恢复和交付动作都会留下证据记录。</p></div><DataTable :rows="audit"/></section>
    </main></div></section>`};
