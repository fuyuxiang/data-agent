import { api, withWorkspace } from './api.js';
import { EmptyState, Icon, Modal, StatusPill } from './components.js';

const splitLines = value => String(value || '').split(/[\n，,]/).map(item => item.trim()).filter(Boolean);

export const HomePanel = {
  components: { EmptyState, Icon, StatusPill }, props: { ctx: Object },
  data: () => ({ home: null, loading: true, question: '' }),
  computed: {
    state() { return this.ctx.state; },
    canAdmin() { return ['owner', 'editor'].includes(this.state.workspaceRole); },
    activeSpace() { return this.home?.active_space || null; },
    readyCount() { return Object.values(this.home?.readiness || {}).filter(Boolean).length; },
    activeSubscriptions() { return this.home?.subscriptions?.filter(item => item.enabled).length || 0; },
    pendingInsights() { return this.home?.insights?.filter(item => !item.acknowledged && !item.dismissed).length || 0; },
  },
  mounted() { this.load(); },
  methods: {
    async load() {
      this.loading = true;
      try {
        const suffix = this.state.activeBusinessSpaceId ? `&business_space_id=${encodeURIComponent(this.state.activeBusinessSpaceId)}` : '';
        this.home = await api(withWorkspace('/api/business/home?view=home' + suffix, this.state.workspaceId));
        this.state.businessSpaces = this.home.spaces || [];
        this.state.activeBusinessSpaceId = this.home.active_space?.id || '';
        this.state.businessHome = this.home;
      } catch (error) { this.ctx.fail(error); }
      finally { this.loading = false; }
    },
    async switchSpace(id) {
      this.state.activeBusinessSpaceId = id;
      localStorage.setItem('meridian-business-space', id);
      await this.load();
    },
    ask(question = '') { const value = question || this.question; this.question = ''; this.ctx.startAnalysis(value); },
  },
  template: `<section class="workspace-page business-home">
    <header class="business-hero page-heading">
      <div><h1>经营分析工作台</h1><p>查看当前经营状态，发起分析并处理需要关注的数据变化。</p></div>
      <div class="page-heading__actions"><label v-if="home?.spaces?.length" class="business-space-picker"><span>业务空间</span><select :value="activeSpace?.id" @change="switchSpace($event.target.value)"><option v-for="item in home.spaces" :key="item.id" :value="item.id">{{ item.name }}{{ item.status==='published'?'':'（草稿）' }}</option></select></label><button class="button button--primary" @click="ask()"><Icon name="plus"/>新建分析</button></div>
    </header>

    <section v-if="activeSpace" class="analysis-launcher">
      <div class="analysis-launcher__context"><span><Icon name="brain" :size="17"/></span><div><b>向数据提问</b><small>基于“{{ activeSpace.name }}”中的已授权数据与认证指标</small></div></div>
      <form @submit.prevent="ask()"><input v-model.trim="question" placeholder="例如：本月收入与目标差距多大，主要由哪些区域造成？"><button class="button button--primary" type="submit" :disabled="!question"><Icon name="play"/>开始分析</button></form>
    </section>

    <section v-else class="readiness-banner">
      <div><Icon name="warning"/><span><b>尚未发布业务空间</b><small>业务空间用于限定可访问的数据、指标和分析能力。</small></span></div>
      <button v-if="canAdmin" class="button button--primary" @click="ctx.go('spaces')">配置业务空间</button>
    </section>

    <section v-if="activeSpace" class="overview-strip" aria-label="工作台状态">
      <button @click="ctx.go('chat')"><span>可用指标</span><b>{{ home?.metrics?.length || 0 }}</b><small>已发布认证指标</small></button>
      <button @click="ctx.go('insights')"><span>待处理洞察</span><b :class="{'attention':pendingInsights}">{{ pendingInsights }}</b><small>需要查看或确认</small></button>
      <button @click="ctx.go('insights')"><span>运行中订阅</span><b>{{ activeSubscriptions }}</b><small>自动监测任务</small></button>
      <button @click="ctx.go('reports')"><span>分析报告</span><b>{{ home?.reports?.length || 0 }}</b><small>已沉淀业务成果</small></button>
    </section>

    <div v-if="activeSpace" class="business-grid business-grid--main">
      <section class="content-card home-section enterprise-panel">
        <div class="card-heading"><div><h2>常用分析</h2><p>按当前业务空间配置的高频分析任务</p></div><button class="text-button" @click="ask()">自定义问题</button></div>
        <div class="question-list">
          <button v-for="(question,index) in activeSpace?.recommended_questions?.slice(0,5) || []" :key="question" @click="ask(question)"><span>{{ index + 1 }}</span><b>{{ question }}</b><Icon name="chevron" :size="15"/></button>
          <EmptyState v-if="!activeSpace?.recommended_questions?.length" icon="chat" title="暂无常用分析" text="可在上方直接输入业务问题。"/>
        </div>
      </section>
      <section class="content-card home-section enterprise-panel">
        <div class="card-heading"><div><h2>待关注事项</h2><p>自动监测发现的异常、机会与提醒</p></div><button class="text-button" @click="ctx.go('insights')">查看全部</button></div>
        <div class="insight-list compact">
          <article v-for="item in home?.insights?.slice(0,5) || []" :key="item.id" :data-severity="item.severity"><i></i><div><b>{{ item.title }}</b><p>{{ item.summary }}</p><small>{{ ctx.time(item.detected_at) }}</small></div></article>
          <EmptyState v-if="!home?.insights?.length" icon="check" title="暂无待处理事项" text="当前订阅未发现需要处理的数据变化。"/>
        </div>
      </section>
    </div>

    <div v-if="activeSpace" class="business-grid business-grid--secondary">
      <section class="content-card home-section enterprise-panel"><div class="card-heading"><div><h2>最近分析</h2><p>最近发起或更新的分析记录</p></div><button class="text-button" @click="ctx.go('chat')">全部分析</button></div><div class="activity-list"><button v-for="item in home?.recent_analyses?.slice(0,6) || []" :key="item.id" @click="ctx.openAnalysis(item)"><span class="activity-icon"><Icon name="chat" :size="15"/></span><span><b>{{ item.objective || '数据分析' }}</b><small>{{ ctx.time(item.updated_at) }}</small></span><StatusPill :status="item.published?'published':item.status"/></button><EmptyState v-if="!home?.recent_analyses?.length" icon="chat" title="暂无分析记录" text="从上方输入一个业务问题开始。"/></div></section>
      <section class="content-card home-section enterprise-panel"><div class="card-heading"><div><h2>业务空间概览</h2><p>{{ activeSpace.business_domain || '经营管理' }} · {{ activeSpace.description || '当前分析数据与权限范围' }}</p></div><StatusPill :status="activeSpace.status"/></div><dl class="space-overview"><div><dt>授权数据源</dt><dd>{{ activeSpace.source_ids?.length || 0 }} 个</dd></div><div><dt>认证指标</dt><dd>{{ activeSpace.metric_ids?.length || 0 }} 个</dd></div><div><dt>分析能力</dt><dd>{{ activeSpace.skill_ids?.length || 0 }} 项</dd></div><div><dt>配置完整度</dt><dd>{{ readyCount }}/3</dd></div></dl><div class="metric-inline-list"><span v-for="metric in home?.metrics?.slice(0,5) || []" :key="metric.id"><b>{{ metric.label || metric.name }}</b><small>{{ metric.unit || '无单位' }} · v{{ metric.version }}</small></span><button v-if="!home?.metrics?.length && canAdmin" class="text-button" @click="ctx.go('semantic')">配置认证指标</button></div></section>
    </div>
  </section>`,
};

