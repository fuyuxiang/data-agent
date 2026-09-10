import { api, withWorkspace } from './api.js';
import { EmptyState, Icon, Modal, StatusPill } from './components.js';

const splitLines = value => String(value || '').split(/[\n，,]/).map(item => item.trim()).filter(Boolean);

export const HomePanel = {
  components: { EmptyState, Icon, StatusPill }, props: { ctx: Object },
  data: () => ({ home: null, loading: true }),
  computed: {
    state() { return this.ctx.state; },
    canAdmin() { return ['owner', 'editor'].includes(this.state.workspaceRole); },
    activeSpace() { return this.home?.active_space || null; },
    readyCount() { return Object.values(this.home?.readiness || {}).filter(Boolean).length; },
    greeting() {
      const hour = new Date().getHours();
      return hour < 11 ? '早上好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好';
    },
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
    ask(question = '') { this.ctx.startAnalysis(question); },
  },
  template: `<section class="workspace-page business-home">
    <header class="business-hero">
      <div><span class="eyebrow">业务工作台</span><h1>{{ greeting }}，{{ state.user?.name || '业务伙伴' }}</h1><p>从可信指标出发，直接提问、跟踪经营变化并沉淀决策成果。</p></div>
      <label v-if="home?.spaces?.length" class="business-space-picker"><span>当前业务空间</span><select :value="activeSpace?.id" @change="switchSpace($event.target.value)"><option v-for="item in home.spaces" :key="item.id" :value="item.id">{{ item.name }}{{ item.status==='published'?'':'（草稿）' }}</option></select></label>
    </header>

    <section v-if="activeSpace" class="ask-hero">
      <div><span class="ask-hero__icon"><Icon name="brain" :size="24"/></span><div><small>{{ activeSpace.business_domain || '企业经营分析' }}</small><h2>今天想从数据里确认什么？</h2><p>{{ activeSpace.description || '系统会自动使用当前空间内已认证的指标、业务口径与授权数据。' }}</p></div></div>
      <button class="button button--primary" @click="ask()"><Icon name="chat"/>新建分析</button>
    </section>

    <section v-else class="readiness-banner">
      <div><Icon name="warning"/><span><b>还没有可用的业务数据空间</b><small>管理员发布空间后，业务用户即可在不接触表和 SQL 的情况下问数。</small></span></div>
      <button v-if="canAdmin" class="button button--primary" @click="ctx.go('spaces')">去配置</button>
    </section>

    <div class="business-grid business-grid--main">
      <section class="content-card home-section">
        <div class="card-heading"><div><h2>常用分析</h2><p>已绑定当前空间的业务问题</p></div><button class="text-button" @click="ask()">自由提问</button></div>
        <div class="question-list">
          <button v-for="(question,index) in activeSpace?.recommended_questions || []" :key="question" @click="ask(question)"><span>{{ index + 1 }}</span><b>{{ question }}</b><Icon name="chevron" :size="15"/></button>
          <EmptyState v-if="!activeSpace?.recommended_questions?.length" icon="chat" title="暂无推荐问题" text="仍可自由提问；管理员也可以在业务数据空间中配置常用问题。"/>
        </div>
      </section>
      <section class="content-card home-section">
        <div class="card-heading"><div><h2>经营动态</h2><p>主动发现的异常、机会与提醒</p></div><button class="text-button" @click="ctx.go('insights')">全部洞察</button></div>
        <div class="insight-list compact">
          <article v-for="item in home?.insights || []" :key="item.id" :data-severity="item.severity"><i></i><div><b>{{ item.title }}</b><p>{{ item.summary }}</p><small>{{ ctx.time(item.detected_at) }}</small></div></article>
          <EmptyState v-if="!home?.insights?.length" icon="check" title="暂无待处理洞察" text="已订阅的指标出现变化时会在这里提示。"/>
        </div>
      </section>
    </div>

    <section class="content-card home-section metric-catalogue">
      <div class="card-heading"><div><h2>可问的认证指标</h2><p>每个数字都有统一口径、负责人和版本</p></div><StatusPill :status="home?.readiness?.metrics?'published':'draft'" :label="home?.metrics?.length+' 个可用'"/></div>
      <div class="metric-business-grid">
        <article v-for="metric in home?.metrics || []" :key="metric.id"><header><span>{{ metric.metric_type==='derived'?'派生':metric.metric_type==='composite'?'复合':'原子' }}</span><b>v{{ metric.version }}</b></header><h3>{{ metric.label || metric.name }}</h3><p>{{ metric.description || '已通过指标治理发布，可用于正式问数。' }}</p><footer><span>{{ metric.unit || '无单位' }}</span><span>{{ metric.business_owner || '待认领业务负责人' }}</span></footer></article>
        <EmptyState v-if="!home?.metrics?.length" icon="chart" title="暂无认证指标" text="正式经营数字必须先由管理员定义并审批，探索性分析会明确标记。"/>
      </div>
    </section>

    <div class="business-grid">
      <section class="content-card home-section"><div class="card-heading"><div><h2>最近分析</h2><p>你的可信问数记录</p></div><button class="text-button" @click="ctx.go('chat')">进入分析</button></div><div class="activity-list"><button v-for="item in home?.recent_analyses?.slice(0,5) || []" :key="item.id" @click="ctx.openAnalysis(item)"><span class="activity-icon"><Icon name="chat" :size="15"/></span><span><b>{{ item.objective || '数据分析' }}</b><small>{{ ctx.time(item.updated_at) }}</small></span><StatusPill :status="item.published?'published':item.status"/></button><EmptyState v-if="!home?.recent_analyses?.length" icon="chat" title="还没有分析记录" text="从一个业务问题开始。"/></div></section>
      <section class="content-card home-section"><div class="card-heading"><div><h2>我的交付</h2><p>报告与订阅自动送达</p></div></div><div class="delivery-summary"><button @click="ctx.go('reports')"><span><Icon name="book"/></span><b>{{ home?.reports?.length || 0 }}</b><small>分析报告</small></button><button @click="ctx.go('insights')"><span><Icon name="workflow"/></span><b>{{ home?.subscriptions?.filter(item=>item.enabled).length || 0 }}</b><small>有效订阅</small></button><div><span><Icon name="check"/></span><b>{{ readyCount }}/3</b><small>产品就绪度</small></div></div></section>
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
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">主动分析</span><h1>洞察与订阅</h1><p>系统持续关注经营变化；你只需要决定哪些信号值得行动。</p></div><button class="button button--primary" @click="openCreate"><Icon name="plus"/>新建订阅</button></header>
    <nav class="page-tabs"><button :class="{active:tab==='insights'}" @click="tab='insights'">经营洞察 <span v-if="insights.length">{{ insights.length }}</span></button><button :class="{active:tab==='subscriptions'}" @click="tab='subscriptions'">我的订阅</button></nav>
    <div v-if="tab==='insights'" class="insight-board"><article v-for="item in insights" :key="item.id" class="insight-card" :data-severity="item.severity"><header><span class="insight-signal"><i></i>{{ {critical:'重要异常',warning:'风险提醒',opportunity:'增长机会',info:'经营变化'}[item.severity] || '经营变化' }}</span><small>{{ ctx.time(item.detected_at) }}</small></header><h2>{{ item.title }}</h2><p>{{ item.summary }}</p><footer><button class="button button--small button--primary" @click="ask(item)">进一步分析</button><button class="button button--small" @click="receipt(item)">标记已读</button><button class="text-button" @click="receipt(item,true)">不再显示</button></footer></article><EmptyState v-if="!insights.length" icon="check" title="当前没有待处理洞察" text="新洞察会保留触发指标、时间和证据来源。"/></div>
    <div v-else class="subscription-list"><article v-for="item in subscriptions" :key="item.id"><span class="subscription-icon"><Icon name="workflow"/></span><div><header><b>{{ item.name }}</b><StatusPill :status="item.enabled?'active':'paused'"/></header><p>{{ item.question || item.condition || '定期推送所选指标变化' }}</p><small>{{ {daily:'每天',weekly:'每周',monthly:'每月'}[item.frequency] }} {{ item.delivery_time }} · {{ {in_app:'站内',email:'邮件',feishu:'飞书',webhook:'Webhook'}[item.channel] }}<template v-if="item.last_run_at"> · 最近执行 {{ ctx.time(item.last_run_at) }}</template></small></div><button class="button button--small" @click="runNow(item)">立即运行</button><button class="switch" :class="{on:item.enabled}" @click="toggle(item)"><i></i></button><button class="icon-button danger" @click="remove(item)"><Icon name="close"/></button></article><EmptyState v-if="!subscriptions.length" icon="workflow" title="还没有数据订阅" text="按业务语言设置关注对象、频率和送达渠道。"/></div>
    <Modal :open="open" title="新建数据订阅" @close="open=false"><div class="form-grid"><label class="span-2"><span>订阅名称</span><input v-model.trim="form.name" placeholder="每日经营简报"></label><label><span>业务数据空间</span><select v-model="form.business_space_id"><option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label><span>关注指标（可选）</span><select v-model="form.metric_id"><option value="">综合分析任务</option><option v-for="item in availableMetrics" :key="item.id" :value="item.id">{{ item.label || item.name }}</option></select></label><label class="span-2"><span>希望系统持续回答的问题</span><textarea v-model="form.question" placeholder="例如：每天汇总收入、成本和毛利的主要变化"></textarea></label><label class="span-2"><span>提醒条件（可选）</span><input v-model="form.condition" placeholder="例如：环比下降超过 10% 时提醒"></label><label><span>频率</span><select v-model="form.frequency"><option value="daily">每天</option><option value="weekly">每周</option><option value="monthly">每月</option></select></label><label><span>送达时间</span><input v-model="form.delivery_time" type="time"></label><label><span>送达渠道</span><select v-model="form.channel" @change="form.connector_id=''"><option value="in_app">站内通知</option><option value="email">邮件</option><option value="feishu">飞书</option><option value="webhook">Webhook</option></select></label><label v-if="form.channel!=='in_app'"><span>已配置通知连接</span><select v-model="form.connector_id"><option value="">请选择</option><option v-for="item in compatibleConnectors" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><p v-if="form.channel!=='in_app'&&!compatibleConnectors.length" class="form-note">管理员需先在“系统治理 → 通知连接”完成渠道配置。</p></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="!form.name||!form.business_space_id||(form.channel!=='in_app'&&!form.connector_id)" @click="save">创建订阅</button></template></Modal>
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
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">成果沉淀</span><h1>报告库</h1><p>把一次或多次可信分析组合成可发布、可刷新、可追溯的经营报告。</p></div><button class="button button--primary" :disabled="!analyses.length" @click="openCreate"><Icon name="plus"/>新建报告</button></header>
    <div class="report-library"><article v-for="item in reports" :key="item.id" class="report-card"><div class="report-cover"><Icon name="book" :size="27"/><span>DATA REPORT</span></div><div class="report-card__body"><header><StatusPill :status="item.status"/><small>v{{ item.version }}</small></header><h2>{{ item.title }}</h2><p>{{ item.description || (item.sections || []).join(' · ') }}</p><div class="tag-row"><span v-for="section in item.sections?.slice(0,4)" :key="section">{{ section }}</span></div><footer><small>{{ ctx.time(item.updated_at) }}</small><div><button v-if="item.status==='draft'" class="button button--small button--primary" @click="publish(item)">发布</button><button class="icon-button danger" @click="remove(item)"><Icon name="close"/></button></div></footer></div></article><EmptyState v-if="!reports.length" icon="book" title="报告库还是空的" text="完成一次通过验证的分析后，即可把它加入报告。"/></div>
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
  template: `<section class="workspace-page"><header class="surface-header"><div><span class="eyebrow">发布边界</span><h1>业务数据空间</h1><p>把授权数据、认证指标、业务知识和推荐问题打包发布给业务用户。MCP 工具不等同于数据源。</p></div><button class="button button--primary" @click="openCreate"><Icon name="plus"/>新建业务空间</button></header>
    <div class="space-grid"><article v-for="item in spaces" :key="item.id" class="space-card"><header><span class="space-icon"><Icon name="dashboard"/></span><StatusPill :status="item.status"/></header><h2>{{ item.name }}</h2><p>{{ item.description || '面向 '+(item.business_domain || '业务团队')+' 的可信问数范围。' }}</p><div class="space-stats"><span><b>{{ item.source_ids?.length || 0 }}</b> 数据源</span><span><b>{{ item.metric_ids?.length || 0 }}</b> 指标</span><span><b>{{ item.recommended_questions?.length || 0 }}</b> 问题</span></div><div class="readiness-track"><i :style="{width:Math.round(item.readiness.score*100)+'%'}"></i></div><small>发布就绪度 {{ Math.round(item.readiness.score*100) }}%</small><footer><button class="button button--small" @click="edit(item)">编辑</button><button v-if="item.status!=='published'" class="button button--small button--primary" :disabled="!item.readiness.publishable" @click="publish(item)">检查并发布</button><button class="icon-button danger" @click="remove(item)"><Icon name="close"/></button></footer></article><EmptyState v-if="!spaces.length" icon="dashboard" title="还没有业务数据空间" text="建议按经营域、部门或数据权限边界创建，而不是让业务用户直接选择数据库。"/></div>
    <Modal :open="open" :title="editingId?'编辑业务数据空间':'新建业务数据空间'" wide @close="open=false"><div class="form-grid"><label><span>空间名称</span><input v-model.trim="form.name" placeholder="集团经营分析"></label><label><span>业务域</span><input v-model.trim="form.business_domain" placeholder="经营管理"></label><label class="span-2"><span>空间说明</span><textarea v-model="form.description" placeholder="说明面向谁、回答什么问题以及数据边界"></textarea></label><fieldset class="span-2 selection-field"><legend>授权数据源</legend><div class="selection-grid"><label v-for="item in state.sources" :key="item.id" :class="{selected:form.source_ids.includes(item.id)}"><input type="checkbox" :checked="form.source_ids.includes(item.id)" @change="toggle(form.source_ids,item.id)"><span><b>{{ item.name }}</b><small>{{ item.kind }} · {{ item.classification || 'internal' }}</small></span></label></div><p v-if="!state.sources.length">请先在数据接入中登记数据库、文件、API 或数仓资产。</p></fieldset><fieldset class="span-2 selection-field"><legend>认证指标</legend><div class="selection-grid"><label v-for="item in approvedMetrics" :key="item.id" :class="{selected:form.metric_ids.includes(item.id)}"><input type="checkbox" :checked="form.metric_ids.includes(item.id)" @change="toggle(form.metric_ids,item.id)"><span><b>{{ item.label || item.name }}</b><small>{{ item.metric_type || 'atomic' }} · v{{ item.version }}</small></span></label></div><p v-if="!approvedMetrics.length">所选数据源还没有已审批指标。</p></fieldset><fieldset class="span-2 selection-field"><legend>已审批分析能力（可选）</legend><div class="selection-grid"><label v-for="item in publishedSkills" :key="item.id" :class="{selected:form.skill_ids.includes(item.id)}"><input type="checkbox" :checked="form.skill_ids.includes(item.id)" @change="toggle(form.skill_ids,item.id)"><span><b>{{ item.display_name || item.name }}</b><small>{{ item.description || '业务分析方法' }}</small></span></label></div><p>这些能力由统一 Agent 按问题自动调用，业务用户无需选“专家”。</p></fieldset><label><span>知识标签（每行一个）</span><textarea v-model="form.knowledgeText" placeholder="经营口径\n组织规则"></textarea></label><label><span>推荐问题（每行一个）</span><textarea v-model="form.questionsText" placeholder="本月收入是多少？\n哪个区域偏离目标最多？"></textarea></label><fieldset class="span-2 selection-field"><legend>限定成员（不选代表所有业务成员）</legend><div class="selection-grid"><label v-for="item in members" :key="item.user_id" :class="{selected:form.member_ids.includes(item.user_id)}"><input type="checkbox" :checked="form.member_ids.includes(item.user_id)" @change="toggle(form.member_ids,item.user_id)"><span><b>{{ item.name || item.email || item.user_id }}</b><small>{{ {owner:'所有者',editor:'数据管理员',analyst:'业务分析员',viewer:'只读用户'}[item.role] }}</small></span></label></div></fieldset></div><template #footer><button class="button" @click="open=false">取消</button><button class="button button--primary" :disabled="!form.name" @click="save">保存为草稿</button></template></Modal>
  </section>`,
};
