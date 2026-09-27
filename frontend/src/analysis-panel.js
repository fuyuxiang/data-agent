import { api, withWorkspace } from './api.js';
import { ChartView, DataTable, Icon, StatusPill, renderMarkdown } from './components.js';

const { nextTick } = Vue;
const TERMINAL = new Set(['finished', 'failed', 'cancelled']);
const ACTIVE = new Set(['queued', 'running', 'waiting_job', 'cancelling']);

function idempotencyKey(prefix = 'analysis') {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return `${prefix}-${cryptoApi.randomUUID()}`;
  }
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${prefix}-${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export const AnalysisPanel = {
  components: { ChartView, DataTable, Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    prompt: '', executionMode: 'auto', current: null, runs: [], events: [], eventCursor: 0, result: null, evidence: null,
    activeEvidence: null, evidenceLoading: false, evidenceError: '', showEventDetails: false,
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
    formatResultValue(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return '不可用';
      if (typeof value === 'number') {
        return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value);
      }
      return String(value);
    },
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
      this.current = null; this.runs = []; this.events = []; this.result = null; this.artifacts = [];
      if (!this.session) return;
      try {
        const path = '/api/analyses?session_id=' + encodeURIComponent(this.session.id) + '&limit=50';
        const response = await api(withWorkspace(path, this.state.workspaceId));
        this.runs = response.items || [];
        if (this.runs.length) await this.setRun(this.runs[0]);
      } catch (error) { this.ctx.fail(error); }
    },
    async setRun(run) {
      clearTimeout(this.pollingTimer);
      this.current = run; this.eventCursor = 0; this.events = []; this.result = null;
      this.details = []; this.artifacts = []; this.evidence = null; this.activeEvidence = null; this.evidenceError = ''; this.activeTab = 'summary';
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
      const selectedRunId = this.current.id;
      try {
        const base = '/api/analyses/' + selectedRunId;
        const [run, eventPage, attachments] = await Promise.all([
          api(withWorkspace(base, this.state.workspaceId)),
          api(withWorkspace(base + '/events?after=' + this.eventCursor + '&limit=500', this.state.workspaceId)),
          api(withWorkspace(base + '/attachments', this.state.workspaceId)),
        ]);
        if (this.current?.id !== selectedRunId) return;
        this.current = run.item; this.attachments = attachments.items || [];
        const historyIndex = this.runs.findIndex(item => item.id === selectedRunId);
        if (historyIndex >= 0) this.runs.splice(historyIndex, 1, run.item);
        for (const event of eventPage.items || []) {
          if (!this.events.some(item => item.sequence === event.sequence)) this.events.push(event);
        }
        this.eventCursor = eventPage.next_cursor || this.eventCursor;
        if (this.current.execution_status === 'finished') {
          const result = await api(withWorkspace(base + '/results', this.state.workspaceId));
          if (this.current?.id !== selectedRunId) return;
          this.result = result;
          this.artifacts = this.result.artifacts || [];
          if (this.result.status === 'published') {
            const evidence = await api(withWorkspace(base + '/evidence', this.state.workspaceId));
            if (this.current?.id !== selectedRunId) return;
            this.evidence = evidence;
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
          headers: { 'Idempotency-Key': idempotencyKey() },
          body: {
            session_id: this.session.id, objective,
            source_ids: this.session.source_ids || [],
            provider_id: this.session.provider_id || null,
            execution_mode: this.executionMode, auto_confirm: this.executionMode !== 'deep',
            confirm_required: this.executionMode === 'deep',
          },
        });
        this.runs.unshift(response.item);
        await this.setRun(response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    async toggleSource(source) {
      if (!this.session) return;
      const knownSourceIds = new Set(this.state.sources.map(item => String(item.id)));
      const ids = new Set((this.session.source_ids || []).map(String).filter(id => knownSourceIds.has(id)));
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
    gateIssues() {
      if (!this.current?.source_scope?.length) return [{rule_id:'source_scope',reason:'本次分析未带入数据源，无法核对结果',action:'选择数据'}];
      const partial = [...this.events].reverse().find(item => item.type === 'analysis.partial');
      const issues = partial?.payload?.validation?.blocking_issues || [];
      if (!issues.length) return [{rule_id:'retry',reason:'本次结果未通过质量校验，请重新分析并检查执行记录',action:'重新分析补证'}];
      return issues.map(item => ({
        rule_id: item.rule_id,
        reason: item.reason || '校验未通过',
        action: ['current_authorization'].includes(item.rule_id) ? '查看数据权限'
          : ['numeric_claim_replay','independent_validation','tool_evidence','tool_failures','result_completeness','claim_provenance','contract_confirmed'].includes(item.rule_id) ? '重新分析补证'
          : '查看执行记录',
      }));
    },
    resolveGate(issue) {
      if (issue.rule_id === 'source_scope') return this.retryWithSources();
      if (issue.rule_id === 'current_authorization') return this.ctx.go('sources');
      if (issue.action === '重新分析补证') return this.retryFailed();
      this.showEventDetails = true;
    },
    claimSegments(claim) {
      const payload = claim.payload || {};
      const value = String(payload.text || '');
      const cells = payload.evidence_cells || [];
      let cursor = 0;
      const used = new Set();
      const segments = [];
      for (const number of payload.numbers || []) {
        const start = Number(number.start), end = Number(number.end);
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end > value.length) continue;
        if (start > cursor) segments.push({ text: value.slice(cursor, start) });
        const cellIndex = cells.findIndex((cell, index) => !used.has(index) && cell.number === number.text);
        if (cellIndex >= 0) used.add(cellIndex);
        segments.push({ text: value.slice(start, end), cellIndex });
        cursor = end;
      }
      if (cursor < value.length) segments.push({ text: value.slice(cursor) });
      return segments;
    },
    summaryWithEvidence() {
      const answer = String(this.manifest?.summary || '');
      if (!this.evidence?.claims?.length) return this.md(answer);
      const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
      const replacements = [];
      let seek = 0;
      for (const claim of this.evidence.claims) {
        const payload = claim.payload || {};
        const text = String(payload.text || '');
        const offset = answer.indexOf(text, seek);
        if (offset < 0) continue;
        seek = offset + text.length;
        const cells = payload.evidence_cells || [];
        const used = new Set();
        for (const number of payload.numbers || []) {
          const start = Number(number.start), end = Number(number.end);
          if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > text.length) continue;
          const cellIndex = cells.findIndex((cell,index) => !used.has(index) && cell.number === number.text);
          if (cellIndex < 0) continue;
          used.add(cellIndex);
          const raw = text.slice(start,end);
          const button = `<button type="button" class="evidence-number" data-evidence-claim="${escape(claim.id)}" data-cell-index="${cellIndex}" aria-label="回放数字 ${escape(raw)} 的数据证据">${escape(raw)}<sup>${cellIndex+1}</sup></button>`;
          replacements.push({start:offset+start,end:offset+end,button});
        }
      }
      let marked = answer;
      for (const item of replacements.sort((a,b) => b.start-a.start)) marked = marked.slice(0,item.start)+item.button+marked.slice(item.end);
      return this.md(marked);
    },
    onSummaryClick(event) {
      const button = event.target.closest('button[data-evidence-claim]');
      if (!button) return;
      const claim = this.evidence?.claims?.find(item => item.id === button.dataset.evidenceClaim);
      if (claim) this.replayCell(claim, Number(button.dataset.cellIndex));
    },
    claimStatus(claim) {
      const payload = claim.payload || {};
      if (payload.numeric_replay === 'FAIL') return '待核对';
      if ((payload.evidence_cells || []).length) return '数字已核对 · 解释需判断';
      if ((payload.definition_numbers || []).length) return '口径常量已核对 · 解释需判断';
      return '解释需人工判断';
    },
    async replayCell(claim, cellIndex) {
      if (!this.current) return;
      const runId = this.current.id;
      this.evidenceLoading = true; this.evidenceError = ''; this.activeEvidence = null;
      try {
        const response = await api(withWorkspace(`/api/analyses/${runId}/evidence/claims/${claim.id}/cells/${cellIndex}`, this.state.workspaceId));
        if (this.current?.id === runId) this.activeEvidence = response.item;
      } catch (error) { this.evidenceError = error?.message || '证据单元格暂时无法回放'; }
      finally { this.evidenceLoading = false; }
    },
    progressStages() {
      const has = (type, tool) => this.events.some(event => event.type === type && (!tool || event.payload?.tool_id === tool));
      const published = this.current?.quality_status === 'passed' && this.result?.status === 'published';
      const stages = [
        { label:'确认分析范围', done:!!this.contract?.confirmed_at },
        { label:'查询授权数据', done:published || has('action.succeeded','query_data') },
        { label:'核对结果与口径', done:published || has('action.succeeded','validate_result') },
        { label:'生成可核验结论', done:published || has('analysis.published') },
      ];
      const next = stages.findIndex(stage => !stage.done);
      return stages.map((stage,index) => ({...stage,status:stage.done?'completed':index===next&&this.processing?'running':index===next&&this.current?.execution_status==='finished'?'failed':'pending'}));
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
      const entry = rating === 'incorrect' ? await this.ctx.askForm({title:'反馈分析问题',fields:[{key:'category',label:'主要问题',placeholder:'例如：口径错误、数据错误或理解错误',required:true}],submitLabel:'提交反馈'}) : null;
      if (rating === 'incorrect' && !entry) return;
      const category = entry?.category || '';
      try {
        await api('/api/feedback', { method: 'POST', body: { workspace_id: this.state.workspaceId, run_id: this.current.id, rating, category } });
        this.feedbackSent = rating; this.ctx.toast('反馈将进入管理员的质量运营闭环', '感谢反馈');
      } catch (error) { this.ctx.fail(error); }
    },
    async branch(mode) {
      const labels = { followup: '继续追问', refresh: '刷新数据', reproduce: '精确复现', reanalyze: '重新分析' };
      const entry = await this.ctx.askForm({title:labels[mode],fields:[{key:'objective',label:'分析目标',value:this.contract?.payload?.objective || '',required:true,multiline:true}],submitLabel:'开始分析'});
      if (!entry) return;
      const promptValue = entry.objective;
      try {
        const response = await api('/api/analyses/' + this.current.id + '/branch', {
          method: 'POST', body: { mode, prompt: promptValue },
        });
        this.runs.unshift(response.item);
        await this.setRun(response.item);
      } catch (error) { this.ctx.fail(error); }
    },
    eventLabel(event) {
      const names = {
        'analysis.created': '已接收分析问题', 'contract.confirmed': '分析范围已确认',
        'model.decision': '正在确定分析步骤', 'plan.revised': '分析步骤已更新',
        'action.submitted': '正在执行分析步骤',
        'action.succeeded': '分析步骤已完成', 'action.failed': '分析步骤未完成',
        'analysis.published': '结论通过校验', 'analysis.partial': '结论未通过发布校验',
        'analysis.answer_repaired': '已移除缺少证据的数字',
        'analysis.status': '分析状态已更新', 'attachments.added': '已添加参考附件',
        'attachment.removed': '已移除参考附件',
      };
      if (event.type === 'action.succeeded') {
        return {query_data:'数据查询已完成',validate_result:'数据结果已核对'}[event.payload?.tool_id] || names[event.type];
      }
      return names[event.type] || '分析记录已更新';
    },
    eventStatus(event) {
      if (event.type === 'analysis.published') return 'completed';
      if (event.type === 'analysis.partial') return 'failed';
      if (event.type === 'action.failed') return 'failed';
      if (event.type === 'analysis.status') {
        const status = event.payload?.status;
        if (status === 'finished') return 'completed';
        if (status === 'failed') return 'failed';
        if (status === 'running' || status === 'queued' || status === 'waiting_job') return 'running';
      }
      if (['action.submitted', 'model.decision'].includes(event.type)) return 'running';
      return 'completed';
    },
    timelineRows() {
      const visible = new Set(['analysis.created','contract.confirmed','model.decision','action.submitted','action.succeeded','action.failed','analysis.published','analysis.partial','analysis.answer_repaired','analysis.status','attachments.added','attachment.removed']);
      return this.events.filter(event => visible.has(event.type)).slice(-12).map(event => ({
        id: event.sequence,
        label: this.eventLabel(event),
        time: this.ctx.time(event.created_at),
        status: this.eventStatus(event),
      }));
    },
  },
  template: `
    <section class="chat-surface">
      <div ref="feed" class="chat-feed" :class="{'chat-feed--empty':!current}">
        <div v-if="!current" class="welcome-block">
          <section class="welcome-hero">
            <h2>今天想了解什么？</h2>
            <p>用业务语言描述问题，我会拆解目标、查询数据、核对证据，并生成可被审计的结论。</p>
          </section>
          <div class="home-readiness">
            <div class="source-picker-wrap">
              <button class="empty-data-action" @click="sourcePickerOpen=!sourcePickerOpen"><Icon name="database"/><span><b>{{ selectedSources.length ? '已选择 '+selectedSources.length+' 个数据源' : '选择分析数据' }}</b><small>{{ selectedSources.length ? selectedSources.map(item=>item.name).join('、') : '发起分析前需要先确定数据范围' }}</small></span><Icon name="chevron"/></button>
              <section v-if="sourcePickerOpen" class="source-picker">
                <header><b>本次分析的数据范围</b><button v-if="['owner','editor'].includes(state.workspaceRole)" @click="ctx.go('sources')">管理数据源</button></header>
                <button v-for="source in availableSources" :key="source.id" :class="{selected:session?.source_ids?.includes(source.id)}" @click="toggleSource(source)"><span class="source-picker-check"><Icon v-if="session?.source_ids?.includes(source.id)" name="check" :size="13"/></span><span><b>{{ source.name }}</b><small>{{ source.kind==='database' ? '数据库' : '文件' }} · {{ source.tables?.length || 0 }} 张表</small></span></button>
                <p v-if="!availableSources.length">还没有可用数据源，请联系工作空间管理员接入数据。</p>
              </section>
            </div>
            <span class="trust-note"><Icon name="check" :size="14"/>核对数据权限、指标引用与可回放数值</span>
          </div>
          <section class="suggestion-section">
            <header><div><b>{{ demoMode ? '演示问题' : '试试这样问' }}</b></div></header>
            <div v-if="demoMode" class="prompt-grid">
              <button @click="usePrompt('活跃合作商家总数是多少，各省份如何分布？')"><span class="prompt-icon"><Icon name="table"/></span><span><b>供给规模</b><small>总量与省份分布</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('哪些城市的商家供给存在明显差异？')"><span class="prompt-icon"><Icon name="warning"/></span><span><b>城市差异</b><small>识别结构异常</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('结合盈利状态、补贴和履约成本，分析需要优先关注的城市。')"><span class="prompt-icon"><Icon name="chart"/></span><span><b>经营诊断</b><small>定位重点城市</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('生成一份城市经营简报，包含结论、证据、风险和建议。')"><span class="prompt-icon"><Icon name="workflow"/></span><span><b>经营简报</b><small>结论、证据与建议</small></span><Icon name="chevron"/></button>
            </div>
            <div v-else class="prompt-grid">
              <button @click="usePrompt('概览已选数据，指出最重要的三个发现和数据质量风险')"><span class="prompt-icon"><Icon name="table"/></span><span><b>经营概览</b><small>关键指标与结构</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('识别关键指标的异常变化，并定位贡献最大的群组')"><span class="prompt-icon"><Icon name="warning"/></span><span><b>异常归因</b><small>变化与贡献度</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('分析核心数值的时间趋势，并说明可验证的变化')"><span class="prompt-icon"><Icon name="chart"/></span><span><b>趋势洞察</b><small>走势与关键拐点</small></span><Icon name="chevron"/></button>
              <button @click="usePrompt('生成一份适合经营会的分析摘要，包含结论、证据和建议')"><span class="prompt-icon"><Icon name="workflow"/></span><span><b>经营简报</b><small>结论与行动建议</small></span><Icon name="chevron"/></button>
            </div>
          </section>
        </div>

        <template v-else>
          <nav v-if="runs.length>1" class="analysis-run-history" aria-label="本会话分析历史">
            <b>本会话分析</b>
            <button v-for="run in runs" :key="run.id" type="button" :class="{active:current.id===run.id}" @click="setRun(run)">{{ run.contract?.payload?.objective || '未命名分析' }}<small>{{ ctx.time(run.created_at) }}</small></button>
          </nav>
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
            <ol class="analysis-stage-list" aria-label="分析进度">
              <li v-for="stage in progressStages()" :key="stage.label" :data-status="stage.status"><span class="analysis-stage-list__mark"><Icon v-if="stage.status==='completed'" name="check" :size="13"/><Icon v-else-if="stage.status==='failed'" name="warning" :size="13"/></span><b>{{ stage.label }}</b><small>{{ {completed:'已完成',running:'进行中',failed:'未完成',pending:'待执行'}[stage.status] }}</small></li>
            </ol>
            <details :open="showEventDetails" @toggle="showEventDetails=$event.target.open" class="event-details"><summary>查看详细执行记录（{{ events.length }} 条）</summary>
              <ul class="timeline-list">
                <li v-for="row in timelineRows()" :key="row.id" class="timeline-item" :data-status="row.status">
                  <span class="timeline-item__icon"><Icon v-if="row.status==='completed'" name="check" :size="14"/><Icon v-else-if="row.status==='failed'" name="warning" :size="14"/><Icon v-else name="bolt" :size="14"/></span>
                  <div class="timeline-item__main"><b>{{ row.label }}</b><small>{{ row.time }}</small></div>
                  <span class="timeline-item__time">{{ row.status === 'completed' ? '已完成' : row.status === 'failed' ? '需关注' : '进行中' }}</span>
                </li>
              </ul>
            </details>
            <div v-if="current.execution_status==='waiting_input' && current.stop_reason==='clarification_required'" class="clarification-card"><h3>{{ clarification?.question || '还需要补充一个条件' }}</h3><div v-if="clarification?.options?.length" class="clarification-options"><button v-for="item in clarification.options" :key="item" class="button button--small" @click="answerClarification(item)">{{ item }}</button></div><div class="clarification-answer"><input v-model="clarificationAnswer" @keyup.enter="answerClarification()" placeholder="输入补充信息"><button class="button button--primary" @click="answerClarification()">继续分析</button></div></div>
            <div v-if="current.execution_status==='failed'" class="analysis-blocked">
              <b>任务未完成：{{ stopReasonLabel(current.stop_reason) }}</b>
              <span>系统已保留执行记录，但不会生成未经验证的成果。</span>
              <button class="button button--small" @click="retryFailed">带入原问题重新分析</button>
            </div>
            <div v-if="current.quality_status && current.quality_status!=='passed' && current.execution_status==='finished'" class="analysis-blocked">
              <b>{{ !current.source_scope?.length ? '本次分析没有带入数据' : '分析结果未通过发布校验' }}</b>
              <div v-for="(issue,index) in gateIssues()" :key="index" class="gate-issue"><span>{{ issue.reason }}</span><button type="button" class="button button--small" @click="resolveGate(issue)">{{ issue.action }}</button></div>
              <small>系统已阻止未经证据验证的结果导出或发送。</small>
            </div>
          </section>

          <section v-if="manifest" class="analysis-results">
            <nav class="result-tabs">
              <button :class="{active:activeTab==='summary'}" @click="activeTab='summary'">分析结论</button>
              <button :class="{active:activeTab==='dashboard'}" @click="activeTab='dashboard';loadDetails(true)">指标与图表</button>
            </nav>
            <div v-if="activeTab==='summary'" class="result-pane">
              <div v-if="manifest.kpis?.length" class="kpi-grid summary-kpi-grid"><article v-for="item in manifest.kpis" :key="item.id"><small>{{ item.label }}</small><b>{{ formatResultValue(item.value) }}</b><span v-if="item.unavailable_reason">{{ item.unavailable_reason }}</span></article></div>
              <div class="markdown analysis-summary-markdown" v-html="summaryWithEvidence()" @click="onSummaryClick"></div>
              <section v-if="evidence?.claims?.length" class="evidence-section" aria-label="结论依据">
                <header><div><span class="section-label">结论依据</span><h2>逐条核对分析结论</h2></div><small>点击青色数字查看原始结果行；业务解释仍需人工判断</small></header>
                <div class="evidence-layout" :class="{ 'evidence-layout--open':activeEvidence || evidenceLoading || evidenceError }">
                  <div class="claim-list">
                    <article v-for="claim in evidence.claims" :key="claim.id" class="claim-card" :data-verification="claim.payload?.numeric_replay">
                      <div class="claim-card__text"><template v-for="(segment,index) in claimSegments(claim)" :key="index"><button v-if="segment.cellIndex>=0" type="button" class="evidence-number" :aria-label="'回放数字 '+segment.text+' 的数据证据'" @click="replayCell(claim,segment.cellIndex)">{{ segment.text }}<sup>{{ segment.cellIndex+1 }}</sup></button><span v-else>{{ segment.text }}</span></template></div>
                      <footer><StatusPill :status="claim.payload?.numeric_replay==='FAIL'?'failed':claim.payload?.evidence_cells?.length?'completed':'waiting_approval'" :label="claimStatus(claim)"/><span v-for="ref in claim.payload?.definition_refs || []" :key="ref" class="metric-ref">{{ ref }}</span><small v-if="claim.payload?.evidence_cells?.length">{{ claim.payload.evidence_cells.length }} 个数据单元格</small></footer>
                    </article>
                  </div>
                  <aside v-if="activeEvidence || evidenceLoading || evidenceError" class="evidence-inspector" aria-live="polite">
                    <header><h3>数据证据回放</h3><button type="button" class="icon-button" aria-label="关闭证据详情" @click="activeEvidence=null;evidenceError=''">×</button></header>
                    <p v-if="evidenceLoading">正在读取已记录的数据行…</p>
                    <p v-else-if="evidenceError" role="alert">{{ evidenceError }}</p>
                    <template v-else-if="activeEvidence"><p class="evidence-inspector__claim">{{ activeEvidence.claim }}</p><p><b>核对值：</b>{{ activeEvidence.number }} · {{ activeEvidence.column }} = {{ activeEvidence.value }}</p><p v-if="activeEvidence.metric?.metric_id"><b>指标版本：</b>{{ activeEvidence.metric.metric_id }}@{{ activeEvidence.metric.metric_version }}</p><p><b>结果行：</b>{{ activeEvidence.row_index+1 }}</p><dl><template v-for="(value,key) in activeEvidence.row" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></dl></template>
                  </aside>
                </div>
              </section>
              <details><summary>分析范围与验证局限</summary><div class="analysis-scope-details"><p><b>目标：</b>{{ manifest.contract?.objective || '未指定' }}</p><p><b>覆盖范围：</b>{{ manifest.contract?.coverage || '未指定' }}</p><p><b>维度：</b>{{ manifest.contract?.dimensions?.join('、') || '未指定' }}</p></div><ul><li v-for="item in manifest.limitations" :key="item">{{ item }}</li></ul></details>
            </div>
            <div v-else-if="activeTab==='dashboard'" class="result-pane">
              <div class="four-chart-grid"><article v-for="chart in manifest.charts" :key="chart.id"><h3>{{ chart.title }}</h3><ChartView v-if="chart.available" :spec="chart"/><p v-else>{{ chart.unavailable_reason }}</p></article></div>
              <section class="detail-table"><header><h3>授权明细分页</h3><span>不会向浏览器加载全仓明细</span></header><DataTable :rows="details" :columns="detailColumns"/><button v-if="detailCursor!==null" class="button button--small" @click="loadDetails()">加载下一页</button></section>
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
        <section v-if="sourcePickerOpen && current" class="source-picker composer-source-picker">
          <header><b>下次提问的数据范围</b><button v-if="['owner','editor'].includes(state.workspaceRole)" type="button" @click="ctx.go('sources')">管理数据源</button></header>
          <button v-for="source in availableSources" :key="source.id" type="button" :class="{selected:session?.source_ids?.includes(source.id)}" @click="toggleSource(source)"><span class="source-picker-check"><Icon v-if="session?.source_ids?.includes(source.id)" name="check" :size="13"/></span><span><b>{{ source.name }}</b><small>{{ source.kind==='database' ? '数据库' : '文件' }} · {{ source.tables?.length || 0 }} 张表</small></span></button>
          <p v-if="!availableSources.length">当前没有可用数据源，请联系工作空间管理员。</p>
        </section>
        <textarea ref="composer" v-model="prompt" :disabled="processing" @keydown="keydown" placeholder="描述分析问题；Enter 发送，Shift+Enter 换行"></textarea>
        <div class="composer__toolbar">
          <button type="button" @click="sourcePickerOpen=!sourcePickerOpen"><Icon name="database" :size="14"/>{{ current ? '下次提问 · ' : '' }}{{ selectedSources.length }} 个数据源</button>
          <span class="composer__toolbar-spacer"></span>
          <label class="composer__mode"><Icon name="bolt" :size="13"/>分析模式<select v-model="executionMode"><option value="auto">智能判断</option><option value="quick">快速问数</option><option value="deep">深度分析</option></select></label>
          <button type="submit" class="composer__send" :disabled="!canSend" :title="canSend?'发起分析':'请先完成输入与数据源选择'" aria-label="发起分析"><Icon name="play" :size="16"/></button>
        </div>
      </form>
    </section>`,
};
