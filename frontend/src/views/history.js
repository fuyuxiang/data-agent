/** 完整对话历史：服务端按权限搜索、分页，最近列表不再是唯一入口。 */
import { Icon } from '../components/icons.js';
import { EmptyState, SearchInput } from '../components/ui.js';
import { actions, formatTime, state } from '../store.js';
import { navigate } from '../router.js';

export const HistoryView = {
  name: 'HistoryView',
  components: { EmptyState, Icon, SearchInput },
  setup() { return { formatTime }; },
  data() {
    return {
      query: state.routeParams.q || '',
      page: Math.max(1, Number(state.routeParams.page) || 1),
      items: [], total: 0, hasMore: false, loading: true, error: '', disposed: false,
    };
  },
  mounted() { this.load(); },
  beforeUnmount() { this.disposed = true; },
  methods: {
    async load() {
      this.loading = true;
      this.error = '';
      try {
        const params = new URLSearchParams({ q: state.routeParams.q || '', page: this.page, limit: 30 });
        const result = await actions.get(`/api/sessions?${params}`);
        if (this.disposed) return;
        this.items = result.items || [];
        this.total = result.total || 0;
        this.hasMore = !!result.has_more;
      } catch (error) {
        if (!this.disposed) this.error = error.message;
      } finally {
        if (!this.disposed) this.loading = false;
      }
    },
    search() {
      const q = this.query.trim();
      if (q === (state.routeParams.q || '') && this.page === 1) this.load();
      else navigate('history', q ? { q } : {});
    },
    turnPage(page) {
      navigate('history', { ...(state.routeParams.q ? { q: state.routeParams.q } : {}), page });
    },
    open(item) {
      // Old conversations need not be present in the bootstrap's recent window.
      if (!state.sessions.some(session => session.id === item.id)) state.sessions.push(item);
      state.activeSessionId = item.id;
      navigate('conversation', { id: item.id });
    },
  },
  template: `
    <div class="view--page"><div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">全部对话</h1>
          <p class="page-head__desc">查找以前的分析，打开后继续提问。这里只显示你有权访问的对话。</p>
        </div>
      </header>
      <form class="toolbar" @submit.prevent="search">
        <SearchInput v-model="query" placeholder="搜索对话名称" />
        <button class="btn btn--primary" type="submit" :disabled="loading">搜索</button>
      </form>
      <div v-if="loading" class="stack" role="status" aria-label="正在加载对话">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:68px"></div>
      </div>
      <div v-else-if="error" class="card" role="alert">
        <p class="small" style="color:var(--danger)">{{ error }}</p>
        <button class="btn btn--sm" @click="load">重新加载</button>
      </div>
      <EmptyState v-else-if="!items.length" icon="chat" :title="query ? '没有匹配的对话' : '暂无对话'"
                  :text="query ? '换一个名称关键词搜索，或清空关键词查看全部对话。' : '在工作台提出问题后，可以在这里找回分析。'" />
      <div v-else class="stack">
        <button v-for="item in items" :key="item.id" class="card history-item" @click="open(item)">
          <Icon name="chat" :size="17" />
          <span class="grow" style="min-width:0">
            <b>{{ item.name || '新对话' }}</b>
            <small class="muted">{{ formatTime(item.updated_at || item.created_at) }}</small>
          </span>
          <Icon name="arrowRight" :size="16" />
        </button>
      </div>
      <nav v-if="!loading && !error && total" class="toolbar" style="margin-top:16px" aria-label="对话分页">
        <span class="small muted" role="status">共 {{ total }} 个对话 · 第 {{ page }} 页</span>
        <span class="toolbar__spacer"></span>
        <button class="btn btn--sm" :disabled="page <= 1" @click="turnPage(page - 1)">上一页</button>
        <button class="btn btn--sm" :disabled="!hasMore" @click="turnPage(page + 1)">下一页</button>
      </nav>
    </div></div>`,
};
