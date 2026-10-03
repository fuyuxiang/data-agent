/**
 * 智能体构建器。
 *
 * 一个 Agent = 模型 + 技能 + 数据 + 指标 + 知识 + MCP + 权限。
 * 右侧是实时测试对话，不是设计向导。
 */

import { Icon } from '../components/icons.js';
import { EmptyState, Modal, SearchInput, Status, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';
import { navigate } from '../router.js';

const SECTIONS = [
  { key: 'basic', label: '基本信息', icon: 'fileText' },
  { key: 'model', label: '模型', icon: 'cpu' },
  { key: 'skills', label: '技能', icon: 'layers' },
  { key: 'data', label: '数据', icon: 'database' },
  { key: 'metrics', label: '指标', icon: 'metric' },
  { key: 'knowledge', label: '知识', icon: 'book' },
  { key: 'mcp', label: 'MCP', icon: 'plug' },
  { key: 'experience', label: '体验设置', icon: 'sparkle' },
  { key: 'permission', label: '权限', icon: 'shield' },
];

export const AgentBuilderView = {
  name: 'AgentBuilderView',
  components: { EmptyState, Icon, Modal, SearchInput, Status, Tabs },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return {
      agents: [],
      sources: [],
      skills: [],
      metrics: [],
      documents: [],
      mcpServers: [],
      providers: [],
      loading: true,
      editing: null,
      section: 'basic',
      saving: false,
      testLog: [],
      testing: false,
      testQuestion: '',
      removeTarget: null,
    };
  },
  computed: {
    draft() {
      return this.editing;
    },
    published() {
      return this.agents.filter(item => item.status === 'published');
    },
    visibleProviders() {
      return this.providers.filter(item => item.enabled !== false);
    },
    sections() {
      return SECTIONS;
    },
    questionText: {
      get() { return (this.draft?.suggested_questions || []).join('\n'); },
      set(value) {
        if (!this.draft) return;
        this.draft.suggested_questions = String(value || '')
          .split('\n').map(item => item.trim()).filter(Boolean).slice(0, 8);
      },
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const [agents, sources, skills, metrics, documents, mcp, providers] = await Promise.all([
          actions.get('/api/agents'),
          actions.get('/api/sources'),
          actions.get('/api/skills'),
          actions.get('/api/semantic/metrics'),
          actions.get('/api/knowledge/documents'),
          actions.get('/api/mcp/servers'),
          actions.get('/api/providers'),
        ]);
        this.agents = agents.items || [];
        this.sources = sources.items || [];
        this.skills = skills.items || [];
        this.metrics = (metrics.items || []).filter(item => item.status === 'approved');
        this.documents = documents.items || [];
        this.mcpServers = mcp.items || [];
        this.providers = providers.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    blank() {
      return {
        name: '', description: '', instruction: '', icon: 'robot', tags: [],
        provider_id: null, skill_ids: [], source_ids: [], metric_ids: [],
        knowledge_document_ids: [], mcp_server_ids: [],
        welcome: '', suggested_questions: [], visibility: 'workspace', status: 'draft',
      };
    },
    create() {
      this.editing = { ...this.blank(), isNew: true };
      this.section = 'basic';
      this.testLog = [];
    },
    edit(agent) {
      this.editing = {
        ...this.blank(),
        ...agent,
        skill_ids: agent.skill_ids || (agent.skill_id ? [agent.skill_id] : []),
        source_ids: agent.source_ids || [],
        metric_ids: agent.metric_ids || [],
        knowledge_document_ids: agent.knowledge_document_ids || [],
        mcp_server_ids: agent.mcp_server_ids || [],
        suggested_questions: agent.suggested_questions || [],
        isNew: false,
      };
      this.section = 'basic';
      this.testLog = [];
    },
    toggle(list, id) {
      const index = list.indexOf(id);
      if (index === -1) list.push(id);
      else list.splice(index, 1);
    },
    canPublish(agent) {
      return state.workspaceRole === 'owner'
        || (agent?.visibility === 'private' && agent?.created_by === (state.user?.id || 'local-default'));
    },
    canDelete(agent) {
      if (agent.builtin) return false;
      return agent.status !== 'published' || this.canPublish(agent);
    },
    async save({ silent = false } = {}) {
      this.saving = true;
      try {
        const response = this.editing.isNew
          ? await actions.post('/api/agents', this.editing)
          : await actions.patch(`/api/agents/${this.editing.id}`, this.editing);
        this.editing = { ...response.item, isNew: false };
        if (!silent) toast('智能体已保存', '完成');
        await this.load();
        return response.item;
      } catch (error) {
        toast(error.message, '保存失败', 'error');
        return null;
      } finally {
        this.saving = false;
      }
    },
    async publish() {
      if (!await this.save({ silent: true })) return;
      try {
        await actions.post(`/api/agents/${this.editing.id}/publish`);
        toast('智能体已发布，用户端现在可以使用', '完成');
        await this.load();
        const refreshed = this.agents.find(item => item.id === this.editing.id);
        if (refreshed) this.edit(refreshed);
      } catch (error) {
        toast(error.message, '发布失败', 'error');
      }
    },
    async remove(agent) {
      try {
        await actions.remove(`/api/agents/${agent.id}`);
        this.agents = this.agents.filter(item => item.id !== agent.id);
        this.removeTarget = null;
        toast('智能体已删除', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      }
    },
    /** 使用当前表单配置创建真实分析任务，并显示它的结果。 */
    async test() {
      const question = this.testQuestion.trim();
      if (!question || this.testing || !this.editing) return;
      this.testing = true;
      this.testLog.push({ role: 'user', text: question });
      this.testQuestion = '';
      try {
        const created = await actions.post('/api/analyses', {
          objective: question,
          source_ids: this.editing.source_ids,
          agent_preview: this.editing,
          execution_mode: 'quick',
        });
        const entry = {
          role: 'assistant',
          text: '正在使用当前配置分析…',
          runId: created.item.id,
          sessionId: created.item.session_id,
        };
        this.testLog.push(entry);
        for (let attempt = 0; attempt < 30; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          const detail = await actions.get(`/api/analyses/${entry.runId}`);
          const run = detail.item || {};
          if (['finished', 'failed', 'cancelled'].includes(run.execution_status)) {
            const result = await actions.get(`/api/analyses/${entry.runId}/results`);
            entry.text = result.manifest?.payload?.summary
              || (run.execution_status === 'finished' ? '分析已完成，请在对话中查看完整结果。'
                : `分析未完成：${run.stop_reason || run.execution_status}`);
            return;
          }
        }
        entry.text = '任务仍在运行，可在对话中查看进度和结果。';
      } catch (error) {
        this.testLog.push({ role: 'assistant', text: `测试失败：${error.message}` });
      } finally {
        this.testing = false;
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">智能体</h1>
          <p class="page-head__desc">
            智能体不是一段提示词，而是模型、技能、数据、指标、知识与 MCP 的组合。
            组合不同，能力就不同。
          </p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--primary btn--sm" @click="create"><Icon name="plus" :size="14" />新建智能体</button>
        </div>
      </header>

      <div v-if="loading" class="grid grid--2">
        <div v-for="index in 4" :key="index" class="skeleton" style="height:150px"></div>
      </div>

      <EmptyState v-else-if="!agents.length" icon="robot" title="还没有智能体"
                  text="创建第一个智能体，绑定它需要的技能、数据和知识。" />

      <div v-else class="grid grid--2">
        <article v-for="agent in agents" :key="agent.id" class="card card--interactive file-tile">
          <div class="agent-card__top">
            <span class="agent-card__mark"><Icon :name="agent.icon || 'robot'" :size="20" /></span>
            <div class="grow">
              <div class="row row--between">
                <b>{{ agent.name }}</b>
                <Status :status="agent.status" />
              </div>
              <p class="small muted" style="margin-top:4px">{{ agent.description || '暂无描述' }}</p>
            </div>
          </div>
          <div class="tag-row">
            <span class="badge"><Icon name="layers" :size="12" />{{ (agent.skill_ids || []).length }} 个技能</span>
            <span class="badge"><Icon name="database" :size="12" />{{ (agent.source_ids || []).length }} 个数据源</span>
            <span v-if="agent.builtin" class="badge badge--brand">内置</span>
          </div>
          <div class="row" style="margin-top:auto">
            <button class="btn btn--sm" @click="edit(agent)"><Icon name="edit" :size="14" />编辑</button>
            <button v-if="agent.status !== 'published'" class="btn btn--sm" @click="edit(agent); section = 'skills'">配置</button>
            <span class="grow"></span>
            <button v-if="canDelete(agent)" class="icon-btn icon-btn--danger" aria-label="删除"
                    @click="removeTarget = agent"><Icon name="trash" :size="15" /></button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!removeTarget" title="删除智能体" @close="removeTarget = null">
      <p>确定删除「{{ removeTarget?.name }}」吗？它将不再出现在智能体列表中，已有分析记录仍可查看。</p>
      <template #footer>
        <button class="btn" @click="removeTarget = null">取消</button>
        <button class="btn btn--danger" @click="remove(removeTarget)">删除</button>
      </template>
    </Modal>

    <Modal :open="!!editing" :title="editing ? (editing.isNew ? '新建智能体' : editing.name) : ''" wide
           @close="editing = null">
      <div v-if="draft" class="builder">
        <nav class="builder__rail">
          <button v-for="item in sections" :key="item.key" class="admin__nav-item"
                  :class="{ active: section === item.key }" @click="section = item.key">
            <Icon :name="item.icon" :size="15" />{{ item.label }}
          </button>
        </nav>

        <div class="stack">
          <template v-if="section === 'basic'">
            <label class="field"><span>名称<em> *</em></span>
              <input v-model.trim="draft.name" class="input" placeholder="销售分析助手" /></label>
            <label class="field"><span>描述</span>
              <textarea v-model.trim="draft.description" class="textarea" rows="2"
                        placeholder="销售查询、趋势、归因、预测"></textarea></label>
            <label class="field"><span>角色说明</span>
              <textarea v-model.trim="draft.instruction" class="textarea" rows="8"
                        placeholder="你是负责销售经营分析的助手。先确认口径再取数，结论先行，每个数字都要能追溯。"></textarea></label>
            <label class="field"><span>能力标签（逗号分隔）</span>
              <input :value="(draft.tags || []).join(', ')" class="input"
                     @input="draft.tags = $event.target.value.split(/[,，]/).map(s => s.trim()).filter(Boolean)"
                     placeholder="销售, 数据分析" /></label>
          </template>

          <template v-else-if="section === 'model'">
            <p class="small muted">不选则使用工作空间默认模型。</p>
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in visibleProviders" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="radio" :value="item.id" v-model="draft.provider_id" />
                <span class="grow">
                  <b>{{ item.name }}</b>
                  <span class="small muted" style="display:block">
                    {{ item.model || '未指定模型' }} · {{ item.base_url || '默认地址' }}
                  </span>
                </span>
                <Status :status="item.status || 'configured'" />
              </label>
            </div>
          </template>

          <template v-else-if="section === 'skills'">
            <p class="small muted">技能决定这个智能体会做什么。只绑定它真正需要的。</p>
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="skill in skills" :key="skill.id" class="card card--interactive"
                     style="display:flex;align-items:flex-start;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.skill_ids.includes(skill.id)"
                       @change="toggle(draft.skill_ids, skill.id)" style="margin-top:3px" />
                <span class="grow">
                  <b>{{ skill.name }}</b>
                  <span class="small muted" style="display:block">{{ skill.description }}</span>
                </span>
                <span class="badge">{{ skill.category }}</span>
              </label>
            </div>
          </template>

          <template v-else-if="section === 'data'">
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in sources" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.source_ids.includes(item.id)"
                       @change="toggle(draft.source_ids, item.id)" />
                <span class="grow">
                  <b>{{ item.name }}</b>
                  <span class="small muted" style="display:block">{{ item.kind }} · {{ item.status }}</span>
                </span>
              </label>
            </div>
            <p v-if="!sources.length" class="small muted">还没有数据源。发布前至少要绑定一个。</p>
          </template>

          <template v-else-if="section === 'metrics'">
            <p class="small muted">绑定后，这个智能体回答时会优先使用这些正式指标的口径。</p>
            <div class="tag-row">
              <button v-for="item in metrics" :key="item.id" class="chip"
                      :class="{ active: draft.metric_ids.includes(item.id) }"
                      @click="toggle(draft.metric_ids, item.id)">{{ item.label || item.name }}</button>
            </div>
            <p v-if="!metrics.length" class="small muted">还没有已发布的指标。</p>
          </template>

          <template v-else-if="section === 'knowledge'">
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in documents" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.knowledge_document_ids.includes(item.id)"
                       @change="toggle(draft.knowledge_document_ids, item.id)" />
                <span class="grow"><b>{{ item.name }}</b></span>
              </label>
            </div>
            <p v-if="!documents.length" class="small muted">
              还没有知识文档。术语与业务规则在「知识」里维护，会自动参与检索。
            </p>
          </template>

          <template v-else-if="section === 'mcp'">
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in mcpServers" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.mcp_server_ids.includes(item.id)"
                       @change="toggle(draft.mcp_server_ids, item.id)" />
                <span class="grow"><b>{{ item.name }}</b></span>
                <Status :status="item.status || 'configured'" />
              </label>
            </div>
            <p v-if="!mcpServers.length" class="small muted">还没有连接的 MCP 服务。MCP 是外部系统连接能力，与技能是两类东西。</p>
          </template>

          <template v-else-if="section === 'experience'">
            <label class="field"><span>欢迎语</span>
              <textarea v-model.trim="draft.welcome" class="textarea" rows="3"
                        placeholder="问我任何销售数据问题，我会自动选择合适的能力并给出可核验的结论。"></textarea></label>
            <label class="field"><span>推荐问题（每行一个，最多 8 条）</span>
              <textarea v-model="questionText" class="textarea" rows="5"
                        placeholder="本月销售额是多少？&#10;为什么华东销售下降？"></textarea></label>
          </template>

          <template v-else>
            <label class="field"><span>可见范围</span>
              <select v-model="draft.visibility" class="select">
                <option value="workspace">工作空间内所有成员</option>
                <option value="private">仅自己</option>
              </select></label>
            <p class="small muted">
              智能体只能使用当前用户有权访问的数据、指标和知识。权限过滤发生在执行前，
              不是执行后。
            </p>
            <dl class="definition">
              <dt>状态</dt><dd>{{ draft.status }}</dd>
              <dt>版本</dt><dd>v{{ draft.version || 1 }}</dd>
            </dl>
          </template>
        </div>

        <aside class="builder__test">
          <h3 class="card__title" style="margin-bottom:6px">实时测试</h3>
          <p class="xs faint" style="margin-bottom:10px">用真实数据跑一次，确认这个智能体能用。</p>
          <div class="stack" style="display:flex;flex-direction:column;gap:8px;max-height:280px;overflow:auto">
            <EmptyState v-if="!testLog.length" icon="chat" title="还没有测试记录" text="在下面提一个问题。" />
            <div v-for="(item, index) in testLog" :key="index" class="turn__bubble"
                 :style="item.role === 'user' ? 'align-self:flex-end;max-width:100%' : 'align-self:flex-start;background:var(--surface-sunken);color:var(--text-primary);max-width:100%'">
              {{ item.text }}
              <button v-if="item.sessionId" class="btn btn--sm" style="margin-top:8px"
                      @click="navigate('conversation', { id: item.sessionId })">查看完整分析</button>
            </div>
          </div>
          <div class="row" style="margin-top:10px">
            <input v-model="testQuestion" class="input input--sm" placeholder="测试问题"
                   @keyup.enter="test" />
            <button class="btn btn--primary btn--sm" :disabled="testing || !testQuestion.trim()" @click="test">
              <Icon name="send" :size="14" />
            </button>
          </div>
        </aside>
      </div>

      <template #footer>
        <button class="btn" @click="editing = null">关闭</button>
        <button class="btn" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        <button v-if="!editing?.isNew && editing?.status !== 'published' && canPublish(editing)" class="btn btn--primary"
                @click="publish">发布</button>
      </template>
    </Modal>`,
};
