/**
 * 知识：文档、业务规则、指标解释与背景知识。
 *
 * 知识不是"越多越好"，而是让 Agent 在正确的时刻想起正确的业务约定。
 */

import { Icon } from '../components/icons.js';
import { EmptyState, Modal, SearchInput, Status, Switch, Tabs } from '../components/ui.js';
import { actions, canAdmin, state, toast } from '../store.js';

const ENTRY_GROUPS = [
  { type: 'metric', title: '指标解释' },
  { type: 'business_rule', title: '业务规则' },
  { type: 'context_note', title: '背景知识' },
];

export const KnowledgeView = {
  name: 'KnowledgeView',
  components: { EmptyState, Icon, Modal, SearchInput, Status, Switch, Tabs },
  setup() {
    return { canAdmin, state, toast };
  },
  data() {
    return {
      tab: 'structured',
      entries: [],
      documents: [],
      searchQuery: '',
      results: [],
      searching: false,
      searched: false,
      searchError: '',
      searchVersion: 0,
      editor: null,
      saving: false,
      uploading: false,
      uploadOpen: false,
      entryDeleteTarget: null,
      deletingEntry: false,
      entryDeleteError: '',
      changingEntries: [],
      changingDocuments: [],
      documentDeleteTarget: null,
      deletingDocument: false,
      documentDeleteError: '',
      loadingDocumentReferences: false,
      documentReferencesChecked: false,
      documentReferenceVersion: 0,
      loading: true,
      loadError: '',
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
  beforeUnmount() {
    this.searchVersion += 1;
    this.documentReferenceVersion += 1;
  },
  watch: {
    searchQuery() {
      this.invalidateSearch();
    },
  },
  methods: {
    invalidateSearch(documentId = null) {
      this.searchVersion += 1;
      this.searching = false;
      this.searched = false;
      this.searchError = '';
      this.results = documentId
        ? this.results.filter(item => item.document_id !== documentId)
        : [];
    },
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const [entries, documents] = await Promise.all([
          actions.get('/api/knowledge/entries'),
          actions.get('/api/knowledge/documents'),
        ]);
        this.entries = entries.items || [];
        this.documents = documents.items || [];
      } catch (error) {
        this.loadError = error.message;
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
      if (!this.editor || this.saving || !canAdmin.value) return;
      if (!this.editor.name?.trim()) {
        toast('请填写知识条目标题', '信息不完整', 'error');
        return;
      }
      this.saving = true;
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/knowledge/entries/${this.editor.id}`, this.editor)
          : await actions.post('/api/knowledge/entries', this.editor);
        this.entries = this.entries
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.invalidateSearch(response.item.id);
        this.editor = null;
        toast('知识条目已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    async toggle(entry) {
      if (!canAdmin.value || this.changingEntries.includes(entry.id) || this.deletingEntry) return;
      this.changingEntries.push(entry.id);
      try {
        const response = await actions.patch(`/api/knowledge/entries/${encodeURIComponent(entry.id)}`, {
          enabled: entry.enabled === false,
        });
        Object.assign(entry, response.item);
        this.invalidateSearch(entry.id);
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      } finally {
        this.changingEntries = this.changingEntries.filter(id => id !== entry.id);
      }
    },
    openEntryDelete(entry) {
      if (!canAdmin.value || this.deletingEntry || this.changingEntries.includes(entry.id)) return;
      this.entryDeleteTarget = entry;
      this.entryDeleteError = '';
    },
    closeEntryDelete() {
      if (!this.deletingEntry) this.entryDeleteTarget = null;
    },
    async removeEntry() {
      const entry = this.entryDeleteTarget;
      if (!entry || !canAdmin.value || this.deletingEntry) return;
      this.deletingEntry = true;
      this.entryDeleteError = '';
      try {
        await actions.remove(`/api/knowledge/entries/${encodeURIComponent(entry.id)}`);
        this.entries = this.entries.filter(item => item.id !== entry.id);
        this.invalidateSearch(entry.id);
        this.entryDeleteTarget = null;
        toast('知识条目已移入回收站', '完成');
      } catch (error) {
        this.entryDeleteError = error.message;
      } finally {
        this.deletingEntry = false;
      }
    },
    async toggleDocument(document) {
      if (!canAdmin.value || this.changingDocuments.includes(document.id) || this.deletingDocument) return;
      this.changingDocuments.push(document.id);
      try {
        const response = await actions.patch(`/api/knowledge/documents/${encodeURIComponent(document.id)}`, {
          enabled: document.enabled === false,
        });
        Object.assign(document, response.item);
        this.invalidateSearch(document.id);
      } catch (error) {
        if (error.payload?.references) document.references = error.payload.references;
        const names = (error.payload?.references || []).map(item => item.name).join('、');
        toast(names ? `${error.message}：${names}` : error.message, '操作失败', 'error');
      } finally {
        this.changingDocuments = this.changingDocuments.filter(id => id !== document.id);
      }
    },
    async openDocumentDelete(document) {
      if (!canAdmin.value || this.deletingDocument || this.changingDocuments.includes(document.id)) return;
      this.documentDeleteTarget = document;
      this.documentDeleteError = '';
      await this.loadDocumentReferences();
    },
    async loadDocumentReferences() {
      const document = this.documentDeleteTarget;
      if (!document || this.deletingDocument) return;
      const version = ++this.documentReferenceVersion;
      this.loadingDocumentReferences = true;
      this.documentReferencesChecked = false;
      this.documentDeleteError = '';
      try {
        const response = await actions.get(`/api/knowledge/documents/${encodeURIComponent(document.id)}/references`);
        if (version !== this.documentReferenceVersion) return;
        document.references = response.references || [];
        this.documentReferencesChecked = true;
      } catch (error) {
        if (version === this.documentReferenceVersion) this.documentDeleteError = error.message;
      } finally {
        if (version === this.documentReferenceVersion) this.loadingDocumentReferences = false;
      }
    },
    referenceScopes(reference) {
      return (reference.scopes || []).map(scope => scope === 'published' ? '已发布版本' : '草稿配置').join('、');
    },
    closeDocumentDelete() {
      if (!this.deletingDocument) {
        this.documentReferenceVersion += 1;
        this.loadingDocumentReferences = false;
        this.documentDeleteTarget = null;
      }
    },
    async removeDocument() {
      const document = this.documentDeleteTarget;
      if (!document || !canAdmin.value || this.deletingDocument || !this.documentReferencesChecked
          || this.loadingDocumentReferences || document.references?.length) return;
      this.deletingDocument = true;
      this.documentDeleteError = '';
      try {
        await actions.remove(`/api/knowledge/documents/${encodeURIComponent(document.id)}`);
        this.documents = this.documents.filter(item => item.id !== document.id);
        this.invalidateSearch(document.id);
        this.documentDeleteTarget = null;
        toast('知识文档已移入回收站', '完成');
      } catch (error) {
        if (error.payload?.references) document.references = error.payload.references;
        this.documentDeleteError = error.message;
      } finally {
        this.deletingDocument = false;
      }
    },
    async search() {
      if (!this.searchQuery.trim() || this.searching) return;
      const version = ++this.searchVersion;
      this.searching = true;
      this.results = [];
      this.searched = false;
      this.searchError = '';
      try {
        const response = await actions.post('/api/knowledge/search', { query: this.searchQuery });
        if (version === this.searchVersion) {
          this.results = response.items || [];
          this.searched = true;
        }
      } catch (error) {
        if (version === this.searchVersion) this.searchError = error.message;
      } finally {
        if (version === this.searchVersion) this.searching = false;
      }
    },
    async upload(event) {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file || this.uploading || !canAdmin.value) return;
      this.uploading = true;
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
        this.invalidateSearch();
        this.uploadOpen = false;
        toast('文档已索引', '完成');
      } catch (error) {
        toast(error.message, '导入失败', 'error');
      } finally {
        this.uploading = false;
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
        <div v-if="canAdmin" class="page-head__actions">
          <label class="btn btn--sm">
            <Icon name="upload" :size="14" />{{ uploading ? '导入中…' : '导入文档' }}
            <input type="file" hidden :disabled="uploading" accept=".txt,.md,.html,.csv,.json,.pdf,.docx,.xlsx,.xls" @change="upload" />
          </label>
        </div>
      </header>

      <div v-if="loadError" class="card stack"><p class="small" role="alert" style="color:var(--danger)">{{ loadError }}</p><button class="btn btn--sm" @click="load">重新加载知识</button></div>
      <div v-else-if="loading" class="grid grid--3"><div v-for="index in 3" :key="index" class="skeleton" style="height:200px"></div></div>
      <template v-else>
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
            <button v-if="canAdmin" class="btn btn--sm" @click="newEntry(group.type)"><Icon name="plus" :size="14" />新增</button>
          </div>
          <div class="stack" style="display:flex;flex-direction:column;gap:8px">
            <article v-for="item in group.items" :key="item.id" class="knowledge-entry">
              <div class="row row--between" style="align-items:flex-start">
                <div class="grow">
                  <b>{{ entryTitle(item) }}</b>
                  <p class="small muted" style="margin-top:4px;line-height:1.6">{{ entrySummary(item) }}</p>
                  <div v-if="item.alias || item.rule_id" class="tag-row" style="margin-top:6px">
                    <span v-if="item.alias" class="badge">{{ item.alias }}</span>
                    <span v-if="item.rule_id" class="badge badge--brand">{{ item.rule_id }}</span>
                  </div>
                </div>
              </div>
                <div v-if="canAdmin" class="row knowledge-entry__actions" style="gap:4px">
                  <Switch :model-value="item.enabled !== false" :label="'启用 ' + entryTitle(item)"
                          :disabled="deletingEntry || changingEntries.includes(item.id)"
                          @update:model-value="toggle(item)" />
                  <button class="icon-btn" aria-label="编辑" @click="edit(item)"><Icon name="edit" :size="15" /></button>
                  <button class="icon-btn icon-btn--danger" :aria-label="'删除条目 ' + entryTitle(item)"
                          :disabled="deletingEntry || changingEntries.includes(item.id)" @click="openEntryDelete(item)">
                    <Icon name="trash" :size="15" />
                  </button>
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
              <p v-if="item.references?.length" class="small muted">被 {{ item.references.length }} 个智能体引用，解除引用后可删除或停用。</p>
            </div>
          </div>
          <div class="row">
            <Switch v-if="canAdmin" :model-value="item.enabled !== false" :label="'启用文档 ' + item.name"
                    :disabled="deletingDocument || changingDocuments.includes(item.id)"
                    @update:model-value="toggleDocument(item)" />
            <Status :status="item.enabled === false ? 'disabled' : 'ready'" />
            <button v-if="canAdmin" class="btn btn--sm" style="color:var(--danger)"
                    :aria-label="'删除文档 ' + item.name" :disabled="deletingDocument || changingDocuments.includes(item.id)"
                    @click="openDocumentDelete(item)">
              <Icon name="trash" :size="14" />删除
            </button>
          </div>
        </article>
      </div>

      <div v-else class="card" style="margin-top:18px">
        <h2 class="card__title" style="margin-bottom:4px">检索测试</h2>
        <p class="card__hint" style="margin-bottom:12px">检查 Agent 能不能召回正确的业务口径。</p>
        <div class="row" style="margin-bottom:14px">
          <input v-model="searchQuery" class="input" style="flex:1"
                 placeholder="例如：GMV 的计算口径是什么？" @keydown.enter="!$event.isComposing && $event.keyCode !== 229 && search()" />
          <button class="btn btn--primary" :disabled="searching || !searchQuery.trim()" @click="search">
            {{ searching ? '检索中…' : '检索' }}
          </button>
        </div>
        <p v-if="searching" class="small muted" role="status">正在检索当前问题…</p>
        <p v-else-if="searchError" class="small" role="alert" style="color:var(--danger)">{{ searchError }}，可重新检索。</p>
        <EmptyState v-else-if="!results.length" icon="search" :title="searched ? '没有匹配的知识片段' : '还没有检索结果'"
                    :text="searched ? '尝试更具体的业务词语，或确认相关知识已启用。' : '输入一个问题，看看会召回哪些知识片段。'" />
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
      </template>
    </div>

    <Modal :open="!!entryDeleteTarget" title="删除知识条目" size="small" @close="closeEntryDelete">
      <p v-if="entryDeleteTarget" class="small">
        确定删除知识条目「{{ entryTitle(entryDeleteTarget) }}」吗？后续检索将不再使用它，
        已完成的分析记录会保留。可到 <a href="#/admin/trash?collection=knowledge_entries">回收站</a> 恢复。
      </p>
      <p v-if="entryDeleteError" class="small" style="color:var(--danger)">{{ entryDeleteError }}</p>
      <template #footer>
        <button class="btn" :disabled="deletingEntry" @click="closeEntryDelete">取消</button>
        <button class="btn btn--primary" :disabled="deletingEntry" @click="removeEntry">
          {{ deletingEntry ? '删除中…' : '确认删除' }}
        </button>
      </template>
    </Modal>

    <Modal :open="!!documentDeleteTarget" title="删除知识文档" size="small" @close="closeDocumentDelete">
      <p v-if="documentDeleteTarget" class="small">
        确定删除知识文档「{{ documentDeleteTarget.name }}」吗？文档将移入回收站，
        后续知识检索将不再使用它，已完成的分析记录会保留。
        可到 <a href="#/admin/trash?collection=knowledge_documents">回收站</a> 恢复。
      </p>
      <p v-if="loadingDocumentReferences" class="small muted">正在检查智能体引用…</p>
      <div v-if="documentDeleteTarget?.references?.length" class="stack" style="margin-top:12px">
        <p class="small">请先解除以下智能体的引用。已发布版本的引用需要修改配置并重新发布后才会解除。</p>
        <p v-for="(reference, index) in documentDeleteTarget.references" :key="reference.id || index" class="small">
          <b>{{ reference.name }}</b> · {{ referenceScopes(reference) }}
          <span v-if="reference.private">（请联系其创建者解除绑定）</span>
        </p>
        <a href="#/admin/agents" class="small">管理智能体引用</a>
      </div>
      <p v-if="documentDeleteError" class="small" style="color:var(--danger)">{{ documentDeleteError }}</p>
      <template #footer>
        <button class="btn" :disabled="deletingDocument" @click="closeDocumentDelete">取消</button>
        <button class="btn" :disabled="deletingDocument || loadingDocumentReferences" @click="loadDocumentReferences">刷新引用</button>
        <button class="btn btn--primary"
                :disabled="deletingDocument || loadingDocumentReferences || !documentReferencesChecked || !!documentDeleteTarget?.references?.length"
                @click="removeDocument">
          {{ deletingDocument ? '删除中…' : '确认删除' }}
        </button>
      </template>
    </Modal>

    <Modal :open="!!editor" :title="editor?.id ? '编辑知识条目' : '新增知识条目'" @close="!saving && (editor = null)">
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
        <button class="btn" :disabled="saving" @click="editor = null">取消</button>
        <button class="btn btn--primary" :disabled="saving || !editor?.name?.trim()" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>`,
};