export const InsightsPanel = {
  components: { EmptyState, Icon, Modal, StatusPill }, props: { ctx: Object },
  data: () => ({ insights: [], subscriptions: [], spaces: [], metrics: [], connectors: [], tab: 'insights', open: false, form: { name: '', business_space_id: '', metric_id: '', question: '', condition: '', frequency: 'daily', delivery_time: '09:00', channel: 'in_app', connector_id: '' } }),
  computed: { state() { return this.ctx.state; }, selectedSpace() { return this.spaces.find(item => item.id === this.form.business_space_id); }, availableMetrics() { const ids = new Set(this.selectedSpace?.metric_ids || []); return this.metrics.filter(item => ids.has(item.id)); }, compatibleConnectors() { const types = { email:['email'], feishu:['lark','lark_app'], webhook:['webhook','dingtalk','slack','lark'] }[this.form.channel] || []; return this.connectors.filter(item=>types.includes(item.type)&&item.enabled!==false); } },
  mounted() { this.load(); },
  methods: {
    async load() {
      try {
        const [insights, subscriptions, home, connectors] = await Promise.all([
          api(withWorkspace('/api/insights', this.state.workspaceId)), api(withWorkspace('/api/subscriptions', this.state.workspaceId)), api(withWorkspace('/api/business/home', this.state.workspaceId)), api(withWorkspace('/api/connectors', this.state.workspaceId)),
        ]);
        this.insights = insights.items || []; this.subscriptions = subscriptions.items || [];
        this.spaces = (home.spaces || []).filter(item => item.status === 'published'); this.metrics = home.metrics || []; this.connectors = connectors.items || [];
      } catch (error) { this.ctx.fail(error); }
    },
    openCreate() { const space = this.spaces.find(item => item.id === this.state.activeBusinessSpaceId) || this.spaces[0]; this.form = { name: '', business_space_id: space?.id || '', metric_id: '', question: '', condition: '', frequency: 'daily', delivery_time: '09:00', channel: 'in_app', connector_id: '' }; this.open = true; },
    async save() { try { await api('/api/subscriptions', { method: 'POST', body: { ...this.form, workspace_id: this.state.workspaceId } }); this.open = false; await this.load(); this.ctx.toast('系统会按设定条件检查并推送', '订阅已创建'); } catch (error) { this.ctx.fail(error); } },
    async toggle(item) { await api(`/api/subscriptions/${item.id}`, { method: 'PATCH', body: { enabled: !item.enabled } }); await this.load(); },
    async remove(item) { if (!confirm(`删除订阅“${item.name}”？`)) return; await api(`/api/subscriptions/${item.id}`, { method: 'DELETE' }); await this.load(); },
    async runNow(item) { try { await api(`/api/subscriptions/${item.id}/run`, { method: 'POST' }); await this.load(); this.ctx.toast('已进入可信分析队列，完成后会生成一条洞察', '订阅已触发'); } catch (error) { this.ctx.fail(error); } },
    async receipt(item, dismissed = false) { await api(`/api/insights/${item.id}/receipt`, { method: 'POST', body: { acknowledged: !dismissed, dismissed } }); await this.load(); },
    ask(item) { this.ctx.startAnalysis(item.summary || item.title); },
  },
  template: `<section class="workspace-page"><header class="surface-header page-heading"><div><h1>经营洞察</h1><p>集中处理自动监测发现的数据变化，并管理周期性分析订阅。</p></div><button class="button button--primary" @click="openCreate"><Icon name="plus"/>新建订阅</button></header>
    <nav class="page-tabs enterprise-tabs"><button :class="{active:tab==='insights'}" @click="tab='insights'">待处理洞察 <span v-if="insights.length">{{ insights.length }}</span></button><button :class="{active:tab==='subscriptions'}" @click="tab='subscriptions'">分析订阅 <span v-if="subscriptions.length">{{ subscriptions.length }}</span></button></nav>
    <section v-if="tab==='insights'" class="resource-list insight-board"><header class="resource-list__header insight-columns"><span>洞察内容</span><span>类型</span><span>发现时间</span><span>操作</span></header><article v-for="item in insights" :key="item.id" class="resource-row insight-columns" :data-severity="item.severity"><div class="resource-primary"><i class="severity-dot"></i><span><b>{{ item.title }}</b><small>{{ item.summary }}</small></span></div><span class="insight-signal">{{ {critical:'重要异常',warning:'风险提醒',opportunity:'增长机会',info:'经营变化'}[item.severity] || '经营变化' }}</span><time>{{ ctx.time(item.detected_at) }}</time><div class="row-actions"><button class="text-button" @click="ask(item)">分析原因</button><button class="text-button" @click="receipt(item)">标记已处理</button><button class="icon-button danger" title="不再显示" @click="receipt(item,true)"><Icon name="close"/></button></div></article><EmptyState v-if="!insights.length" icon="check" title="暂无待处理洞察" text="自动监测发现异常或机会后，将在此生成待办记录。"/></section>
    <section v-else class="resource-list subscription-list"><header class="resource-list__header subscription-columns"><span>订阅任务</span><span>执行计划</span><span>送达渠道</span><span>状态</span><span>操作</span></header><article v-for="item in subscriptions" :key="item.id" class="resource-row subscription-columns"><div class="resource-primary"><span class="subscription-icon"><Icon name="workflow"/></span><span><b>{{ item.name }}</b><small>{{ item.question || item.condition || '定期检查所选指标变化' }}</small></span></div><span>{{ {daily:'每天',weekly:'每周',monthly:'每月'}[item.frequency] }} {{ item.delivery_time }}<small v-if="item.last_run_at">最近执行 {{ ctx.time(item.last_run_at) }}</small></span><span>{{ {in_app:'站内通知',email:'邮件',feishu:'飞书',webhook:'Webhook'}[item.channel] }}</span><StatusPill :status="item.enabled?'active':'paused'"/><div class="row-actions"><button class="text-button" @click="runNow(item)">立即运行</button><button class="switch" :class="{on:item.enabled}" :title="item.enabled?'暂停订阅':'启用订阅'" @click="toggle(item)"><i></i></button><button class="icon-button danger" title="删除订阅" @click="remove(item)"><Icon name="close"/></button></div></article><EmptyState v-if="!subscriptions.length" icon="workflow" title="暂无分析订阅" text="创建订阅后，系统将按设定的频率检查数据并发送结果。"/></section>
    <Modal :open="open" title="新建分析订阅" @close="open=false"><div class="form-grid"><label class="span-2"><span>订阅名称</span><input v-model.trim="form.name" placeholder="每日经营简报"></label><label><span>业务空间</span><select v-model="form.business_space_id"><option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label><span>关注指标（可选）</span><select v-model="form.metric_id"><option value="">综合分析任务</option><option v-for="item in availableMetrics" :key="item.id" :value="item.id">{{ item.label || item.name }}</option></select></label><label class="span-2"><span>持续分析问题</span><textarea v-model="form.question" placeholder="例如：每天汇总收入、成本和毛利的主要变化"></textarea></label><label class="span-2"><span>提醒条件（可选）</span><input v-model="form.condition" placeholder="例如：环比下降超过 10% 时提醒"></label><label><span>执行频率</span><select v-model="form.frequency"><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label><label><span>送达时间</span><input v-model="form.delivery_time" type="time"></label><label><span>送达渠道</span><select v-model="form.channel" @change="form.connector_id=''"><option value="in_app">站内通知</option><option value="email">邮件</option><option value="feishu">飞书</option><option value="webhook">Webhook</option></select></label><label v-if="form.channel!=='in_app'"><span>通知连接</span><select v-model="form.connector_id"><option value="">请选择</option><option v-for="item in compatibleConnectors" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><p v-if="form.channel!=='in_app'&&!compatibleConnectors.length" class="form-note">请先在“平台设置 → 通知连接”完成渠道配置。</p></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="!form.name||!form.business_space_id||(form.channel!=='in_app'&&!form.connector_id)" @click="save">创建订阅</button></template></Modal>
  </section>`,
};

