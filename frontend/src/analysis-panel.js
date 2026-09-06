import { api, withWorkspace } from './api.js';
import { ChartView, DataTable, EmptyState, Icon, Modal, StatusPill, renderMarkdown } from './components.js';

const { nextTick } = Vue;
const TERMINAL = new Set(['finished', 'failed', 'cancelled']);
const ACTIVE = new Set(['queued', 'running', 'waiting_job', 'cancelling']);

export const AnalysisPanel = {
  components: { ChartView, DataTable, EmptyState, Icon, Modal, StatusPill },
  props: { ctx: Object },
  data: () => ({
    prompt: '', selectedSkill: '', current: null, events: [], eventCursor: 0, result: null, evidence: null,
    details: [], detailColumns: [], detailCursor: 0, activeTab: 'summary', pollingTimer: null,
    demoLoading: false,
    artifacts: [], attachments: [],
    contractForm: { objective: '', coverage: '', dimensions: '', deliverables: '' },
    emailOpen: false, emailMode: 'eml', email: {
      recipients: '', subject: '', body: '', connector_id: '',
      kinds: ['summary_docx', 'report_docx', 'dashboard_png'],
    },
  }),
  computed: {
    state() { return this.ctx.state; },
    session() { return this.ctx.activeSession(); },
    selectedSources() { return this.ctx.selectedSources(); },
    contract() { return this.current?.contract || null; },
    manifest() { return this.result?.manifest?.payload || null; },
    processing() { return ACTIVE.has(this.current?.execution_status); },
    canSend() {
      return !!this.prompt.trim() && !!this.session && !this.processing
        && (!this.current || TERMINAL.has(this.current.execution_status));
    },
  },
  watch: {
    session(value, previous) {
      if (value?.id !== previous?.id) this.load();
    },
  },
  mounted() { this.load(); },
  beforeUnmount() { clearTimeout(this.pollingTimer); },
  methods: {
    md: renderMarkdown,
    split(value) {
      return String(value || '').split(/[，,\n]/).map(item => item.trim()).filter(Boolean);
    },
    percent(value) { return Math.round(Number(value || 0) * 100); },
    async seedDemo() {
      if (this.demoLoading) return;
      this.demoLoading = true;
      try {
        await api(withWorkspace('/api/onboarding/demo', this.state.workspaceId), { method: 'POST' });
        await this.ctx.bootstrap();
        this.ctx.toast('已接入样例数据、业务口径和审批指标，可直接发起经营分析', '演示空间已准备');
      } catch (error) {
        this.ctx.fail(error);
      } finally { this.demoLoading = false; }
    },
    syncContract() {
      const value = this.contract?.payload || {};
      this.contractForm = {
        objective: value.objective || '', coverage: value.coverage || '',
        dimensions: (value.dimensions || []).join('，'),
        deliverables: (value.deliverables || []).join('，'),
      };
    },
    async load() {
      clearTimeout(this.pollingTimer);
      this.current = null; this.events = []; this.result = null; this.artifacts = [];
      if (!this.session) return;
      try {
        const path = '/api/analyses?session_id=' + encodeURIComponent(this.session.id) + '&limit=50';
        const response = await api(withWorkspace(path, this.state.workspaceId));
        if (response.items?.length) await this.setRun(response.items[0]);
      } catch (error) { this.ctx.fail(error); }
    },
    async setRun(run) {
      clearTimeout(this.pollingTimer);
      this.current = run; this.eventCursor = 0; this.events = []; this.result = null;
      this.details = []; this.artifacts = []; this.evidence = null;
      this.syncContract();
      await this.refresh(true);
    },
    schedulePoll() {
      clearTimeout(this.pollingTimer);
      if (this.current && !TERMINAL.has(this.current.execution_status)) {
        this.pollingTimer = setTimeout(() => this.refresh(), 1100);
      }
    },
    async refresh(silent = false) {
      if (!this.current) return;
      try {
        const base = '/api/analyses/' + this.current.id;
        const [run, eventPage, attachments] = await Promise.all([
          api(withWorkspace(base, this.state.workspaceId)),
          api(withWorkspace(base + '/events?after=' + this.eventCursor + '&limit=500', this.state.workspaceId)),
          api(withWorkspace(base + '/attachments', this.state.workspaceId)),
        ]);
        this.current = run.item; this.attachments = attachments.items || [];
        for (const event of eventPage.items || []) {
          if (!this.events.some(item => item.sequence === event.sequence)) this.events.push(event);
        }
        this.eventCursor = eventPage.next_cursor || this.eventCursor;
        if (this.current.execution_status === 'finished') {
          this.result = await api(withWorkspace(base + '/results', this.state.workspaceId));
          this.artifacts = this.result.artifacts || [];
          if (this.result.status === 'published') {
            this.evidence = await api(withWorkspace(base + '/evidence', this.state.workspaceId));
          }
        }
        this.syncContract();
      } catch (error) {
        if (!silent) this.ctx.fail(error);
      } finally { this.schedulePoll(); }
    },
    usePrompt(value) {
      this.prompt = value;
      nextTick(() => this.$refs.composer?.focus());
    },
    keydown(event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
    },
    async send() {
      const objective = this.prompt.trim();
      if (!this.canSend) return;
      this.prompt = '';
      try {
        const response = await api('/api/analyses', {
          method: 'POST',
          headers: { 'Idempotency-Key': 'analysis-' + crypto.randomUUID() },
          body: {
            session_id: this.session.id, objective,
            source_ids: this.session.source_ids || [],
            provider_id: this.session.provider_id || null,
            skill_id: this.selectedSkill || null,
          },
        });
        await this.setRun(response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    contractPayload() {
      return {
        ...(this.contract?.payload || {}), ...this.contractForm,
        dimensions: this.split(this.contractForm.dimensions),
        deliverables: this.split(this.contractForm.deliverables),
        source_scope: this.current.source_scope,
      };
    },
    async saveContract() {
      const response = await api('/api/analyses/' + this.current.id + '/contract', {
        method: 'PUT',
        body: { expected_version: this.contract.version, contract: this.contractPayload() },
      });
      this.current = response.item;
      this.syncContract();
      return response.contract;
    },
    async confirmContract() {
      try {
        const saved = await this.saveContract();
        const response = await api('/api/analyses/' + this.current.id + '/contract/confirm', {
          method: 'POST',
          body: { expected_version: saved.version, contract: saved.payload },
        });
        this.current = response.item;
        this.schedulePoll();
      } catch (error) { this.ctx.fail(error); }
    },
    async control(action) {
      try {
        const response = await api('/api/analyses/' + this.current.id + '/control', {
          method: 'POST', body: { action, expected_version: this.current.version },
        });
        this.current = response.item;
        this.schedulePoll();
      } catch (error) { this.ctx.fail(error); }
    },
    async uploadFiles(files) {
      if (!files?.length || !this.current) return;
      const form = new FormData();
      [...files].forEach(file => form.append('files', file));
      form.append('tags', '分析附件');
      try {
        await api('/api/analyses/' + this.current.id + '/attachments', { method: 'POST', body: form });
        this.ctx.toast(files.length + ' 个附件已建立可追溯索引', '附件已加入');
        await this.refresh(true);
      } catch (error) { this.ctx.fail(error); }
    },
    async removeAttachment(item) {
      try {
        await api('/api/analyses/' + this.current.id + '/attachments/' + item.id, { method: 'DELETE' });
        await this.refresh(true);
      } catch (error) { this.ctx.fail(error); }
    },
    async loadDetails(reset = false) {
      if (!this.current || !this.manifest) return;
      if (reset) { this.detailCursor = 0; this.details = []; }
      const path = '/api/analyses/' + this.current.id + '/details?limit=100&cursor=' + this.detailCursor;
      try {
        const response = await api(withWorkspace(path, this.state.workspaceId));
        this.details.push(...(response.items || []));
        this.detailColumns = response.columns || [];
        this.detailCursor = response.next_cursor;
      } catch (error) { this.ctx.fail(error); }
    },
    async generateArtifacts() {
      try {
        const response = await api('/api/analyses/' + this.current.id + '/artifacts', {
          method: 'POST', body: { kinds: this.email.kinds },
        });
        this.artifacts = response.items || [];
        this.ctx.toast('两个 Word 与四图 PNG 已绑定当前发布版本', '成果已生成');
      } catch (error) { this.ctx.fail(error); }
    },
    openEmail() {
      this.email.subject = '数据分析成果 · ' + (this.session?.name || this.current?.id || '');
      this.email.body = this.manifest?.summary || '';
      this.emailOpen = true;
    },
    async deliverEmail() {
      try {
        const suffix = this.emailMode === 'smtp' ? 'send' : 'eml';
        const response = await api('/api/analyses/' + this.current.id + '/email/' + suffix, {
          method: 'POST',
          headers: { 'Idempotency-Key': 'mail-' + crypto.randomUUID() },
          body: this.email,
        });
        if (response.eml?.download_url) location.href = response.eml.download_url;
        this.emailOpen = false;
        this.ctx.toast(
          this.emailMode === 'smtp' ? 'SMTP 已处理当前发布版本' : '.eml 含真实 MIME 附件',
          this.emailMode === 'smtp' ? '邮件已发送' : '邮件文件已生成',
        );
      } catch (error) { this.ctx.fail(error); }
    },
    async branch(mode) {
      const labels = { followup: '继续追问', refresh: '刷新数据', reproduce: '精确复现', reanalyze: '重新分析' };
      const promptValue = window.prompt(labels[mode] + '：请描述目标', this.contract?.payload?.objective || '') || '';
      if (!promptValue.trim()) return;
      try {
        const response = await api('/api/analyses/' + this.current.id + '/branch', {
          method: 'POST', body: { mode, prompt: promptValue },
        });
        await this.setRun(response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    eventLabel(event) {
      const names = {
        'run.created': '任务已创建', 'contract.confirmed': '需求口径已确认',
        'model.requested': '模型正在决策', 'tool.started': '工具执行中',
        'tool.finished': '工具已完成', 'analysis.published': '成果通过验证并发布',
        'analysis.partial': '发布门禁阻止正式成果', 'run.status': '任务状态变化',
      };
      return names[event.type] || event.type;
    },
  },
  template: `
    <section class="chat-surface">
      <header class="surface-header chat-header">
        <div><span class="eyebrow">工作台 › 当前任务</span><h1>{{ session?.name || '新分析' }}</h1></div>
        <div class="header-cluster">
          <span class="source-chip"><i :class="{on:selectedSources.length}"></i>{{ selectedSources.length ? selectedSources.map(item=>item.name).join('、') : '尚未选择来源' }}</span>
          <StatusPill v-if="current" :status="current.execution_status"/>
        </div>
      </header>

      <div ref="feed" class="chat-feed" :class="{'chat-feed--empty':!current}">
        <div v-if="!current" class="welcome-block">
          <div class="welcome-glyph"><span></span><Icon name="brain" :size="32"/></div>
          <span class="eyebrow">受治理的自主分析</span>
          <h2>今天要从数据中确认什么？</h2>
          <p>选择授权来源并描述问题。系统先生成可编辑需求理解卡；确认后才会自主检索、查询、验证和交付。</p>
          <section v-if="state.onboarding" class="onboarding-card">
            <header>
              <div><b>标准产品开通路径</b><small>{{ percent(state.onboarding.score) }}% 完成 · {{ state.entitlements?.plan?.name || '未开通' }}</small></div>
              <button class="button button--small button--primary" :disabled="demoLoading" @click="seedDemo"><Icon name="database"/>{{ demoLoading ? '准备中' : '载入演示数据' }}</button>
            </header>
            <ol>
              <li v-for="step in state.onboarding.steps" :key="step.id" :class="{done:step.done}">
                <i></i><button @click="ctx.go(step.route)">{{ step.name }}</button><small>{{ step.done ? '已完成' : step.description }}</small>
              </li>
            </ol>
          </section>
          <div class="prompt-grid">
            <button @click="usePrompt('概览已选数据，指出最重要的三个发现和数据质量风险')"><Icon name="table"/><span><b>通用统计</b><small>结构、分布与质量</small></span></button>
            <button @click="usePrompt('识别关键指标的异常变化，并定位贡献最大的群组')"><Icon name="warning"/><span><b>异常诊断</b><small>差异、贡献与风险</small></span></button>
            <button @click="usePrompt('分析核心数值的时间趋势，并说明可验证的变化')"><Icon name="chart"/><span><b>趋势分析</b><small>走势、拐点与限制</small></span></button>
          </div>
        </div>

        <template v-else>
          <article class="message message--user">
            <div class="message__meta"><span>你</span><time>{{ ctx.time(current.created_at) }}</time></div>
            <div class="message__body">{{ contract?.payload?.objective }}</div>
          </article>

          <section v-if="contract && !contract.confirmed_at" class="analysis-contract">
            <header><div><span class="eyebrow">需求理解</span><h2>确认一次，随后自主执行</h2></div><StatusPill status="draft"/></header>
            <div class="contract-grid">
              <label><span>业务分析目标</span><textarea v-model="contractForm.objective"></textarea></label>
              <label><span>统计覆盖范围</span><textarea v-model="contractForm.coverage"></textarea></label>
              <label><span>数据查看维度</span><textarea v-model="contractForm.dimensions" placeholder="用逗号或换行分隔"></textarea></label>
              <label><span>成果交付形式</span><textarea v-model="contractForm.deliverables" placeholder="summary，dashboard，report"></textarea></label>
            </div>
            <div class="attachment-drop" @dragover.prevent @drop.prevent="uploadFiles($event.dataTransfer.files)">
              <Icon name="upload"/><span>拖拽 docx / xlsx / pdf / md / txt，单文件不超过 50MB</span>
              <label class="button button--small">选择附件<input hidden multiple type="file" accept=".docx,.xlsx,.pdf,.md,.txt" @change="uploadFiles($event.target.files)"></label>
            </div>
            <div v-if="attachments.length" class="attachment-list">
              <span v-for="item in attachments" :key="item.id">{{ item.filename }}<button @click="removeAttachment(item)" title="移除">×</button></span>
            </div>
            <footer>
              <button class="button" @click="current=null;events=[]">重新描述</button>
              <button class="button button--primary" @click="confirmContract"><Icon name="check"/>确认并开始分析</button>
            </footer>
          </section>

          <section v-else class="analysis-progress">
            <header><div><span class="eyebrow">真实执行过程</span><h2>{{ processing ? 'Agent 正在依据证据调整计划' : '执行记录' }}</h2></div>
              <div class="row-actions">
                <button v-if="['running','queued','waiting_job'].includes(current.execution_status)" class="button button--small" @click="control('pause')">暂停</button>
                <button v-if="['paused','waiting_input'].includes(current.execution_status)" class="button button--small" @click="control('resume')">继续</button>
                <button v-if="!['finished','failed','cancelled'].includes(current.execution_status)" class="button button--small" @click="control('cancel')">取消</button>
              </div>
            </header>
            <details :open="processing"><summary>{{ events.length }} 条持久化事件 · 完成后自动折叠</summary>
              <div class="process-list"><article v-for="event in events" :key="event.sequence"><i></i><div><b>{{ eventLabel(event) }}</b><small>#{{ event.sequence }} · {{ ctx.time(event.created_at) }}</small><pre v-if="['tool.failed','model.failed'].includes(event.type)">{{ JSON.stringify(event.payload, null, 2) }}</pre></div></article></div>
            </details>
            <p v-if="current.execution_status==='failed'" class="analysis-blocked">任务未完成：{{ current.stop_reason }}。系统未生成伪造成果。</p>
            <p v-if="current.quality_status && current.quality_status!=='passed' && current.execution_status==='finished'" class="analysis-blocked">验证门禁状态：{{ current.quality_status }}；当前只能回看部分状态，不能正式导出或发送。</p>
          </section>

          <section v-if="manifest" class="analysis-results">
            <nav class="result-tabs">
              <button :class="{active:activeTab==='summary'}" @click="activeTab='summary'">极简结论</button>
              <button :class="{active:activeTab==='dashboard'}" @click="activeTab='dashboard';loadDetails(true)">数据看板</button>
              <button :class="{active:activeTab==='report'}" @click="activeTab='report'">完整报告</button>
            </nav>
            <div v-if="activeTab==='summary'" class="result-pane">
              <div class="markdown" v-html="md(manifest.summary)"></div>
              <div class="kpi-grid"><article v-for="item in manifest.kpis" :key="item.id"><small>{{ item.label }}</small><b>{{ item.value ?? '不可用' }}</b><span v-if="item.unavailable_reason">{{ item.unavailable_reason }}</span></article></div>
              <details><summary>局限与验证范围</summary><ul><li v-for="item in manifest.limitations" :key="item">{{ item }}</li></ul></details>
              <details v-if="evidence"><summary>证据与 Claim</summary><pre>{{ JSON.stringify(evidence.claims, null, 2) }}</pre></details>
            </div>
            <div v-else-if="activeTab==='dashboard'" class="result-pane">
              <div class="four-chart-grid"><article v-for="chart in manifest.charts" :key="chart.id"><h3>{{ chart.title }}</h3><ChartView v-if="chart.available" :spec="chart"/><p v-else>{{ chart.unavailable_reason }}</p></article></div>
              <section class="detail-table"><header><h3>授权明细分页</h3><span>不会向浏览器加载全仓明细</span></header><DataTable :rows="details" :columns="detailColumns"/><button v-if="detailCursor!==null" class="button button--small" @click="loadDetails()">加载下一页</button></section>
            </div>
            <div v-else class="result-pane report-view">
              <h2>问题与口径</h2><pre>{{ JSON.stringify(manifest.report.problem_and_definitions, null, 2) }}</pre>
              <h2>数据结果</h2><div class="markdown" v-html="md(manifest.report.data_results)"></div>
              <h2>归因分析</h2><p v-for="item in manifest.report.attribution" :key="item.text"><b>{{ item.type }}</b> · {{ item.text }}</p>
              <h2>建议与局限</h2><ul><li v-for="item in manifest.report.limitations" :key="item">{{ item }}</li></ul>
            </div>
            <footer class="result-actions">
              <button class="button" @click="generateArtifacts"><Icon name="download"/>生成两份 Word 与四图 PNG</button>
              <button class="button button--primary" @click="openEmail"><Icon name="workflow"/>发送成果</button>
              <button class="button" @click="branch('followup')">继续追问</button>
              <button class="button" @click="branch('refresh')">刷新</button>
              <button class="button" @click="branch('reproduce')">复现</button>
              <a v-for="item in artifacts" :key="item.id" class="button button--small" :href="item.download_url">{{ item.filename }}</a>
            </footer>
          </section>
        </template>
      </div>

      <form class="composer" @submit.prevent="send">
        <div class="composer__input"><textarea ref="composer" v-model="prompt" :disabled="processing" @keydown="keydown" placeholder="描述分析问题；Enter 发送，Shift+Enter 换行"></textarea><button type="submit" :disabled="!canSend"><Icon name="play"/></button></div>
        <div class="composer__hint"><span>正式分析仅有一个智能模式；执行中不可重复提交</span><select v-model="selectedSkill"><option value="">自动选择方法</option><option v-for="item in state.skills.filter(skill=>!skill.status||skill.status==='published')" :key="item.id" :value="item.id">{{ item.display_name || item.name }}</option></select></div>
      </form>

      <Modal :open="emailOpen" title="发送已发布成果" @close="emailOpen=false"><div class="form-grid">
        <label class="span-2"><span>收件人（逗号分隔）</span><input v-model="email.recipients" type="text"></label>
        <label class="span-2"><span>主题</span><input v-model="email.subject"></label>
        <label class="span-2"><span>正文</span><textarea v-model="email.body"></textarea></label>
        <label><span>发送方式</span><select v-model="emailMode"><option value="eml">下载含附件 .eml</option><option value="smtp">SMTP 真实发送</option></select></label>
        <label v-if="emailMode==='smtp'"><span>SMTP 连接器 ID</span><input v-model="email.connector_id"></label>
        <fieldset class="span-2"><legend>附件（默认全选）</legend><label v-for="kind in ['summary_docx','report_docx','dashboard_png']" :key="kind"><input type="checkbox" :value="kind" v-model="email.kinds">{{ kind }}</label></fieldset>
        <p class="form-note span-2">.eml 含真实 MIME 附件；mailto 仅能作为不含附件的文本回退。</p>
      </div><template #footer><button class="button" @click="emailOpen=false">取消</button><button class="button button--primary" @click="deliverEmail">{{ emailMode==='smtp'?'发送':'生成 .eml' }}</button></template></Modal>
    </section>`,
};
