/**
 * 对话页：提问 → 执行状态 → 结构化结果 → 继续追问。
 *
 * 这是 V2 的纵向切片主干，也是"默认简单、需要时深入"的落点：
 * 结果占满宽度，SQL/口径/执行过程只在用户点开时从右侧 Drawer 出现。
 */

const { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } = Vue;

import { ClarificationCard, ResultView, renderMarkdown } from '../components/result-blocks.js';
import { Composer } from '../components/composer.js';
import { ExecutionStatus } from '../components/execution.js';
import { Icon } from '../components/icons.js';
import { Drawer, EmptyState, Modal } from '../components/ui.js';
import { actions, state, toast } from '../store.js';
import { navigate } from '../router.js';

const POLL_MS = 1200;

export const ConversationView = {
  name: 'ConversationView',
  components: {
    ClarificationCard, Composer, Drawer, EmptyState, ExecutionStatus, Icon, Modal, ResultView,
  },
  props: { sessionId: String },
  data() {
    return {
      messages: [],
      runs: [],
      results: {},
      tables: {},
      artifacts: {},
      activeRunId: '',
      polling: null,
      pollEpoch: 0,
      pollError: '',
      disposed: false,
      sourceDrawer: { open: false, title: '', blocks: [] },
      filePreview: null,
      feedback: {},
      submitting: false,
      clarificationAnswer: '',
      loading: true,
      runOperations: {},
      runErrors: {},
      deleteTarget: null,
      deleteError: '',
    };
  },
  computed: {
    session() {
      return state.sessions.find(item => item.id === this.sessionId) || null;
    },
    canAnalyze() {
      return ['owner', 'editor', 'analyst'].includes(state.workspaceRole);
    },
    activeRun() {
      const run = this.runs.find(item => item.id === this.activeRunId);
      return run || this.runs.at(-1) || null;
    },
    pendingClarification() {
      return this.runs.find(item => item.execution_status === 'waiting_input'
        && item.stop_reason !== 'clarification_required') || null;
    },
    pendingAnswer() {
      return this.runs.find(item => item.execution_status === 'waiting_input'
        && item.stop_reason === 'clarification_required') || null;
    },
    running() {
      return this.runs.some(item => ['queued', 'running', 'waiting_job', 'cancelling'].includes(item.execution_status));
    },
    lastAnswer() {
      const turns = this.messages.filter(item => item.role === 'assistant');
      return turns.at(-1) || null;
    },
    turns() {
      const byRun = new Map(this.runs.map(run => [run.id, run]));
      const seen = new Set();
      const turns = [];
      for (const message of this.messages) {
        const runId = message.metadata?.run_id;
        if (message.role === 'assistant' && runId && this.results[runId]) continue;
        turns.push({ kind: 'message', id: message.id, message });
        if (message.role === 'user' && byRun.has(runId) && !seen.has(runId)) {
          turns.push({ kind: 'run', id: runId, run: byRun.get(runId) });
          seen.add(runId);
        }
      }
      for (const run of this.runs) {
        if (!seen.has(run.id)) turns.push({ kind: 'run', id: run.id, run });
      }
      return turns;
    },
  },
  async mounted() {
    await this.load();
    if (state.routeParams.ask && this.$refs.composer) {
      this.$refs.composer.text = state.routeParams.ask;
      this.$nextTick(() => this.$refs.composer.focus());
    }
  },
  beforeUnmount() {
    this.disposed = true;
    this.stopPolling();
  },
  watch: {
    sessionId() {
      this.load();
    },
  },
  methods: {
    navigate,
    async load() {
      this.stopPolling();
      const epoch = this.pollEpoch;
      this.loading = true;
      const sessionId = this.sessionId;
      try {
        const [detail, runs] = await Promise.all([
          actions.get(`/api/sessions/${sessionId}`),
          actions.get(`/api/analyses?session_id=${sessionId}&limit=50`),
        ]);
        if (epoch !== this.pollEpoch || this.disposed) return;
        this.results = {};
        this.tables = {};
        this.artifacts = {};
        this.feedback = {};
        this.runErrors = {};
        this.messages = detail.messages || [];
        this.runs = (runs.items || []).slice().reverse();
        this.activeRunId = this.runs.at(-1)?.id || '';
        await this.hydrate(epoch);
        if (epoch === this.pollEpoch && this.running) this.startPolling();
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        if (!this.disposed) this.loading = false;
      }
    },

    async hydrate(epoch = this.pollEpoch) {
      for (const run of this.runs) {
        if (this.results[run.id]) continue;
        if (!run.publication && !['finished', 'partial'].includes(run.execution_status)) continue;
        try {
          const response = await actions.get(`/api/analyses/${run.id}/results`);
          if (epoch !== this.pollEpoch || this.disposed || !this.runs.some(item => item.id === run.id)) return;
          if (response.status !== 'published') continue;
          const payload = response.manifest?.payload || null;
          const tables = await this.loadTables(payload);
          if (epoch !== this.pollEpoch || this.disposed || !this.runs.some(item => item.id === run.id)) return;
          this.results[run.id] = payload;
          this.artifacts[run.id] = response.artifacts || [];
          this.tables[run.id] = tables;
        } catch {
          // 单次结果拉取失败不影响整页展示。
        }
      }
    },

    /** 明细表按结果 id 分页取，成果清单里只带列名不带行。 */
    async loadTables(payload) {
      const output = [];
      for (const table of (payload?.tables || []).slice(0, 2)) {
        if (!table.result_id) continue;
        try {
          const response = await actions.get(
            `/api/query-results/${table.result_id}?offset=0&limit=50`,
          );
          output.push({
            id: table.id,
            title: table.title,
            columns: table.columns || [],
            rows: response.result?.preview || response.result?.data || [],
            total: response.result?.rows ?? (response.result?.preview || []).length,
          });
        } catch {
          // 取不到明细就不渲染表格，不用空表占位。
        }
      }
      return output;
    },

    startPolling() {
      if (this.disposed) return;
      this.stopPolling();
      const epoch = this.pollEpoch;
      let inFlight = false;
      this.polling = setInterval(async () => {
        if (document.hidden || inFlight || epoch !== this.pollEpoch || this.disposed) return;
        inFlight = true;
        try {
          const runs = await actions.get(`/api/analyses?session_id=${this.sessionId}&limit=50`);
          if (epoch !== this.pollEpoch || this.disposed) return;
          this.pollError = '';
          const next = (runs.items || []).slice().reverse();
          const changed = next.length !== this.runs.length || next.some(item =>
            this.runs.find(previous => previous.id === item.id)?.execution_status !== item.execution_status);
          this.runs = next;
          if (changed || !this.running) await this.refreshMessages(epoch);
          await this.hydrate(epoch);
          if (epoch !== this.pollEpoch || this.disposed) return;
          if (!this.running) {
            this.stopPolling();
          }
        } catch (error) {
          if (epoch === this.pollEpoch && !this.disposed) this.pollError = error.message || '暂时无法连接，正在重试';
        } finally {
          inFlight = false;
        }
      }, POLL_MS);
    },
    stopPolling() {
      this.pollEpoch += 1;
      if (this.polling) clearInterval(this.polling);
      this.polling = null;
    },

    async refreshMessages(epoch = this.pollEpoch) {
      try {
        const detail = await actions.get(`/api/sessions/${this.sessionId}`);
        if (epoch !== this.pollEpoch || this.disposed) return;
        this.messages = detail.messages || [];
      } catch {
        // 保留旧消息，不因为一次轮询失败清空界面。
      }
    },

    async ask(payload) {
      if (!this.sessionId || this.submitting) return;
      this.submitting = true;
      this.stopPolling();
      try {
        const body = {
          session_id: this.sessionId,
          objective: payload.text,
          source_ids: payload.sourceIds,
          agent_id: payload.agentId || undefined,
          skill_id: payload.skillHint || undefined,
        };
        const created = await actions.post('/api/analyses', body);
        if (payload.files.length) {
          try {
            await actions.uploadAttachments(created.item.id, payload.files);
          } catch (error) {
            await actions.post(`/api/analyses/${created.item.id}/control`, { action: 'cancel' }).catch(() => {});
            throw error;
          }
        }
        this.$refs.composer?.reset();
        this.runs.push(created.item);
        this.activeRunId = created.item.id;
        await this.refreshMessages();
        this.startPolling();
      } catch (error) {
        toast(error.message, '无法开始分析', 'error');
        this.startPolling();
      } finally {
        this.submitting = false;
      }
    },

    async confirmClarification(choices = {}) {
      const run = this.pendingClarification;
      if (!run) return;
      try {
        const detail = await actions.get(`/api/analyses/${run.id}`);
        const revision = detail.item?.contract;
        if (!revision) throw new Error('分析口径不存在，请刷新后重试');
        const contract = { ...revision.payload };
        if (choices.period) {
          const today = new Date();
          const date = (year, month, day) => `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
          const month = today.getMonth();
          const year = today.getFullYear();
          const offset = choices.period === '上月' ? -1
            : choices.period === '最近3个月' ? -2
              : choices.period === '最近12个月' ? -11 : 0;
          const first = new Date(year, month + offset, 1);
          contract.time_range = {
            start: date(first.getFullYear(), first.getMonth(), 1),
            end: date(year, month, today.getDate()),
          };
          if (choices.period === '上月') {
            const last = new Date(year, month, 0);
            contract.time_range.end = date(last.getFullYear(), last.getMonth(), last.getDate());
          }
          if (choices.period === '自定义') {
            contract.time_range = { start: choices.start, end: choices.end };
            if (!choices.start || !choices.end || choices.start > choices.end) {
              throw new Error('请选择有效的开始和结束日期');
            }
          }
        }
        if (choices.focus) {
          contract.confirmed_assumptions = [
            ...(contract.confirmed_assumptions || []), `重点关注：${choices.focus}`,
          ];
          if (choices.focus === '城市差异') contract.dimensions = ['城市'];
          if (choices.focus === '品类表现') contract.dimensions = ['品类'];
        }
        const confirmed = await actions.post(`/api/analyses/${run.id}/contract/confirm`, {
          expected_version: revision.version, contract,
        });
        this.runs = this.runs.map(item => (item.id === run.id
          ? { ...item, execution_status: confirmed.item?.execution_status || 'queued' } : item));
        this.startPolling();
      } catch (error) {
        toast(error.message, '无法开始', 'error');
      }
    },
    async answerClarification() {
      const answer = this.clarificationAnswer.trim();
      if (!this.pendingAnswer || !answer) return;
      try {
        await actions.post(`/api/analyses/${this.pendingAnswer.id}/clarifications`, { answer });
        this.clarificationAnswer = '';
        await this.refreshMessages();
        this.startPolling();
      } catch (error) {
        toast(error.message, '无法提交补充说明', 'error');
      }
    },

    async cancelClarification() {
      const run = this.pendingClarification;
      if (!run) return;
      await this.cancelRun(run);
    },
    isCancelable(run) {
      return ['queued', 'running', 'waiting_job', 'waiting_input', 'waiting_approval', 'paused', 'cancelling']
        .includes(run?.execution_status);
    },
    cancelError(run) {
      return this.runErrors[run.id] || (run.cancel_errors || []).map(item => item.message).filter(Boolean).join('；');
    },
    async cancelRun(run) {
      if (!run || !this.canAnalyze || !this.isCancelable(run) || this.runOperations[run.id]) return;
      this.runOperations[run.id] = 'cancel';
      delete this.runErrors[run.id];
      try {
        const response = await actions.post(`/api/analyses/${run.id}/control`, { action: 'cancel' });
        this.runs = this.runs.map(item => (item.id === run.id
          ? (response.item || { ...item, execution_status: 'cancelling' }) : item));
        if (response.item?.execution_status === 'cancelling' || this.running) this.startPolling();
        else {
          this.stopPolling();
          await this.refreshMessages();
          await this.hydrate();
        }
      } catch (error) {
        this.runErrors[run.id] = error.message;
        toast(error.message, '终止失败', 'error');
        this.startPolling();
      } finally {
        delete this.runOperations[run.id];
      }
    },
    deleteRun(run) {
      if (!run || !this.canAnalyze || this.isCancelable(run) || this.runOperations[run.id]) return;
      this.deleteTarget = run;
      this.deleteError = '';
    },
    closeRunDelete() {
      if (!this.deleteTarget || !this.runOperations[this.deleteTarget.id]) this.deleteTarget = null;
    },
    async confirmRunDelete() {
      const run = this.deleteTarget;
      if (!run || !this.canAnalyze || this.runOperations[run.id]) return;
      this.runOperations[run.id] = 'delete';
      this.deleteError = '';
      this.stopPolling();
      try {
        await actions.remove(`/api/analyses/${run.id}`);
        delete this.results[run.id];
        delete this.tables[run.id];
        delete this.artifacts[run.id];
        delete this.feedback[run.id];
        this.runs = this.runs.filter(item => item.id !== run.id);
        this.activeRunId = this.runs.at(-1)?.id || '';
        this.deleteTarget = null;
        await this.refreshMessages();
        toast('分析已移入回收站，资料库成果仍可使用', '完成');
      } catch (error) {
        this.deleteError = error.message;
        toast(error.message, '删除失败', 'error');
      } finally {
        delete this.runOperations[run.id];
        if (this.running) this.startPolling();
      }
    },

    /** 结果操作条：全站统一，不同页面不会出现两套动作。 */
    async generate(runId, kind, label) {
      if (!runId) return;
      try {
        await actions.post(`/api/analyses/${runId}/artifacts`, { kinds: [kind] });
        this.artifacts[runId] = await actions.get(`/api/analyses/${runId}/results`)
          .then(response => response.artifacts || []);
        toast(`${label}已生成，可在资料库查看`, '完成');
      } catch (error) {
        toast(error.message, `生成${label}失败`, 'error');
      }
    },

    async sendFeedback(runId, rating) {
      if (this.feedback[runId]) return;
      this.feedback[runId] = rating;
      try {
        await actions.post('/api/feedback', {
          run_id: runId,
          rating: rating === 'up' ? 'correct' : 'incorrect',
          category: rating === 'up' ? '结果符合预期' : '需要改进',
        });
        toast('已记录反馈', '谢谢');
      } catch (error) {
        this.feedback[runId] = '';
        toast(error.message, '反馈未提交', 'error');
      }
    },

    /** 查看来源：默认隐藏的复杂度，只在用户点开时出现。 */
    async openSource(runId) {
      const blocks = [];
      try {
        const detail = await actions.get(`/api/analyses/${runId}/execution`);
        for (const item of detail.item?.actions || []) {
          if (item.tool_id === 'query_metric') {
            blocks.push({ title: '使用的指标', body: String(item.arguments?.metric || '由指标编译器执行') });
          }
          if (item.tool_id === 'query_data') {
            blocks.push({ title: '执行的 SQL', body: item.arguments?.sql || '未记录 SQL' });
          }
          if (item.tool_id === 'list_semantic_metrics') {
            blocks.push({ title: '读取的指标清单', body: JSON.stringify(item.arguments, null, 2) });
          }
        }
        const validations = await actions.get(`/api/analyses/${runId}/validations`);
        if ((validations.items || []).length) {
          blocks.push({
            title: '核验结果',
            body: validations.items.map(item => `${item.rule_id} · ${item.status} · ${item.severity || ''}`).join('\n'),
          });
        }
      } catch (error) {
        toast(error.message, '无法读取来源', 'error');
        return;
      }
      if (!blocks.length) blocks.push({ title: '来源', body: '本次分析没有可展示的查询来源。' });
      this.sourceDrawer = { open: true, title: '查看来源', blocks };
    },

    openTimeline(runId) {
      actions.get(`/api/analyses/${runId}/execution`)
        .then(response => {
          const item = response.item;
          this.sourceDrawer = {
            open: true,
            title: '执行过程',
            blocks: [
              { title: '使用的技能', body: (item.skills || []).join('、') || '自动分析' },
              {
                title: '模型决策',
                body: (item.decisions || []).map(d => `#${d.sequence} ${d.model} · ${d.tool_call_count} 次工具调用`).join('\n')
                  || '无记录',
              },
              {
                title: '工具调用',
                body: (item.actions || []).map(a => `${a.tool_id} · ${a.status}${a.error_code ? ' · ' + a.error_code : ''}`).join('\n')
                  || '无记录',
              },
            ],
          };
        })
        .catch(error => toast(error.message, '无法读取执行过程', 'error'));
    },

    askFollowUp(text) {
      navigate('workbench', { ask: text });
    },
    previewFile(file) {
      this.filePreview = file;
    },
    markdown(value) {
      return renderMarkdown(value);
    },
    /** 把后端 run 行翻译成执行状态组件认识的形状。 */
    runView(run) {
      const payload = this.results[run.id] || {};
      return {
        execution_status: run.execution_status,
        quality_status: run.quality_status,
        stop_reason: run.stop_reason,
        hasData: Boolean((payload.tables || [])[0]?.result_id),
        hasChart: Boolean((payload.charts || []).length),
        hasAnalysis: Boolean(payload.kpis?.length),
        validated: payload.validation?.status === 'PASS',
      };
    },
    runTitle(run) {
      const active = this.activeRunId === run.id;
      return active ? '正在分析你的问题' : '分析中';
    },
  },
  template: `
    <div class="view__inner view__inner--reading" style="padding:24px 28px 0">
      <div v-if="pollError" class="card row row--between" style="margin-bottom:12px" role="status">
        <p class="small muted">连接暂时中断，正在重试。{{ pollError }}</p>
        <button class="btn btn--sm" @click="startPolling">重新连接</button>
      </div>
      <div v-if="loading" class="stack" style="padding-top:24px">
        <div class="skeleton" style="height:56px"></div>
        <div class="skeleton" style="height:180px"></div>
      </div>

      <div v-else class="conversation" style="padding-top:8px">
        <template v-for="turn in turns" :key="turn.kind + ':' + turn.id">
          <div v-if="turn.kind === 'message' && turn.message.role === 'user'" class="turn turn--user">
            <div class="turn__bubble">{{ turn.message.content }}</div>
          </div>
          <div v-else-if="turn.kind === 'message' && turn.message.role === 'assistant'" class="turn">
            <div class="markdown" v-html="markdown(turn.message.content)"></div>
          </div>
          <div v-else-if="turn.kind === 'run'" class="turn" :data-run-id="turn.run.id">
          <ExecutionStatus :run="runView(turn.run)" :title="runTitle(turn.run)" />
          <div v-if="canAnalyze" class="row" style="margin:10px 0 4px">
            <button v-if="isCancelable(turn.run)" class="btn btn--sm btn--danger"
                    :disabled="!!runOperations[turn.run.id] || (turn.run.execution_status === 'cancelling' && !cancelError(turn.run))"
                    @click="cancelRun(turn.run)">
              {{ runOperations[turn.run.id] ? '正在终止…' : (cancelError(turn.run) ? '重试终止' : (turn.run.execution_status === 'cancelling' ? '正在终止…' : '终止分析')) }}
            </button>
            <button v-else class="btn btn--sm" :disabled="!!runOperations[turn.run.id]" @click="deleteRun(turn.run)">删除分析</button>
          </div>
          <p v-if="cancelError(turn.run)" class="small" style="color:var(--danger)">{{ cancelError(turn.run) }}</p>
          <ResultView v-if="results[turn.run.id]" :payload="results[turn.run.id]"
                      :can-export="canAnalyze"
                      :artifacts="artifacts[turn.run.id] || []" :tables="tables[turn.run.id] || []"
                      @preview="previewFile" @source="openSource(turn.run.id)" />
          <div v-if="results[turn.run.id]" class="result-actions">
            <button class="btn btn--sm" @click="openSource(turn.run.id)"><Icon name="shield" :size="14" />查看来源</button>
            <button class="btn btn--sm" @click="openTimeline(turn.run.id)"><Icon name="history" :size="14" />执行过程</button>
            <button v-if="canAnalyze" class="btn btn--sm" @click="generate(turn.run.id, 'report_docx', 'Word 报告')"><Icon name="fileText" :size="14" />生成报告</button>
            <button v-if="canAnalyze" class="btn btn--sm" @click="generate(turn.run.id, 'report_pptx', 'PPT')"><Icon name="filePresentation" :size="14" />生成 PPT</button>
            <button v-if="canAnalyze" class="btn btn--sm" @click="generate(turn.run.id, 'data_xlsx', 'Excel')"><Icon name="fileSpreadsheet" :size="14" />导出 Excel</button>
            <button class="btn btn--sm" @click="navigate('library')"><Icon name="library" :size="14" />查看资料库</button>
            <span class="grow"></span>
            <button class="icon-btn tip" data-tip="结果有帮助" @click="sendFeedback(turn.run.id, 'up')"><Icon name="thumbUp" :size="16" /></button>
            <button class="icon-btn tip" data-tip="结果需要改进" @click="sendFeedback(turn.run.id, 'down')"><Icon name="thumbDown" :size="16" /></button>
          </div>
          </div>
        </template>

        <ClarificationCard v-if="canAnalyze && pendingClarification" :contract="pendingClarification.contract"
                           @submit="confirmClarification" @cancel="cancelClarification" />

        <div v-if="canAnalyze && pendingAnswer" class="card" style="margin:16px 0">
          <h3 class="card__title">还需要你补充一点信息</h3>
          <p class="small muted">说明分析对象、时间或口径后，系统会继续当前任务。</p>
          <textarea v-model.trim="clarificationAnswer" class="textarea" placeholder="输入补充说明"></textarea>
          <button class="btn btn--primary btn--sm" :disabled="!clarificationAnswer.trim()"
                  @click="answerClarification">继续分析</button>
        </div>

        <EmptyState v-if="!messages.length && !runs.length" icon="chat" title="还没有提问"
                    text="在上方输入你的业务问题，系统会自动选择合适的能力并给出可核验的结论。" />
      </div>
    </div>

    <div v-if="!loading" class="view__inner view__inner--reading" style="padding:0 28px 24px">
      <Composer ref="composer" :disabled="running || submitting || !canAnalyze" placeholder="继续追问，或换一个问题…" @submit="ask" />
      <p v-if="!canAnalyze" class="small muted">当前为只读权限，无法继续分析。</p>
      <button class="btn btn--sm" style="margin-top:10px" @click="navigate('trash', { collection: 'agent_runs' })"><Icon name="trash" :size="14" />恢复已删除分析</button>
    </div>

    <Modal :open="!!deleteTarget" title="删除分析" @close="closeRunDelete">
      <p v-if="deleteTarget" class="small">将「{{ deleteTarget.contract?.payload?.objective || '这条分析' }}」移入回收站？会话中的提问和回答将隐藏，资料库成果和执行记录会保留，可在回收站恢复。</p>
      <p v-if="deleteError" class="small" style="color:var(--danger)">{{ deleteError }}</p>
      <template #footer>
        <button class="btn" :disabled="!!runOperations[deleteTarget?.id]" @click="closeRunDelete">取消</button>
        <button class="btn btn--primary" :disabled="!!runOperations[deleteTarget?.id]" @click="confirmRunDelete">{{ runOperations[deleteTarget?.id] ? '删除中…' : '移入回收站' }}</button>
      </template>
    </Modal>

    <Drawer :open="sourceDrawer.open" :title="sourceDrawer.title" subtitle="默认隐藏，需要时再深入"
            @close="sourceDrawer.open = false">
      <div v-for="block in sourceDrawer.blocks" :key="block.title" style="margin-bottom:18px">
        <h3 class="small" style="margin-bottom:6px">{{ block.title }}</h3>
        <pre>{{ block.body }}</pre>
      </div>
    </Drawer>

    <Modal :open="!!filePreview" :title="filePreview?.title || '预览'" wide @close="filePreview = null">
      <iframe v-if="filePreview" class="preview-frame" :src="'/api/library/' + filePreview.id + '/preview'"
              :title="filePreview.title"></iframe>
    </Modal>`,
};
