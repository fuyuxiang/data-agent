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

// Vue's reactive objects cannot be passed directly to structuredClone.
const copyConfiguration = value => JSON.parse(JSON.stringify(value));
const withMissingBindings = (items, selectedIds, label) => {
  const known = new Set(items.map(item => item.id));
  return [...items, ...(selectedIds || []).filter(id => !known.has(id))
    .map(id => ({ id, name: `失效或不可访问的${label}（${id}）`, status: '不可用', enabled: false, missing: true }))];
};

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
      loadError: '',
      editing: null,
      section: 'basic',
      saving: false,
      publishing: false,
      deleting: false,
      testLog: [],
      testing: false,
      testVersion: 0,
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
    providerBindings() {
      return withMissingBindings(this.visibleProviders, this.draft?.provider_id ? [this.draft.provider_id] : [], '模型');
    },
    skillBindings() {
      return withMissingBindings(this.skills.filter(item => item.status === 'published'
        || this.draft?.skill_ids?.includes(item.id)), this.draft?.skill_ids, '技能');
    },
    metricBindings() {
      return withMissingBindings(this.metrics, this.draft?.metric_ids, '指标');
    },
    mcpBindings() {
      return withMissingBindings(this.mcpServers, this.draft?.mcp_server_ids, 'MCP 服务');
    },
    sections() {
      return SECTIONS;
    },
    sourceBindings() {
      const ids = new Set(this.sources.map(item => item.id));
      return [...this.sources, ...(this.draft?.source_ids || [])
        .filter(id => !ids.has(id))
        .map(id => ({ id, name: `失效或不可访问的数据源（${id}）`, status: '不可用', missing: true }))];
    },
    documentBindings() {
      const ids = new Set(this.documents.map(item => item.id));
      return [...this.documents, ...(this.draft?.knowledge_document_ids || [])
        .filter(id => !ids.has(id))
        .map(id => ({ id, name: `失效的知识文档（${id}）`, enabled: false, missing: true }))];
    },
    questionText: {
      get() { return (this.draft?.suggested_questions || []).join('\n'); },
      set(value) {
        if (!this.draft) return;
        this.draft.suggested_questions = String(value || '')
          .split('\n');
      },
    },
  },
  async mounted() {
    await this.load();
  },
  beforeUnmount() {
    this.testVersion += 1;
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const [agents, sources, skills, metrics, documents, mcp, providers, published] = await Promise.all([
          actions.get('/api/agents'),
          actions.get('/api/sources'),
          actions.get('/api/skills'),
          actions.get('/api/semantic/metrics'),
          actions.get('/api/knowledge/documents'),
          actions.get('/api/mcp/servers'),
          actions.get('/api/providers'),
          actions.get('/api/agents?view=published'),
        ]);
        this.agents = agents.items || [];
        this.sources = sources.items || [];
        this.skills = skills.items || [];
        this.metrics = (metrics.items || []).filter(item => item.status === 'approved');
        this.documents = documents.items || [];
        this.mcpServers = mcp.items || [];
        this.providers = providers.items || [];
        // The workbench always receives the live configuration, never a draft.
        state.agents = copyConfiguration(published.items || []);
      } catch (error) {
        this.loadError = error.message;
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
      this.resetTest();
      this.editing = { ...this.blank(), isNew: true };
      this.section = 'basic';
      this.testLog = [];
    },
    edit(agent) {
      this.resetTest();
      const configuration = copyConfiguration(agent);
      this.editing = {
        ...this.blank(),
        ...configuration,
        skill_ids: configuration.skill_ids || (configuration.skill_id ? [configuration.skill_id] : []),
        source_ids: configuration.source_ids || [],
        metric_ids: configuration.metric_ids || [],
        knowledge_document_ids: configuration.knowledge_document_ids || [],
        mcp_server_ids: configuration.mcp_server_ids || [],
        suggested_questions: configuration.suggested_questions || [],
        isNew: false,
      };
      this.section = 'basic';
      this.testLog = [];
    },
    resetTest() {
      this.testVersion += 1;
      this.testing = false;
      this.testQuestion = '';
    },
    closeEditor() {
      if (this.saving || this.publishing) return;
      this.resetTest();
      this.editing = null;
    },
    testKeydown(event) {
      if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        this.test();
      }
    },
    toggle(list, id) {
      const index = list.indexOf(id);
      if (index === -1) list.push(id);
      else list.splice(index, 1);
    },
    canPublish(agent) {
      return state.workspaceRole === 'owner'
        || (agent?.visibility === 'private' && agent?.created_by === (state.user?.id || 'local-default')
          && (!agent.published_version || agent.published_visibility === 'private'));
    },
    canDelete(agent) {
      if (agent.builtin) return false;
      return !agent.published_version || this.canPublish(agent);
    },
    async save({ silent = false } = {}) {
      if (!this.editing || this.saving) return null;
      if (!this.editing.name?.trim()) {
        this.section = 'basic';
        toast('请填写智能体名称', '信息未填完整', 'error');
        return null;
      }
      this.saving = true;
      try {
        const payload = copyConfiguration(this.editing);
        payload.suggested_questions = (payload.suggested_questions || []).map(item => item.trim()).filter(Boolean).slice(0, 8);
        const response = this.editing.isNew
          ? await actions.post('/api/agents', payload)
          : await actions.patch(`/api/agents/${this.editing.id}`, payload);
        this.editing = { ...copyConfiguration(response.item), isNew: false };
        if (!silent) toast(response.item.published_version ? '草稿已保存，线上版本继续可用；发布后生效' : '智能体草稿已保存', '完成');
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
      if (this.publishing || this.saving || !this.editing) return;
      this.publishing = true;
      try {
        if (!await this.save({ silent: true })) return;
        await actions.post(`/api/agents/${this.editing.id}/publish`);
        toast('智能体已发布，用户端现在可以使用', '完成');
        await this.load();
        const refreshed = this.agents.find(item => item.id === this.editing.id);
        if (refreshed) this.edit(refreshed);
      } catch (error) {
        toast(error.message, '发布失败', 'error');
      } finally {
        this.publishing = false;
      }
    },
    async remove(agent) {
      if (!agent || this.deleting) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/agents/${agent.id}`);
        this.agents = this.agents.filter(item => item.id !== agent.id);
        state.agents = state.agents.filter(item => item.id !== agent.id);
        this.removeTarget = null;
        toast('智能体已删除', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      } finally {
        this.deleting = false;
      }
    },
    /** 使用当前表单配置创建真实分析任务，并显示它的结果。 */
    async test() {
      const question = this.testQuestion.trim();
      if (!question || this.testing || !this.editing) return;
      this.testing = true;
      const version = ++this.testVersion;
      this.testLog.push({ role: 'user', text: question });
      this.testQuestion = '';
      let entry = null;
      try {
        const preview = copyConfiguration(this.editing);
        if (preview.source_scope_mode === 'authorized') {
          preview.source_ids = this.sources.filter(item => item.status === 'ready').map(item => item.id);
        }
        const created = await actions.post('/api/analyses', {
          objective: question,
          source_ids: preview.source_ids,
          agent_preview: preview,
          execution_mode: 'quick',
        });
        if (version !== this.testVersion) return;
        entry = {
          role: 'assistant',
          text: '正在使用当前配置分析…',
          runId: created.item.id,
          sessionId: created.item.session_id,
        };
        this.testLog.push(entry);
        for (let attempt = 0; attempt < 30; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          if (version !== this.testVersion) return;
          const detail = await actions.get(`/api/analyses/${entry.runId}`);
          if (version !== this.testVersion) return;
          const run = detail.item || {};
          if (['finished', 'failed', 'cancelled'].includes(run.execution_status)) {
            const result = await actions.get(`/api/analyses/${entry.runId}/results`);
            if (version !== this.testVersion) return;
            entry.text = result.manifest?.payload?.summary
              || (run.execution_status === 'finished' ? '分析已完成，请在对话中查看完整结果。'
                : `分析未完成：${run.stop_reason || run.execution_status}`);
            return;
          }
        }
        entry.text = '任务仍在运行，可在对话中查看进度和结果。';
      } catch (error) {
        if (version === this.testVersion) {
          if (entry) entry.text = `测试进度读取失败：${error.message}。可打开完整分析查看任务状态。`;
          else this.testLog.push({ role: 'assistant', text: `测试失败：${error.message}` });
          if (!this.testQuestion) this.testQuestion = question;
        }
      } finally {
        if (version === this.testVersion) this.testing = false;
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

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>智能体加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

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
                <span v-if="agent.has_unpublished_changes" class="badge">有未发布修改</span>
              </div>
              <p class="small muted" style="margin-top:4px">{{ agent.description || '暂无描述' }}</p>
            </div>
          </div>
          <div class="tag-row">
            <span class="badge"><Icon name="layers" :size="12" />{{ (agent.skill_ids || []).length }} 个技能</span>
            <span class="badge"><Icon name="database" :size="12" />{{ agent.source_scope_mode === 'authorized' ? '当前用户授权的数据' : (agent.source_ids || []).length + ' 个数据源' }}</span>
            <span v-if="agent.published_version" class="badge">线上 v{{ agent.published_version }}</span>
            <span v-if="agent.builtin" class="badge badge--brand">内置</span>
          </div>
          <div class="row" style="margin-top:auto">
            <button v-if="!agent.read_only_draft" class="btn btn--sm" @click="edit(agent)"><Icon name="edit" :size="14" />编辑</button>
            <span v-else class="xs muted">作者的私有草稿仅本人可编辑；这里显示线上共享版本。</span>
            <button v-if="!agent.read_only_draft && agent.status !== 'published'" class="btn btn--sm" @click="edit(agent); section = 'skills'">配置</button>
            <span class="grow"></span>
            <button v-if="canDelete(agent)" class="icon-btn icon-btn--danger" aria-label="删除"
                    @click="removeTarget = agent"><Icon name="trash" :size="15" /></button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!removeTarget" size="small" title="删除智能体" @close="!deleting && (removeTarget = null)">
      <p>确定删除「{{ removeTarget?.name }}」吗？它将不再出现在智能体列表中，已有分析记录仍可查看。</p>
      <template #footer>
        <button class="btn" :disabled="deleting" @click="removeTarget = null">取消</button>
        <button class="btn btn--danger" :disabled="deleting" @click="remove(removeTarget)">{{ deleting ? '删除中…' : '删除' }}</button>
      </template>
    </Modal>

    <Modal :open="!!editing" :title="editing ? (editing.isNew ? '新建智能体' : editing.name) : ''" size="editor"
           @close="closeEditor">
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
              <label class="card card--interactive" style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="radio" :value="null" v-model="draft.provider_id" />
                <b>使用工作空间默认模型</b>
              </label>
              <label v-for="item in providerBindings" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="radio" :value="item.id" v-model="draft.provider_id" :disabled="item.missing" />
                <span class="grow">
                  <b>{{ item.name }}</b>
                  <span class="small muted" style="display:block">
                    {{ item.missing ? '请选择可用模型或改用默认模型，解除失效绑定' : (item.model || '未指定模型') + ' · ' + (item.base_url || '默认地址') }}
                  </span>
                </span>
                <Status :status="item.status || 'configured'" />
              </label>
            </div>
          </template>

          <template v-else-if="section === 'skills'">
            <p class="small muted">技能决定这个智能体会做什么。只绑定它真正需要的。</p>
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="skill in skillBindings" :key="skill.id" class="card card--interactive"
                     style="display:flex;align-items:flex-start;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.skill_ids.includes(skill.id)"
                       :disabled="skill.status !== 'published' && !draft.skill_ids.includes(skill.id)"
                       @change="toggle(draft.skill_ids, skill.id)" style="margin-top:3px" />
                <span class="grow">
                  <b>{{ skill.name }}</b>
                  <span class="small muted" style="display:block">{{ skill.status !== 'published' ? '已停用或失效，可取消绑定' : skill.description }}</span>
                </span>
                <span class="badge">{{ skill.category }}</span>
              </label>
            </div>
          </template>

          <template v-else-if="section === 'data'">
            <p v-if="draft.source_scope_mode === 'authorized'" class="small muted">内置智能体使用提问者当前有权分析的数据。每次提问都可以缩小范围。</p>
            <div v-else class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in sourceBindings" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.source_ids.includes(item.id)"
                       @change="toggle(draft.source_ids, item.id)" />
                <span class="grow">
                  <b>{{ item.name }}</b>
                  <span class="small muted" style="display:block">{{ item.kind }} · {{ item.status }}</span>
                </span>
              </label>
            </div>
            <p v-if="draft.source_scope_mode !== 'authorized' && !sourceBindings.length" class="small muted">还没有数据源。发布前至少要绑定一个。</p>
            <p v-if="sourceBindings.some(item => item.missing)" class="small muted">失效绑定可取消勾选；保存并发布后更新线上范围。</p>
          </template>

          <template v-else-if="section === 'metrics'">
            <p class="small muted">绑定后，这个智能体回答时会优先使用这些正式指标的口径。</p>
            <div class="tag-row">
              <button v-for="item in metricBindings" :key="item.id" class="chip"
                      :class="{ active: draft.metric_ids.includes(item.id) }"
                      @click="toggle(draft.metric_ids, item.id)">{{ item.label || item.name }}</button>
            </div>
            <p v-if="!metricBindings.length" class="small muted">还没有已发布的指标。</p>
            <p v-if="metricBindings.some(item => item.missing)" class="small muted">失效指标可点击取消绑定，保存并发布后更新线上配置。</p>
          </template>

          <template v-else-if="section === 'knowledge'">
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in documentBindings" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.knowledge_document_ids.includes(item.id)"
                       :disabled="item.enabled === false && !draft.knowledge_document_ids.includes(item.id)"
                       @change="toggle(draft.knowledge_document_ids, item.id)" />
                <span class="grow"><b>{{ item.name }}</b><small v-if="item.enabled === false" class="muted"> · 已停用或失效，可取消绑定</small></span>
              </label>
            </div>
            <p v-if="!documentBindings.length" class="small muted">
              还没有知识文档。术语与业务规则在「知识」里维护，会自动参与检索。
            </p>
          </template>

          <template v-else-if="section === 'mcp'">
            <div class="stack" style="display:flex;flex-direction:column;gap:8px">
              <label v-for="item in mcpBindings" :key="item.id" class="card card--interactive"
                     style="display:flex;align-items:center;gap:10px;cursor:pointer">
                <input type="checkbox" :checked="draft.mcp_server_ids.includes(item.id)"
                       :disabled="item.enabled === false && !draft.mcp_server_ids.includes(item.id)"
                       @change="toggle(draft.mcp_server_ids, item.id)" />
                <span class="grow"><b>{{ item.name }}</b><small v-if="item.enabled === false" class="muted"> · 已停用或失效，可取消绑定</small></span>
                <Status :status="item.status || 'configured'" />
              </label>
            </div>
            <p v-if="!mcpBindings.length" class="small muted">还没有连接的 MCP 服务。MCP 是外部系统连接能力，与技能是两类东西。</p>
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
              <template v-if="draft.published_version"><dt>线上版本</dt><dd>v{{ draft.published_version }}{{ draft.has_unpublished_changes ? '（有未发布修改）' : '' }}</dd></template>
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
          <div class="row" style="margin-top:10px;flex-wrap:nowrap">
            <input v-model="testQuestion" class="input input--sm" placeholder="测试问题"
                   aria-label="测试问题" @keydown="testKeydown" />
            <button class="btn btn--primary btn--sm" aria-label="发送测试问题" :disabled="testing || !testQuestion.trim()" @click="test">
              <Icon name="send" :size="14" />
            </button>
          </div>
        </aside>
      </div>

      <template #footer>
        <button class="btn" :disabled="saving || publishing" @click="closeEditor">关闭</button>
        <button class="btn" :disabled="saving || publishing" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        <button v-if="!editing?.isNew && canPublish(editing)" class="btn btn--primary" :disabled="saving || publishing"
                @click="publish">{{ publishing ? '发布中…' : editing?.published_version ? '发布新版本' : '发布' }}</button>
      </template>
    </Modal>`,
};
