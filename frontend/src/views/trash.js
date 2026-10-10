/** 可恢复的删除入口：个人分析与有权限管理的数据、知识、文件、连接。 */
import { Icon } from '../components/icons.js';
import { EmptyState, Modal, SearchInput } from '../components/ui.js';
import { actions, bootstrap, formatDate, state, toast } from '../store.js';
import { navigate } from '../router.js';

const COLLECTION_LABELS = {
  agent_runs: '分析', sessions: '会话', knowledge_documents: '知识文档',
  knowledge_entries: '知识条目', artifacts: '资料', saved_sessions: '保存的会话', sources: '数据源',
  mcp_servers: 'MCP 服务', connectors: '集成连接',
};

export const TrashView = {
  name: 'TrashView',
  components: { EmptyState, Icon, Modal, SearchInput },
  setup() {
    return { formatDate, navigate, state };
  },
  data() {
    return {
      items: [], loading: true, error: '', query: '',
      collection: state.routeParams.collection || '', busy: '', deleteTarget: null, deleteError: '',
    };
  },
  computed: {
    categories() {
      return Object.entries(COLLECTION_LABELS)
        .filter(([key]) => this.items.some(item => item.collection === key) || key === this.collection)
        .map(([key, label]) => ({ key, label }));
    },
    filtered() {
      const query = this.query.trim().toLowerCase();
      return this.items.filter(item => (!this.collection || item.collection === this.collection)
        && (!query || `${item.title} ${this.label(item)}`.toLowerCase().includes(query)));
    },
  },
  mounted() {
    this.load();
  },
  methods: {
    label(item) {
      return COLLECTION_LABELS[item.collection] || '内容';
    },
    key(item) {
      return `${item.collection}:${item.id}`;
    },
    async load() {
      this.loading = true;
      this.error = '';
      try {
        this.items = (await actions.get('/api/trash')).items || [];
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loading = false;
      }
    },
    async restore(item) {
      if (this.busy) return;
      this.busy = this.key(item);
      try {
        const id = encodeURIComponent(item.id);
        await actions.post(item.collection === 'agent_runs'
          ? `/api/analyses/${id}/restore`
          : `/api/trash/${item.collection}/${id}/restore`);
        this.items = this.items.filter(entry => this.key(entry) !== this.key(item));
        if (['sessions', 'sources', 'agent_runs', 'saved_sessions'].includes(item.collection)) await bootstrap({ quiet: true });
        if (['sessions', 'sources'].includes(item.collection)) await this.load();
        toast(`「${item.title}」已恢复`, '恢复成功');
      } catch (error) {
        toast(error.message, '恢复失败', 'error');
      } finally {
        this.busy = '';
      }
    },
    openDelete(item) {
      if (this.busy) return;
      this.deleteTarget = item;
      this.deleteError = '';
    },
    closeDelete() {
      if (!this.busy) this.deleteTarget = null;
    },
    async permanentlyDelete() {
      const item = this.deleteTarget;
      if (!item || this.busy) return;
      this.busy = this.key(item);
      this.deleteError = '';
      try {
        await actions.remove(`/api/trash/${item.collection}/${encodeURIComponent(item.id)}`, { confirm: true });
        this.items = this.items.filter(entry => this.key(entry) !== this.key(item));
        this.deleteTarget = null;
        toast('内容已永久删除', '完成');
      } catch (error) {
        this.deleteError = error.message;
      } finally {
        this.busy = '';
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">回收站</h1>
          <p class="page-head__desc">恢复误删的内容。删除分析不影响资料库成果，执行记录会保留。</p>
        </div>
        <button class="btn btn--sm" :disabled="loading || !!busy" @click="load"><Icon name="refresh" :size="14" />刷新</button>
      </header>
      <div class="toolbar">
        <select v-model="collection" class="select" style="width:150px" aria-label="回收站内容类型">
          <option value="">全部类型</option>
          <option v-for="item in categories" :key="item.key" :value="item.key">{{ item.label }}</option>
        </select>
        <span class="toolbar__spacer"></span>
        <SearchInput v-model="query" placeholder="搜索已删除内容" />
      </div>
      <div v-if="loading" class="stack"><div v-for="index in 3" :key="index" class="skeleton" style="height:68px"></div></div>
      <div v-else-if="error" class="card"><p class="small" style="color:var(--danger)">{{ error }}</p><button class="btn btn--sm" @click="load">重新加载</button></div>
      <EmptyState v-else-if="!filtered.length" icon="trash" :title="query || collection ? '没有匹配的已删除内容' : '没有已删除的内容'" :text="query || collection ? '调整关键词或内容类型后重试。' : '这里只显示你有权限恢复的内容。'" />
      <div v-else class="stack">
        <article v-for="item in filtered" :key="key(item)" class="card row row--between" style="padding:14px 16px">
          <div class="grow" style="min-width:0">
            <b class="small" style="overflow-wrap:anywhere">{{ item.title }}</b>
            <p class="xs muted">{{ label(item) }} · 删除于 {{ formatDate(item.archived_at) }}</p>
            <p v-if="item.restore_block_reason" class="small muted">{{ item.restore_block_reason }}</p>
          </div>
          <div class="row">
            <button v-if="item.can_restore" class="btn btn--sm btn--primary" :disabled="!!busy" @click="restore(item)">
              {{ busy === key(item) ? '处理中…' : '恢复' }}
            </button>
            <button v-if="item.can_delete" class="btn btn--sm" style="color:var(--danger)" :disabled="!!busy" @click="openDelete(item)">永久删除</button>
          </div>
        </article>
      </div>
      <Modal :open="!!deleteTarget" title="永久删除" size="small" @close="closeDelete">
        <p v-if="deleteTarget" class="small">永久删除「{{ deleteTarget.title }}」及其文件？此操作无法恢复。</p>
        <p v-if="deleteError" class="small" style="color:var(--danger)">{{ deleteError }}</p>
        <template #footer>
          <button class="btn" :disabled="!!busy" @click="closeDelete">取消</button>
          <button class="btn btn--danger" :disabled="!!busy" @click="permanentlyDelete">{{ busy ? '删除中…' : '永久删除' }}</button>
        </template>
      </Modal>
    </div>`,
};
