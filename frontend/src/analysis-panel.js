import { api, withWorkspace } from './api.js';
import { ChartView, DataTable, Icon, StatusPill, renderMarkdown } from './components.js';

const { nextTick } = Vue;
const TERMINAL = new Set(['finished', 'failed', 'cancelled']);
const ACTIVE = new Set(['queued', 'running', 'waiting_job', 'cancelling']);

export const AnalysisPanel = {
  components: { ChartView, DataTable, Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    prompt: '', executionMode: 'auto', current: null, events: [], eventCursor: 0, result: null, evidence: null,
    details: [], detailColumns: [], detailCursor: 0, activeTab: 'summary', pollingTimer: null,
    artifacts: [], attachments: [], sourcePickerOpen: false,
    clarificationAnswer: '', feedbackSent: '',
    contractForm: { objective: '', coverage: '', dimensions: '', deliverables: '' },
    artifactKinds: ['summary_docx', 'report_docx', 'dashboard_png'],
  }),
  computed: {
    state() { return this.ctx.state; },
    session() { return this.ctx.activeSession(); },
    selectedSources() { return this.ctx.selectedSources(); },
    availableSources() { return this.state.sources.filter(item => item.status === 'ready'); },
    demoMode() { return this.selectedSources.some(item => item.sample_seed?.id === 'instant_retail_city_pack'); },
    contract() { return this.current?.contract || null; },
    manifest() { return this.result?.manifest?.payload || null; },
    processing() { return ACTIVE.has(this.current?.execution_status); },
    clarification() {
      const event = [...this.events].reverse().find(item => item.type === 'ask_user');
      return event?.payload || null;
    },
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
  mounted() {
    if (this.state.pendingPrompt) { this.prompt = this.state.pendingPrompt; this.state.pendingPrompt = ''; }
    this.load();
  },
  beforeUnmount() { clearTimeout(this.pollingTimer); },
  methods: {
    md: renderMarkdown,
    split(value) {
      return String(value || '').split(/[，,\n]/).map(item => item.trim()).filter(Boolean);
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
      if (!this.selectedSources.length) {
        this.sourcePickerOpen = true;
        this.ctx.fail(new Error('请先选择至少一个数据源，再发起分析'));
        return;
      }
      this.prompt = '';
      try {
        const response = await api('/api/analyses', {
          method: 'POST',
          headers: { 'Idempotency-Key': 'analysis-' + crypto.randomUUID() },
          body: {
            session_id: this.session.id, objective,
            source_ids: this.session.source_ids || [],
            provider_id: this.session.provider_id || null,
            execution_mode: this.executionMode, auto_confirm: this.executionMode !== 'deep',
            confirm_required: this.executionMode === 'deep',
          },
        });
        await this.setRun(response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    async toggleSource(source) {
      if (!this.session) return;
      const ids = new Set(this.session.source_ids || []);
      ids.has(source.id) ? ids.delete(source.id) : ids.add(source.id);
      try {
        const response = await api(`/api/sessions/${this.session.id}`, {
          method: 'PATCH', body: { source_ids: [...ids] },
        });
        Object.assign(this.session, response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    retryWithSources() {
      this.current = null; this.events = []; this.result = null; this.artifacts = [];
      this.sourcePickerOpen = true;
    },
    retryFailed() {
      const objective = this.contract?.payload?.objective || '';
      this.current = null; this.events = []; this.result = null; this.artifacts = [];
      this.prompt = objective;
      nextTick(() => this.$refs.composer?.focus());
    },
    stopReasonLabel(reason) {
      const labels = {
        model_budget_exceeded: '本次模型调用预算已耗尽',
        daily_model_budget_exceeded: '今日模型调用额度已耗尽',
        model_unavailable: '模型服务暂时不可用',
        run_time_budget_exceeded: '任务执行时间超过上限',
        repeated_tool_failures: '数据工具连续执行失败',
        publication_gate_blocked: '分析结果未通过发布校验',
      };
      return labels[reason] || reason || '未知原因';
    },
    gateReasons() {
      if (!this.current?.source_scope?.length) return ['本次分析未带入数据源，Agent 无法执行数据查询'];
      const partial = [...this.events].reverse().find(item => item.type === 'analysis.partial');
      return (partial?.payload?.validation?.blocking_issues || []).map(item => item.reason).filter(Boolean).slice(0, 3);
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
    async answerClarification(answer = '') {
      const value = String(answer || this.clarificationAnswer).trim();
      if (!value || !this.current) return;
      try {
        const response = await api('/api/analyses/' + this.current.id + '/clarifications', { method: 'POST', body: { answer: value } });
        this.current = response.item; this.clarificationAnswer = ''; this.schedulePoll();
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
          method: 'POST', body: { kinds: this.artifactKinds },
        });
        this.artifacts = response.items || [];
        this.ctx.toast('两个 Word 与四图 PNG 已绑定当前发布版本', '成果已生成');
      } catch (error) { this.ctx.fail(error); }
    },
    async feedback(rating) {
      if (!this.current) return;
      const category = rating === 'incorrect' ? (window.prompt('主要问题是什么？例如：口径错误、数据错误、理解错误', '口径错误') || '') : '';
      if (rating === 'incorrect' && !category) return;
      try {
        await api('/api/feedback', { method: 'POST', body: { workspace_id: this.state.workspaceId, run_id: this.current.id, rating, category } });
        this.feedbackSent = rating; this.ctx.toast('反馈将进入管理员的质量运营闭环', '感谢反馈');
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
      <div ref="feed" class="chat-feed" :class="{'chat-feed--empty':!current}">
        <div v-if="!current" class="welcome-block">
          <section class="agent-welcome">
            <div class="welcome-glyph"><Icon name="brain" :size="22"/></div>
            <div class="agent-welcome__copy"><span class="welcome-kicker">智能分析</span><h2>今天想了解什么？</h2><p>用业务语言描述问题，Agent 会完成查询、分析、验证并生成可信结论。</p></div>
          </section>
          <div class="home-readiness">
            <div class="source-picker-wrap">
              <button class="empty-data-action" @click="sourcePickerOpen=!sourcePickerOpen"><Icon name="database"/><span><b>{{ selectedSources.length ? '已选择 '+selectedSources.length+' 个数据源' : '选择分析数据' }}</b><small>{{ selectedSources.length ? selectedSources.map(item=>item.name).join('、') : '发起分析前需要先确定数据范围' }}</small></span><Icon name="chevron"/></button>
              <section v-if="sourcePickerOpen" class="source-picker">
                <header><b>本次分析的数据范围</b><button @click="ctx.go('sources')">管理数据源</button></header>
                <button v-for="source in availableSources" :key="source.id" :class="{selected:session?.source_ids?.includes(source.id)}" @click="toggleSource(source)"><span class="source-picker-check"><Icon v-if="session?.source_ids?.includes(source.id)" name="check" :size="13"/></span><span><b>{{ source.name }}</b><small>{{ source.kind==='database' ? '数据库' : '文件' }} · {{ source.tables?.length || 0 }} 张表</small></span></button>
                <p v-if="!availableSources.length">还没有可用数据源，请先上传文件或建立数据库连接。</p>
              </section>
            </div>
            <span class="trust-note"><Icon name="check" :size="14"/>自动核对指标口径、权限与结论证据</span>
          </div>
          <section class="suggestion-section">
            <header><div><b>{{ demoMode ? '演示问题' : '试试这样问' }}</b></div></header>
            <div v-if="demoMode" class="prompt-grid">
              <button @click="usePrompt('活跃合作商家总数是多少，各省份如何分布？')"><span class="prompt-icon"><Icon name="table"/></span><span><b>供给规模</b><small>总量与省份分布</small></span></button>
              <button @click="usePrompt('哪些城市的商家供给存在明显差异？')"><span class="prompt-icon"><Icon name="warning"/></span><span><b>城市差异</b><small>识别结构异常</small></span></button>
              <button @click="usePrompt('结合盈利状态、补贴和履约成本，分析需要优先关注的城市。')"><span class="prompt-icon"><Icon name="chart"/></span><span><b>经营诊断</b><small>定位重点城市</small></span></button>
              <button @click="usePrompt('生成一份城市经营简报，包含结论、证据、风险和建议。')"><span class="prompt-icon"><Icon name="workflow"/></span><span><b>经营简报</b><small>结论、证据与建议</small></span></button>
            </div>
            <div v-else class="prompt-grid">
              <button @click="usePrompt('概览已选数据，指出最重要的三个发现和数据质量风险')"><span class="prompt-icon"><Icon name="table"/></span><span><b>经营概览</b><small>关键指标与结构</small></span></button>
              <button @click="usePrompt('识别关键指标的异常变化，并定位贡献最大的群组')"><span class="prompt-icon"><Icon name="warning"/></span><span><b>异常归因</b><small>变化与贡献度</small></span></button>
              <button @click="usePrompt('分析核心数值的时间趋势，并说明可验证的变化')"><span class="prompt-icon"><Icon name="chart"/></span><span><b>趋势洞察</b><small>走势与关键拐点</small></span></button>
              <button @click="usePrompt('生成一份适合经营会的分析摘要，包含结论、证据和建议')"><span class="prompt-icon"><Icon name="workflow"/></span><span><b>经营简报</b><small>结论与行动建议</small></span></button>
            </div>
          </section>
        </div>

        <template v-else>
          <article class="message message--user">
            <div class="message__meta"><span>你</span><time>{{ ctx.time(current.created_at) }}</time></div>
            <div class="message__body">{{ contract?.payload?.objective }}</div>
          </article>

          <section v-if="contract && !contract.confirmed_at" class="analysis-contract">
            <header><div><small class="section-label">分析范围确认</small><h2>请核对复杂任务的统计范围</h2></div><StatusPill status="draft" label="待确认"/></header>
            <div class="contract-grid">
              <label><span>业务分析目标</span><textarea v-model="contractForm.objective"></textarea></label>
              <label><span>统计覆盖范围</span><textarea v-model="contractForm.coverage"></textarea></label>
              <label><span>查看维度</span><textarea v-model="contractForm.dimensions" placeholder="用逗号或换行分隔"></textarea></label>
              <label><span>需要的结果</span><textarea v-model="contractForm.deliverables" placeholder="结论、图表、报告"></textarea></label>
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
              <button class="button button--primary" @click="confirmContract"><Icon name="check"/>确认范围并分析</button>
            </footer>
          </section>

          <section v-else class="analysis-progress">
            <header><div><small class="section-label">执行进度</small><h2>{{ processing ? '正在查询、分析并核对数据' : '执行记录' }}</h2></div>
              <div class="row-actions">
                <button v-if="['running','queued','waiting_job'].includes(current.execution_status)" class="button button--small" @click="control('pause')">暂停</button>
                <button v-if="current.execution_status==='paused'" class="button button--small" @click="control('resume')">继续</button>
                <button v-if="!['finished','failed','cancelled'].includes(current.execution_status)" class="button button--small" @click="control('cancel')">取消</button>
              </div>
            </header>
            <details :open="processing"><summary>{{ events.length }} 条持久化事件 · 完成后自动折叠</summary>
              <div class="process-list"><article v-for="event in events" :key="event.sequence"><i></i><div><b>{{ eventLabel(event) }}</b><small>#{{ event.sequence }} · {{ ctx.time(event.created_at) }}</small><pre v-if="['tool.failed','model.failed'].includes(event.type)">{{ JSON.stringify(event.payload, null, 2) }}</pre></div></article></div>
            </details>
            <div v-if="current.execution_status==='waiting_input' && current.stop_reason==='clarification_required'" class="clarification-card"><h3>{{ clarification?.question || '还需要补充一个条件' }}</h3><div v-if="clarification?.options?.length" class="clarification-options"><button v-for="item in clarification.options" :key="item" class="button button--small" @click="answerClarification(item)">{{ item }}</button></div><div class="clarification-answer"><input v-model="clarificationAnswer" @keyup.enter="answerClarification()" placeholder="输入补充信息"><button class="button button--primary" @click="answerClarification()">继续分析</button></div></div>
            <div v-if="current.execution_status==='failed'" class="analysis-blocked">
              <b>任务未完成：{{ stopReasonLabel(current.stop_reason) }}</b>
              <span>系统已保留执行记录，但不会生成未经验证的成果。</span>
              <button class="button button--small" @click="retryFailed">带入原问题重新分析</button>
            </div>
            <div v-if="current.quality_status && current.quality_status!=='passed' && current.execution_status==='finished'" class="analysis-blocked">
              <b>{{ !current.source_scope?.length ? '本次分析没有带入数据' : '分析结果未通过发布校验' }}</b>
              <span v-for="reason in gateReasons()" :key="reason">{{ reason }}</span>
              <small>系统已阻止未经证据验证的结果导出或发送。</small>
              <button v-if="!current.source_scope?.length" class="button button--small" @click="retryWithSources">选择数据并重新分析</button>
            </div>
          </section>

          <section v-if="manifest" class="analysis-results">
            <nav class="result-tabs">
              <button :class="{active:activeTab==='summary'}" @click="activeTab='summary'">分析结论</button>
              <button :class="{active:activeTab==='dashboard'}" @click="activeTab='dashboard';loadDetails(true)">指标与图表</button>
              <button :class="{active:activeTab==='report'}" @click="activeTab='report'">详细报告</button>
            </nav>
            <div v-if="activeTab==='summary'" class="result-pane">
              <div class="markdown" v-html="md(manifest.summary)"></div>
              <div class="kpi-grid"><article v-for="item in manifest.kpis" :key="item.id"><small>{{ item.label }}</small><b>{{ item.value ?? '不可用' }}</b><span v-if="item.unavailable_reason">{{ item.unavailable_reason }}</span></article></div>
              <details><summary>局限与验证范围</summary><ul><li v-for="item in manifest.limitations" :key="item">{{ item }}</li></ul></details>
              <details v-if="evidence" class="evidence-drawer"><summary>查看结论依据（{{ evidence.claims?.length || 0 }} 条）</summary><div class="claim-list"><article v-for="claim in evidence.claims" :key="claim.id"><header><StatusPill :status="claim.payload?.status==='validated'?'completed':'draft'" :label="claim.payload?.numeric_replay==='PASS'?'已复算':'待核对'"/><span v-for="ref in claim.payload?.definition_refs || []" :key="ref">{{ ref }}</span></header><p>{{ claim.payload?.text }}</p><small>{{ claim.payload?.evidence_cells?.length || 0 }} 个数据单元格可回放</small></article></div></details>
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
              <button class="button button--primary" @click="branch('followup')"><Icon name="chat"/>继续追问</button>
              <button class="button" @click="generateArtifacts"><Icon name="download"/>导出成果</button>
              <a v-for="item in artifacts" :key="item.id" class="button button--small" :href="item.download_url">{{ item.filename }}</a>
            </footer>
            <div class="result-feedback"><span>这个结果对你有帮助吗？</span><button :class="{active:feedbackSent==='correct'}" @click="feedback('correct')">准确</button><button :class="{active:feedbackSent==='partially_correct'}" @click="feedback('partially_correct')">部分准确</button><button :class="{active:feedbackSent==='incorrect'}" @click="feedback('incorrect')">需要纠正</button></div>
          </section>
        </template>
      </div>

      <form class="composer" @submit.prevent="send">
        <div class="composer__input"><textarea ref="composer" v-model="prompt" :disabled="processing" @keydown="keydown" placeholder="描述分析问题；Enter 发送，Shift+Enter 换行"></textarea><button type="submit" :disabled="!canSend" aria-label="发起分析"><Icon name="play"/></button></div>
        <div class="composer__hint"><span><Icon name="check" :size="13"/>结论附带指标口径与可回放证据</span><label>分析模式<select v-model="executionMode"><option value="auto">智能判断</option><option value="quick">快速问数</option><option value="deep">深度分析</option></select></label></div>
      </form>
    </section>`,
};