export const ReportsPanel = {
  components: { EmptyState, Icon, Modal, StatusPill }, props: { ctx: Object },
  data: () => ({ reports: [], analyses: [], open: false, form: { title: '', description: '', run_id: '', sectionsText: '核心结论\n指标表现\n原因分析\n行动建议' } }),
  computed: { state() { return this.ctx.state; } }, mounted() { this.load(); },
  methods: {
    async load() { try { const [reports, home] = await Promise.all([api(withWorkspace('/api/reports', this.state.workspaceId)), api(withWorkspace('/api/business/home', this.state.workspaceId))]); this.reports = reports.items || []; this.analyses = (home.recent_analyses || []).filter(item => item.published); } catch (error) { this.ctx.fail(error); } },
    openCreate() { this.form = { title: '', description: '', run_id: this.analyses[0]?.id || '', sectionsText: '核心结论\n指标表现\n原因分析\n行动建议' }; this.open = true; },
    async save() { try { await api('/api/reports', { method: 'POST', body: { workspace_id: this.state.workspaceId, title: this.form.title, description: this.form.description, run_id: this.form.run_id, sections: splitLines(this.form.sectionsText) } }); this.open = false; await this.load(); this.ctx.toast('已建立与原分析结果的版本绑定', '报告草稿已创建'); } catch (error) { this.ctx.fail(error); } },
    async publish(item) { try { await api(`/api/reports/${item.id}/publish`, { method: 'POST', body: { visibility: 'private' } }); await this.load(); this.ctx.toast('报告内容已锁定到已验证结果版本', '报告已发布'); } catch (error) { this.ctx.fail(error); } },
    async remove(item) { if (!confirm(`删除报告“${item.title}”？`)) return; await api(`/api/reports/${item.id}`, { method: 'DELETE' }); await this.load(); },
  },
  template: `<section class="workspace-page"><header class="surface-header page-heading"><div><h1>分析报告</h1><p>管理由已验证分析结果生成的正式业务报告及其发布版本。</p></div><button class="button button--primary" :disabled="!analyses.length" @click="openCreate"><Icon name="plus"/>新建报告</button></header>
    <section class="resource-list report-library"><header class="resource-list__header report-columns"><span>报告名称</span><span>内容结构</span><span>状态</span><span>最近更新</span><span>操作</span></header><article v-for="item in reports" :key="item.id" class="resource-row report-columns"><div class="resource-primary"><span class="resource-icon"><Icon name="book" :size="18"/></span><span><b>{{ item.title }}</b><small>{{ item.description || '由已验证分析结果生成' }}</small></span></div><div class="tag-row"><span v-for="section in item.sections?.slice(0,3)" :key="section">{{ section }}</span><small v-if="item.sections?.length>3">+{{ item.sections.length-3 }}</small></div><div><StatusPill :status="item.status"/><small class="version-label">版本 v{{ item.version }}</small></div><time>{{ ctx.time(item.updated_at) }}</time><div class="row-actions"><button v-if="item.status==='draft'" class="button button--small button--primary" @click="publish(item)">发布</button><button class="icon-button danger" title="删除报告" @click="remove(item)"><Icon name="close"/></button></div></article><EmptyState v-if="!reports.length" icon="book" title="暂无分析报告" text="完成并发布一次分析后，可将结果整理为正式报告。"/></section>
    <Modal :open="open" title="从可信分析创建报告" @close="open=false"><div class="form-grid"><label class="span-2"><span>报告标题</span><input v-model.trim="form.title" placeholder="2026 年 9 月经营分析报告"></label><label class="span-2"><span>已发布分析结果</span><select v-model="form.run_id"><option v-for="item in analyses" :key="item.id" :value="item.id">{{ item.objective || item.id }} · {{ ctx.time(item.updated_at) }}</option></select></label><label class="span-2"><span>报告说明</span><textarea v-model="form.description" placeholder="面向经营会的月度分析"></textarea></label><label class="span-2"><span>章节（每行一个）</span><textarea v-model="form.sectionsText"></textarea></label></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="!form.title||!form.run_id" @click="save">创建草稿</button></template></Modal>
  </section>`,
};

