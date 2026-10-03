/**
 * 技能管理。
 *
 * 技能是 V2 最重要的新能力之一，但编辑器刻意保持克制：左配置 / 中编辑 /
 * 右测试，没有节点、连线、拖拽工作流。
 */

import { Icon } from '../components/icons.js';
import {
  DataTable, Drawer, EmptyState, Modal, SearchInput, Status, Switch, Tabs,
} from '../components/ui.js';
import { actions, state, toast } from '../store.js';

/** 技能编辑器用「每行一项」而不是多选框，触发场景和工具列表通常很长。 */
const splitLines = (value) => String(value || '')
  .split('\n').map(item => item.trim()).filter(Boolean);

const SECTION_TABS = [
  { key: 'basic', label: '基本信息', icon: 'fileText' },
  { key: 'triggers', label: '触发与示例', icon: 'target' },
  { key: 'resources', label: '数据与知识', icon: 'database' },
  { key: 'io', label: '输入输出', icon: 'layers' },
  { key: 'advanced', label: '高级', icon: 'settings' },
];

export const SkillsView = {
  name: 'SkillsView',
  components: {
    DataTable, Drawer, EmptyState, Icon, Modal, SearchInput, Status, Switch, Tabs,
  },
  setup() {
    return { state, toast };
  },
  data() {
    return {
      items: [],
      unavailable: [],
      categories: [],
      canManage: false,
      query: '',
      category: '',
      statusFilter: '',
      loading: true,
      editor: null,
      section: 'basic',
      saving: false,
      aiOpen: false,
      aiPrompt: '',
      aiDraft: null,
      testing: false,
      testResult: null,
      testQuestion: '',
    };
  },
  computed: {
    filtered() {
      const keyword = this.query.trim().toLowerCase();
      return this.items.filter(item => {
        if (this.category && item.category !== this.category) return false;
        if (this.statusFilter && item.status !== this.statusFilter) return false;
        if (!keyword) return true;
        return `${item.name} ${item.description || ''}`.toLowerCase().includes(keyword);
      });
    },
    workspaceSkills() {
      return this.items.filter(item => item.editable !== false);
    },
    sectionTabs() {
      return SECTION_TABS;
    },
    triggerText: {
      get() { return (this.editor?.triggers || []).join('\n'); },
      set(value) { if (this.editor) this.editor.triggers = splitLines(value); },
    },
    exampleText: {
      get() { return (this.editor?.example_questions || []).join('\n'); },
      set(value) { if (this.editor) this.editor.example_questions = splitLines(value); },
    },
    resourceText: {
      get() { return (this.editor?.source_ids || []).join('\n'); },
      set(value) { if (this.editor) this.editor.source_ids = splitLines(value); },
    },
    mcpText: {
      get() { return (this.editor?.mcp_server_ids || []).join('\n'); },
      set(value) { if (this.editor) this.editor.mcp_server_ids = splitLines(value); },
    },
    toolText: {
      get() { return (this.editor?.allowed_tools || []).join('\n'); },
      set(value) { if (this.editor) this.editor.allowed_tools = splitLines(value); },
    },
    inputText: {
      get() { return (this.editor?.inputs || []).join('\n'); },
      set(value) { if (this.editor) this.editor.inputs = splitLines(value); },
    },
    outputText: {
      get() { return (this.editor?.outputs || []).join('\n'); },
      set(value) { if (this.editor) this.editor.outputs = splitLines(value); },
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    sourceHint() {
      return state.sources.map(item => item.id).join(String.fromCharCode(10));
    },
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/skills');
        this.items = response.items || [];
        this.unavailable = response.unavailable || [];
        this.categories = response.categories || [];
        this.canManage = !!response.can_manage;
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    blank() {
      return {
        id: '', name: '', description: '', category: '数据分析', usage: '',
        instruction: '', triggers: [], example_questions: [], allowed_tools: [],
        inputs: [], outputs: [], notes: '', status: 'draft',
      };
    },
    create() {
      this.editor = { ...this.blank(), isNew: true };
      this.section = 'basic';
    },
    async open(skill) {
      this.section = 'basic';
      this.testResult = null;
      this.testQuestion = (skill.example_questions || [])[0] || '';
      try {
        const response = await actions.get(`/api/skills/${skill.id}`);
        this.editor = { ...response.item, isNew: false };
      } catch (error) {
        toast(error.message, '无法打开技能', 'error');
      }
    },
    async save({ silent = false } = {}) {
      this.saving = true;
      try {
        const response = this.editor.isNew
          ? await actions.post('/api/skills', this.editor)
          : await actions.patch(`/api/skills/${this.editor.id}`, this.editor);
        this.editor = { ...response.item, isNew: false };
        if (!silent) toast('技能已保存', '完成');
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
        await actions.post(`/api/skills/${this.editor.id}/publish`);
        toast('技能已发布，可以被智能体绑定', '完成');
        await this.load();
        await this.open({ id: this.editor.id });
      } catch (error) {
        toast(error.message, '发布失败', 'error');
      }
    },
    async clone(skill) {
      try {
        await actions.post(`/api/skills/${skill.id}/clone`, { id: `${skill.id}-copy` });
        toast('已克隆为工作空间技能', '完成');
        await this.load();
      } catch (error) {
        toast(error.message, '克隆失败', 'error');
      }
    },
    async disable(skill) {
      try {
        await actions.patch(`/api/skills/${skill.id}`, { status: 'disabled' });
        await this.load();
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      }
    },
    async generate() {
      try {
        const response = await actions.post('/api/skills/generate', { description: this.aiPrompt });
        this.aiDraft = response.draft;
      } catch (error) {
        toast(error.message, '生成失败', 'error');
      }
    },
    adoptDraft() {
      this.editor = {
        ...this.blank(),
        ...this.aiDraft,
        triggers: [...(this.aiDraft.triggers || [])],
        example_questions: [...(this.aiDraft.example_questions || [])],
        allowed_tools: [...(this.aiDraft.allowed_tools || [])],
        isNew: true,
      };
      this.aiOpen = false;
      this.aiDraft = null;
      this.aiPrompt = '';
    },
    async test() {
      this.testing = true;
      try {
        this.testResult = await actions.post(`/api/skills/${this.editor.id}/test`, {
          question: this.testQuestion,
        });
      } catch (error) {
        toast(error.message, '测试失败', 'error');
      } finally {
        this.testing = false;
      }
    },
    async exportPackage(skill) {
      try {
        await actions.download(`/api/skills/${skill.id}/export`, `skill-${skill.id}.zip`);
      } catch (error) {
        toast(error.message, '导出失败', 'error');
      }
    },
    async importPackage(event) {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      try {
        const response = await fetch('/api/skills/import', {
          method: 'POST',
          headers: {
            'X-Workspace-Id': state.workspaceId,
            'X-CSRF-Token': sessionStorage.getItem('shuqing-csrf') || '',
          },
          body: form,
        });
        const payload = await response.json();
        if (!response.ok || payload.ok === false) throw new Error(payload.error || '导入失败');
        toast('技能已导入为草稿', '完成');
        await this.load();
      } catch (error) {
        toast(error.message, '导入失败', 'error');
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">技能</h1>
          <p class="page-head__desc">
            技能是 Agent 可以自动发现并执行的专业能力。它决定智能体会做什么，
            而不是把每个细碎动作都拆成独立技能。
          </p>
        </div>
        <div v-if="canManage" class="page-head__actions">
          <label class="btn btn--sm">
            <Icon name="upload" :size="14" />导入
            <input type="file" accept=".zip" hidden @change="importPackage" />
          </label>
          <button class="btn btn--sm" @click="aiOpen = true"><Icon name="sparkle" :size="14" />描述生成草稿</button>
          <button class="btn btn--primary btn--sm" @click="create"><Icon name="plus" :size="14" />新建技能</button>
        </div>
      </header>

      <div class="toolbar">
        <SearchInput v-model="query" placeholder="搜索技能" style="width:240px" />
        <select v-model="category" class="select input--sm" style="width:150px">
          <option value="">全部分类</option>
          <option v-for="item in categories" :key="item" :value="item">{{ item }}</option>
        </select>
        <div class="segmented">
          <button :class="{ active: !statusFilter }" @click="statusFilter = ''">全部</button>
          <button :class="{ active: statusFilter === 'published' }" @click="statusFilter = 'published'">已发布</button>
          <button :class="{ active: statusFilter === 'draft' }" @click="statusFilter = 'draft'">草稿</button>
          <button :class="{ active: statusFilter === 'disabled' }" @click="statusFilter = 'disabled'">已停用</button>
        </div>
      </div>

      <div v-if="loading" class="grid grid--3">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:180px"></div>
      </div>

      <EmptyState v-else-if="!filtered.length" icon="layers" title="没有匹配的技能"
                  text="调整筛选条件，或新建一个技能。" />

      <div v-else class="grid grid--3">
        <article v-for="skill in filtered" :key="skill.id" class="card card--interactive file-tile">
          <div class="row row--between">
            <span class="agent-card__mark" style="width:34px;height:34px"><Icon name="layers" :size="17" /></span>
            <Status :status="skill.status" />
          </div>
          <div>
            <b>{{ skill.name }}</b>
            <p class="small muted" style="margin-top:4px;line-height:1.6">{{ skill.description || '暂无描述' }}</p>
          </div>
          <div class="tag-row">
            <span class="badge">{{ skill.category }}</span>
            <span class="badge badge--brand">{{ skill.source === 'builtin' ? '内置' : '自定义' }}</span>
            <span class="badge">v{{ skill.version }}</span>
          </div>
          <div class="row" style="margin-top:auto">
            <button class="btn btn--sm" @click="open(skill)">查看</button>
            <button v-if="canManage && skill.source !== 'builtin'" class="btn btn--sm" @click="clone(skill)">克隆</button>
            <span class="grow"></span>
            <button class="icon-btn" aria-label="导出" @click="exportPackage(skill)"><Icon name="download" :size="15" /></button>
          </div>
        </article>
      </div>

      <div v-if="unavailable.length" class="card" style="margin-top:18px">
        <h3 class="card__title" style="margin-bottom:8px">暂不可用的技能</h3>
        <p class="small muted" style="margin-bottom:10px">这些技能声明了当前工作空间没有的资源，所以不会出现在用户的选择列表里。</p>
        <div class="tag-row">
          <span v-for="item in unavailable" :key="item.id" class="badge" :data-tip="item.unavailable_reason">
            {{ item.name }} · {{ item.unavailable_reason }}
          </span>
        </div>
      </div>
    </div>

    <Modal :open="aiOpen" title="用一句话创建技能" @close="aiOpen = false">
      <p class="small muted" style="margin-bottom:12px">
        描述这个技能要完成什么任务，系统会生成名称、说明、触发场景、示例问题和所需能力，保存前你可以自由修改。
      </p>
      <textarea v-model="aiPrompt" class="textarea" rows="4"
                placeholder="创建一个门店经营分析技能，需要分析销售额、订单量、客单价、库存以及同比环比，并输出主要问题和经营建议。"></textarea>

      <div v-if="aiDraft" class="card" style="margin-top:14px">
        <div class="card__head">
          <div class="grow">
            <h2 class="card__title">{{ aiDraft.name }}</h2>
            <p class="card__hint">{{ aiDraft.category }}</p>
          </div>
        </div>
        <p class="small muted">{{ aiDraft.description }}</p>
        <div class="tag-row" style="margin-top:10px">
          <span v-for="item in aiDraft.triggers.slice(0, 6)" :key="item" class="badge">{{ item }}</span>
        </div>
        <p class="xs faint" style="margin-top:10px">
          将使用 {{ aiDraft.allowed_tools.length }} 个工具：{{ aiDraft.allowed_tools.slice(0, 5).join('、') }}
        </p>
        <p class="xs faint">载入后可以在编辑器里逐项修改，确认无误再保存。</p>
      </div>

      <template #footer>
        <button class="btn" @click="aiOpen = false">取消</button>
        <button v-if="aiDraft" class="btn" @click="aiDraft = null; generate()">重新生成</button>
        <button v-else class="btn btn--primary" :disabled="aiPrompt.trim().length < 8" @click="generate">
          生成草稿
        </button>
        <button v-if="aiDraft" class="btn btn--primary" @click="adoptDraft">载入到编辑器</button>
      </template>
    </Modal>

    <Modal :open="!!editor" :title="editor ? (editor.isNew ? '新建技能' : editor.name) : ''" wide
           @close="editor = null">
      <div v-if="editor" class="builder">
        <nav class="builder__rail">
          <button v-for="tab in sectionTabs" :key="tab.key" class="admin__nav-item"
                  :class="{ active: section === tab.key }" @click="section = tab.key">
            <Icon :name="tab.icon" :size="15" />{{ tab.label }}
          </button>
        </nav>

        <div class="stack">
          <template v-if="section === 'basic'">
            <label class="field"><span>名称<em> *</em></span>
              <input v-model.trim="editor.name" class="input" placeholder="门店经营分析" /></label>
            <label class="field"><span>描述<em> *</em></span>
              <textarea v-model.trim="editor.description" class="textarea" rows="2"
                        placeholder="一句话说明这个技能解决什么问题，Agent 靠它决定何时使用。"></textarea></label>
            <label class="field"><span>分类</span>
              <select v-model="editor.category" class="select">
                <option v-for="item in categories" :key="item" :value="item">{{ item }}</option>
              </select></label>
            <label class="field"><span>使用说明</span>
              <textarea v-model.trim="editor.instruction" class="textarea" rows="10"
                        placeholder="写给执行这个技能的模型：先确认口径，再取数，再下结论；区分事实与推测。"></textarea></label>
            <p class="xs faint">这段说明会进入模型上下文，请写清执行顺序和硬性要求，而不是口号。</p>
          </template>

          <template v-else-if="section === 'triggers'">
            <label class="field"><span>触发场景（每行一个）</span>
              <textarea v-model="triggerText" class="textarea" rows="5"
                        placeholder="门店&#10;经营&#10;库存"></textarea></label>
            <label class="field"><span>示例问题（每行一个）</span>
              <textarea v-model="exampleText" class="textarea" rows="5"
                        placeholder="哪家门店经营最差？&#10;本月库存周转怎么样？"></textarea></label>
            <p class="xs faint">触发场景和示例问题决定系统能不能自动选中这个技能，写得越具体越好。</p>
          </template>

          <template v-else-if="section === 'resources'">
            <label class="field"><span>可使用数据（数据源 ID，每行一个）</span>
              <textarea v-model="resourceText" class="textarea" rows="4"
                        :placeholder="sourceHint"></textarea></label>
            <p class="xs faint">留空表示不限定，技能会使用本次对话范围内的数据。填了 ID 则必须对这些数据有权限才能使用。</p>
            <label class="field"><span>可使用 MCP（服务 ID，每行一个）</span>
              <textarea v-model="mcpText" class="textarea" rows="3"></textarea></label>
          </template>

          <template v-else-if="section === 'io'">
            <label class="field"><span>可使用工具（每行一个）</span>
              <textarea v-model="toolText" class="textarea" rows="8"
                        placeholder="get_schema&#10;list_semantic_metrics&#10;query_metric&#10;query_data&#10;run_analysis&#10;generate_chart&#10;validate_result"></textarea></label>
            <label class="field"><span>输入（每行一个）</span>
              <textarea v-model="inputText" class="textarea" rows="3"></textarea></label>
            <label class="field"><span>输出（每行一个）</span>
              <textarea v-model="outputText" class="textarea" rows="4"
                        placeholder="结论&#10;支撑数据&#10;图表"></textarea></label>
          </template>

          <template v-else>
            <label class="field"><span>备注</span>
              <textarea v-model.trim="editor.notes" class="textarea" rows="4"></textarea></label>
            <dl class="definition">
              <dt>来源</dt><dd>{{ editor.source === 'builtin' ? '内置技能包' : '工作空间' }}</dd>
              <dt>版本</dt><dd>v{{ editor.version }}</dd>
              <dt>状态</dt><dd>{{ editor.status }}</dd>
            </dl>
          </template>
        </div>

        <aside class="builder__test">
          <h3 class="card__title" style="margin-bottom:6px">配置与命中测试</h3>
          <p class="xs faint" style="margin-bottom:10px">用一个问题验证这个技能能不能被正确选中。</p>
          <div class="stack" style="display:flex;flex-direction:column;gap:10px">
            <textarea v-model="testQuestion" class="textarea" rows="3" placeholder="输入一个测试问题"></textarea>
            <button class="btn btn--primary btn--sm" :disabled="testing || editor.isNew" @click="test">
              <Icon name="play" :size="14" />{{ testing ? '测试中…' : '校验配置' }}
            </button>
            <p v-if="editor.isNew" class="xs faint">先保存技能，才能运行测试。</p>

            <div v-if="testResult" class="stack" style="display:flex;flex-direction:column;gap:8px">
              <span class="badge" :class="testResult.evaluation.passed ? 'badge--success' : 'badge--danger'">
                {{ testResult.evaluation.passed ? '校验通过' : '校验未通过' }}
              </span>
              <div v-for="check in testResult.evaluation.checks" :key="check.name" class="row" style="gap:6px">
                <Icon :name="check.passed ? 'check' : 'close'" :size="13"
                      :style="check.passed ? 'color:var(--success)' : 'color:var(--danger)'" />
                <span class="small">{{ check.name }}</span>
                <span v-if="check.detail" class="xs faint">{{ check.detail }}</span>
              </div>
              <div v-if="testResult.resolution">
                <div class="small muted" style="margin:6px 0 4px">这次会选中</div>
                <span v-for="item in testResult.resolution.selected" :key="item.id" class="badge badge--brand">
                  {{ item.name }}
                </span>
              </div>
            </div>
          </div>
        </aside>
      </div>

      <template #footer>
        <button class="btn" @click="editor = null">关闭</button>
        <button v-if="editor?.isNew || editor?.editable" class="btn" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
        <button v-if="!editor?.isNew && editor?.editable && editor?.status !== 'published'"
                class="btn btn--primary" @click="publish">发布</button>
      </template>
    </Modal>`,
};
