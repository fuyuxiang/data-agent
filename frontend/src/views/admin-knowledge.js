/**
 * 知识：文档、业务规则、指标解释与背景知识。
 *
 * 知识不是"越多越好"，而是让 Agent 在正确的时刻想起正确的业务约定。
 */

import { Icon } from '../components/icons.js';
import { EmptyState, Modal, SearchInput, Status, Switch, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';

const ENTRY_GROUPS = [
  { type: 'metric', title: '指标解释' },
  { type: 'business_rule', title: '业务规则' },
  { type: 'context_note', title: '背景知识' },
];

export const KnowledgeView = {
  name: 'KnowledgeView',
  components: { EmptyState, Icon, Modal, SearchInput, Status, Switch, Tabs },
  setup() {
    return { state, toast };
  },
  data() {
    return {
      tab: 'structured',
      entries: [],
      documents: [],
      searchQuery: '',
      results: [],
      searching: false,
      editor: null,
      uploadOpen: false,
      loading: true,
    };
  },
  computed: {
    groups() {
      return ENTRY_GROUPS.map(group => ({
        ...group,
        items: this.entries.filter(item => item.type === group.type),
      }));
    },
    enabledCount() {
      return this.entries.filter(item => item.enabled !== false).length;
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const [entries, documents] = await Promise.all([
          actions.get('/api/knowledge/entries'),
          actions.get('/api/knowledge/documents'),
        ]);
        this.entries = entries.items || [];
        this.documents = documents.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    entryTitle(item) {
      return item.name || item.definition || item.content || '未命名条目';
    },
    entrySummary(item) {
      return item.definition || item.description || item.content || '暂无内容';
    },
    newEntry(type) {
      this.editor = {
        type,
        name: '',
        definition: type === 'metric' ? '' : undefined,
        description: type === 'business_rule' ? '' : undefined,
        content: type === 'context_note' ? '' : undefined,
        alias: '',
        rule_id: '',
        severity: 'medium',
        topic: '',
        tags: [],
        enabled: true,
      };
    },
    edit(entry) {
      this.editor = { ...entry };
    },
    async save() {
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/knowledge/entries/${this.editor.id}`, this.editor)
          : await actions.post('/api/knowledge/entries', this.editor);
        this.entries = this.entries
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('知识条目已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      }
    },
    async toggle(entry) {
      try {
        await actions.patch(`/api/knowledge/entries/${entry.id}`, { enabled: !entry.enabled });
        entry.enabled = !entry.enabled;
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      }
    },
    async remove(entry) {
      try {
        await actions.remove(`/api/knowledge/entries/${entry.id}`);
        this.entries = this.entries.filter(item => item.id !== entry.id);
        toast('已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      }
    },
    async search() {
      if (!this.searchQuery.trim()) return;
      this.searching = true;
      try {
        const response = await actions.post('/api/knowledge/search', { query: this.searchQuery });
        this.results = response.items || [];
      } catch (error) {
        toast(error.message, '检索失败', 'error');
      } finally {
        this.searching = false;
      }
    },
    async upload(event) {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      form.append('workspace_id', state.workspaceId);
      try {
        const response = await fetch('/api/knowledge/documents', {
          method: 'POST',
          headers: {
            'X-Workspace-Id': state.workspaceId,
            'X-CSRF-Token': sessionStorage.getItem('shuqing-csrf') || '',
          },
          body: form,
        });
        const payload = await response.json();
        if (!response.ok || payload.ok === false) throw new Error(payload.error || '导入失败');
        await this.load();
        this.uploadOpen = false;
        toast('文档已索引', '完成');
      } catch (error) {
        toast(error.message, '导入失败', 'error');
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">知识</h1>
          <p class="page-head__desc">
            沉淀业务口径、规则和背景，让 Agent 不只读懂字段，也读懂业务。
            正式指标口径请到「指标中心」维护。
          </p>
        </div>
        <div class="page-head__actions">
          <label class="btn btn--sm">
            <Icon name="upload" :size="14" />导入文档
            <input type="file" hidden accept=".txt,.md,.html,.csv,.json,.pdf,.docx,.xlsx,.xls" @change="upload" />
          </label>
        </div>
      </header>

      <Tabs v-model="tab" :items="[
        { key: 'structured', label: '结构化知识', count: entries.length },
        { key: 'documents', label: '知识文档', count: documents.length },
        { key: 'search', label: '检索测试' },
      ]" />

      <div v-if="tab === 'structured'" class="grid grid--3" style="margin-top:18px">
        <div v-if="loading" v-for="index in 3" :key="index" class="skeleton" style="height:200px"></div>
        <section v-for="group in groups" v-else :key="group.type" class="card">
          <div class="card__head">
            <div class="grow">
              <h2 class="card__title">{{ group.title }}</h2>
              <p class="card__hint">{{ group.items.length }} 条</p>
            </div>
            <button class="btn btn--sm" @click="newEntry(group.type)"><Icon name="plus" :size="14" />新增</button>
          </div>
          <div class="stack" style="display:flex;flex-direction:column;gap:8px">
            <article v-for="item in group.items" :key="item.id" class="card card--interactive"
                     style="padding:12px 14px">
              <div class="row row--between" style="align-items:flex-start">
                <div class="grow">
                  <b>{{ entryTitle(item) }}</b>
                  <p class="small muted" style="margin-top:4px;line-height:1.6">{{ entrySummary(item) }}</p>
                  <div v-if="item.alias || item.rule_id" class="tag-row" style="margin-top:6px">
                    <span v-if="item.alias" class="badge">{{ item.alias }}</span>
                    <span v-if="item.rule_id" class="badge badge--brand">{{ item.rule_id }}</span>
                  </div>
                </div>
                <div class="row" style="gap:4px">
                  <Switch :model-value="item.enabled !== false" :label="'启用 ' + entryTitle(item)"
                          @update:model-value="toggle(item)" />
                  <button class="icon-btn" aria-label="编辑" @click="edit(item)"><Icon name="edit" :size="15" /></button>
                  <button class="icon-btn icon-btn--danger" aria-label="删除" @click="remove(item)">
                    <Icon name="trash" :size="15" />
                  </button>
                </div>
              </div>
            </article>
            <EmptyState v-if="!group.items.length" icon="book" title="暂无条目"
                        :text="'添加一条' + group.title + '，Agent 在相关问题中会用到它。'" />
          </div>
        </section>
      </div>

      <div v-else-if="tab === 'documents'" class="stack" style="margin-top:18px">
        <EmptyState v-if="!documents.length" icon="book" title="还没有知识文档"
                    text="导入 txt / md / pdf / docx / xlsx，系统会切分并建立索引。" />
        <article v-for="item in documents" v-else :key="item.id" class="card row row--between">
          <div class="row">
            <span class="agent-card__mark" style="width:34px;height:34px"><Icon name="fileText" :size="17" /></span>
            <div>
              <b>{{ item.name }}</b>
              <p class="small muted">{{ item.format }} · {{ item.chunk_count || 0 }} 片段 · {{ item.characters || 0 }} 字符</p>
            </div>
          </div>
          <Status :status="item.enabled === false ? 'disabled' : 'ready'" />
        </article>
      </div>

      <div v-else class="card" style="margin-top:18px">
        <h2 class="card__title" style="margin-bottom:4px">检索测试</h2>
        <p class="card__hint" style="margin-bottom:12px">检查 Agent 能不能召回正确的业务口径。</p>
        <div class="row" style="margin-bottom:14px">
          <input v-model="searchQuery" class="input" style="flex:1"
                 placeholder="例如：GMV 的计算口径是什么？" @keyup.enter="search" />
          <button class="btn btn--primary" :disabled="searching || !searchQuery.trim()" @click="search">
            {{ searching ? '检索中…' : '检索' }}
          </button>
        </div>
        <EmptyState v-if="!results.length" icon="search" title="还没有检索结果"
                    text="输入一个问题，看看会召回哪些知识片段。" />
        <div v-else class="stack" style="display:flex;flex-direction:column;gap:8px">
          <article v-for="(item, index) in results" :key="index" class="card" style="padding:12px 14px">
            <div class="row row--between">
              <b class="small">{{ item.document_name || '知识条目' }}</b>
              <span class="badge">相关度 {{ Math.round((item.score || 0) * 100) }}%</span>
            </div>
            <p class="small muted" style="margin-top:6px;line-height:1.6">{{ item.text }}</p>
          </article>
        </div>
      </div>
    </div>

    <Modal :open="!!editor" :title="editor?.id ? '编辑知识条目' : '新增知识条目'" @close="editor = null">
      <div v-if="editor" class="stack">
        <label class="field"><span>标题<em> *</em></span>
          <input v-model.trim="editor.name" class="input" placeholder="销售额口径" /></label>
        <label v-if="editor.type === 'metric'" class="field"><span>同义词（逗号分隔）</span>
          <input v-model.trim="editor.alias" class="input" placeholder="GMV, 成交额" /></label>
        <label v-if="editor.type === 'metric'" class="field"><span>定义</span>
          <textarea v-model.trim="editor.definition" class="textarea" rows="4"></textarea></label>
        <label v-if="editor.type === 'business_rule'" class="field"><span>规则编号</span>
          <input v-model.trim="editor.rule_id" class="input" placeholder="RULE-001" /></label>
        <label v-if="editor.type === 'business_rule'" class="field"><span>规则说明</span>
          <textarea v-model.trim="editor.description" class="textarea" rows="4"></textarea></label>
        <label v-if="editor.type === 'business_rule'" class="field"><span>严重程度</span>
          <select v-model="editor.severity" class="select">
            <option value="low">低</option><option value="medium">中</option><option value="high">高</option>
          </select></label>
        <label v-if="editor.type === 'context_note'" class="field"><span>主题</span>
          <input v-model.trim="editor.topic" class="input" placeholder="华东经营背景" /></label>
        <label v-if="editor.type === 'context_note'" class="field"><span>内容</span>
          <textarea v-model.trim="editor.content" class="textarea" rows="5"></textarea></label>
        <label class="field"><span>备注</span>
          <input v-model.trim="editor.notes" class="input" /></label>
      </div>
      <template #footer>
        <button class="btn" @click="editor = null">取消</button>
        <button class="btn btn--primary" @click="save">保存</button>
      </template>
    </Modal>`,
};