export const BusinessSpacesPanel = {
  components: { EmptyState, Icon, Modal, StatusPill }, props: { ctx: Object },
  data: () => ({ spaces: [], metrics: [], members: [], open: false, editingId: '', form: { name: '', business_domain: '', description: '', source_ids: [], metric_ids: [], skill_ids: [], knowledgeText: '', questionsText: '', member_ids: [] } }),
  computed: { state() { return this.ctx.state; }, approvedMetrics() { return this.metrics.filter(item => item.status === 'approved' && this.form.source_ids.includes(item.source_id)); }, publishedSkills() { return this.state.skills.filter(item => !item.status || item.status === 'published'); } },
  mounted() { this.load(); },
  methods: {
    async load() { try { const [spaces, metrics, members] = await Promise.all([api(withWorkspace('/api/business-spaces', this.state.workspaceId)), api(withWorkspace('/api/semantic/metrics', this.state.workspaceId)), api(`/api/workspaces/${this.state.workspaceId}/members`)]); this.spaces = spaces.items || []; this.metrics = metrics.items || []; this.members = members.items || []; this.state.businessSpaces = this.spaces; } catch (error) { this.ctx.fail(error); } },
    openCreate() { this.editingId = ''; this.form = { name: '', business_domain: '', description: '', source_ids: [], metric_ids: [], skill_ids: [], knowledgeText: '', questionsText: '', member_ids: [] }; this.open = true; },
    edit(item) { this.editingId = item.id; this.form = { name: item.name || '', business_domain: item.business_domain || '', description: item.description || '', source_ids: [...(item.source_ids || [])], metric_ids: [...(item.metric_ids || [])], skill_ids: [...(item.skill_ids || [])], knowledgeText: (item.knowledge_tags || []).join('\n'), questionsText: (item.recommended_questions || []).join('\n'), member_ids: [...(item.member_ids || [])] }; this.open = true; },
    async save() { try { const payload = { workspace_id: this.state.workspaceId, ...this.form, knowledge_tags: splitLines(this.form.knowledgeText), recommended_questions: splitLines(this.form.questionsText) }; delete payload.knowledgeText; delete payload.questionsText; const path = this.editingId ? `/api/business-spaces/${this.editingId}` : '/api/business-spaces'; await api(path, { method: this.editingId ? 'PATCH' : 'POST', body: payload }); this.open = false; await this.load(); this.ctx.toast('变更后需重新通过发布检查', '业务数据空间已保存'); } catch (error) { this.ctx.fail(error); } },
    async publish(item) { try { await api(`/api/business-spaces/${item.id}/publish`, { method: 'POST' }); await this.load(); this.ctx.toast('业务用户现在可以基于该空间问数', '空间已发布'); } catch (error) { this.ctx.fail(error); } },
    async remove(item) { if (!confirm(`归档业务数据空间“${item.name}”？`)) return; await api(`/api/business-spaces/${item.id}`, { method: 'DELETE' }); await this.load(); },
    toggle(list, id) { const index = list.indexOf(id); index >= 0 ? list.splice(index, 1) : list.push(id); if (list === this.form.source_ids) this.form.metric_ids = this.form.metric_ids.filter(metricId => this.approvedMetrics.some(metric => metric.id === metricId)); },
  },
  template: `<section class="workspace-page"><header class="surface-header page-heading"><div><h1>业务空间</h1><p>按业务域配置可使用的数据源、认证指标、知识规则和成员权限。</p></div><button class="button button--primary" @click="openCreate"><Icon name="plus"/>新建业务空间</button></header>
    <section class="resource-list space-grid"><header class="resource-list__header space-columns"><span>空间名称</span><span>资源范围</span><span>访问范围</span><span>发布状态</span><span>操作</span></header><article v-for="item in spaces" :key="item.id" class="resource-row space-columns"><div class="resource-primary"><span class="resource-icon"><Icon name="dashboard"/></span><span><b>{{ item.name }}</b><small>{{ item.business_domain || '未设置业务域' }} · {{ item.description || '暂无说明' }}</small></span></div><div class="scope-summary"><span><b>{{ item.source_ids?.length || 0 }}</b> 数据源</span><span><b>{{ item.metric_ids?.length || 0 }}</b> 指标</span><span><b>{{ item.skill_ids?.length || 0 }}</b> 能力</span></div><span>{{ item.member_ids?.length ? item.member_ids.length+' 名指定成员' : '全部业务成员' }}</span><div class="publish-state"><StatusPill :status="item.status"/><small>配置完整度 {{ Math.round(item.readiness.score*100) }}%</small><div class="readiness-track"><i :style="{width:Math.round(item.readiness.score*100)+'%'}"></i></div></div><div class="row-actions"><button class="button button--small" @click="edit(item)">编辑</button><button v-if="item.status!=='published'" class="button button--small button--primary" :disabled="!item.readiness.publishable" @click="publish(item)">发布</button><button class="icon-button danger" title="归档业务空间" @click="remove(item)"><Icon name="close"/></button></div></article><EmptyState v-if="!spaces.length" icon="dashboard" title="暂无业务空间" text="创建业务空间后，业务人员才能在明确的数据与权限范围内发起分析。"/></section>
    <Modal :open="open" :title="editingId?'编辑业务空间':'新建业务空间'" wide @close="open=false"><div class="form-grid space-form"><div class="form-section-title span-2"><b>基本信息</b><small>定义空间用途与所属业务域</small></div><label><span>空间名称</span><input v-model.trim="form.name" placeholder="集团经营分析"></label><label><span>业务域</span><input v-model.trim="form.business_domain" placeholder="经营管理"></label><label class="span-2"><span>空间说明</span><textarea v-model="form.description" placeholder="说明适用对象、分析主题和数据边界"></textarea></label><div class="form-section-title span-2"><b>数据与指标</b><small>仅已授权数据源中的认证指标可用于正式分析</small></div><fieldset class="selection-field"><legend>授权数据源</legend><div class="selection-grid"><label v-for="item in state.sources" :key="item.id" :class="{selected:form.source_ids.includes(item.id)}"><input type="checkbox" :checked="form.source_ids.includes(item.id)" @change="toggle(form.source_ids,item.id)"><span><b>{{ item.name }}</b><small>{{ item.kind }} · {{ item.classification || '内部数据' }}</small></span></label></div><p v-if="!state.sources.length">请先在数据源管理中完成数据接入。</p></fieldset><fieldset class="selection-field"><legend>认证指标</legend><div class="selection-grid"><label v-for="item in approvedMetrics" :key="item.id" :class="{selected:form.metric_ids.includes(item.id)}"><input type="checkbox" :checked="form.metric_ids.includes(item.id)" @change="toggle(form.metric_ids,item.id)"><span><b>{{ item.label || item.name }}</b><small>{{ {atomic:'原子指标',derived:'派生指标',composite:'复合指标'}[item.metric_type] || '指标' }} · v{{ item.version }}</small></span></label></div><p v-if="!approvedMetrics.length">所选数据源中暂无已审批指标。</p></fieldset><div class="form-section-title span-2"><b>分析上下文</b><small>系统根据问题自动应用知识与分析能力</small></div><fieldset class="span-2 selection-field"><legend>已发布分析能力（可选）</legend><div class="selection-grid"><label v-for="item in publishedSkills" :key="item.id" :class="{selected:form.skill_ids.includes(item.id)}"><input type="checkbox" :checked="form.skill_ids.includes(item.id)" @change="toggle(form.skill_ids,item.id)"><span><b>{{ item.display_name || item.name }}</b><small>{{ item.description || '业务分析方法' }}</small></span></label></div></fieldset><label><span>知识标签（每行一个）</span><textarea v-model="form.knowledgeText" placeholder="经营口径\n组织规则"></textarea></label><label><span>常用分析问题（每行一个）</span><textarea v-model="form.questionsText" placeholder="本月收入与目标差距多大？\n哪些区域偏离目标最多？"></textarea></label><div class="form-section-title span-2"><b>访问权限</b><small>不指定成员时，对全部业务成员开放</small></div><fieldset class="span-2 selection-field"><legend>指定可访问成员</legend><div class="selection-grid"><label v-for="item in members" :key="item.user_id" :class="{selected:form.member_ids.includes(item.user_id)}"><input type="checkbox" :checked="form.member_ids.includes(item.user_id)" @change="toggle(form.member_ids,item.user_id)"><span><b>{{ item.name || item.email || item.user_id }}</b><small>{{ {owner:'所有者',editor:'数据管理员',analyst:'业务分析员',viewer:'只读用户'}[item.role] }}</small></span></label></div></fieldset></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="!form.name" @click="save">保存草稿</button></template></Modal>
  </section>`,
};
