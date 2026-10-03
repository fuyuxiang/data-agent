/**
 * 资料库：报告、PPT、表格、网页、图片与上传文件的统一入口。
 *
 * 内部叫 Artifact，用户端叫资料库——行业成熟术语，不额外创造概念。
 * 每个文件都能预览、下载、收藏、删除，并能"围绕该文件继续提问"。
 */

import { Icon, iconForCategory } from '../components/icons.js';
import { EmptyState, Modal, SearchInput } from '../components/ui.js';
import { actions, formatDate, formatSize, state, toast } from '../store.js';
import { navigate } from '../router.js';

const { computed } = Vue;

export const LibraryView = {
  name: 'LibraryView',
  components: { EmptyState, Icon, Modal, SearchInput },
  setup() {
    return { navigate, state, toast, formatDate, formatSize };
  },
  data() {
    return {
      items: [],
      categories: [],
      category: '全部',
      query: '',
      loading: true,
      preview: null,
      rename: null,
      renameValue: '',
      menuFor: '',
      uploading: false,
    };
  },
  computed: {
    canAnalyze() {
      return ['owner', 'editor', 'analyst'].includes(state.workspaceRole);
    },
    filtered() {
      const keyword = this.query.trim().toLowerCase();
      return this.items.filter(item => (this.category === '全部' || item.category === this.category)
        && (!keyword || item.title.toLowerCase().includes(keyword)
          || (item.filename || '').toLowerCase().includes(keyword)));
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/library');
        this.items = response.items || [];
        this.categories = response.categories || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    iconFor(file) {
      return iconForCategory(file.category, file.kind);
    },
    select(category) {
      this.category = category;
    },
    async download(file) {
      try {
        await actions.download(file.download_url, file.filename || file.title);
      } catch (error) {
        toast(error.message, '下载失败', 'error');
      }
    },
    openRename(file) {
      this.rename = file;
      this.renameValue = file.title;
    },
    async saveRename() {
      const title = this.renameValue.trim();
      if (!title) return;
      try {
        await actions.patch(`/api/library/${this.rename.id}`, { title });
        this.rename.title = title;
        this.rename = null;
        toast('已重命名', '完成');
      } catch (error) {
        toast(error.message, '重命名失败', 'error');
      }
    },
    async toggleFavorite(file) {
      try {
        const response = await actions.patch(`/api/library/${file.id}`, {
          favorite: file.favorite ? '' : file.title,
        });
        file.favorite = response.item.favorite;
        file.title = response.item.title;
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      }
    },
    async remove(file) {
      try {
        await actions.remove(`/api/library/${file.id}`);
        this.items = this.items.filter(item => item.id !== file.id);
        toast('已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      }
    },
    canAsk(file) {
      return Boolean(file.run_id) || ['.docx', '.xlsx', '.pdf', '.md', '.txt'].includes(file.extension);
    },
    async askAbout(file) {
      if (!this.canAsk(file)) return;
      if (file.run_id) {
        try {
          const detail = await actions.get(`/api/analyses/${file.run_id}`);
          navigate('conversation', {
            id: detail.item.session_id,
            ask: `围绕「${file.title}」继续追问：`,
          });
        } catch (error) {
          toast(error.message, '无法打开原分析', 'error');
        }
        return;
      }
      navigate('workbench', { file: file.id, ask: `分析「${file.title}」：` });
    },
    async upload(event) {
      const files = Array.from(event.target.files || []);
      event.target.value = '';
      if (!files.length) return;
      if (files.length > 20 || files.some(file => file.size > 50 * 1024 * 1024)) {
        toast('单次最多 20 个文件，每个不超过 50 MB', '无法上传', 'error');
        return;
      }
      this.uploading = true;
      const form = new FormData();
      files.forEach(file => form.append('file', file));
      try {
        const response = await fetch('/api/library', {
          method: 'POST',
          headers: {
            'X-Workspace-Id': state.workspaceId,
            'X-CSRF-Token': sessionStorage.getItem('shuqing-csrf') || '',
          },
          body: form,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || result.message || '上传失败');
        this.items.unshift(...(result.items || [result.item]));
        await this.load();
        toast(`${files.length} 个文件已保存`, '完成');
      } catch (error) {
        toast(error.message, '上传失败', 'error');
      } finally {
        this.uploading = false;
      }
    },
  },
  template: `
    <div class="view--page">
      <div class="view__inner">
        <header class="page-head">
          <div class="grow">
            <h1 class="page-head__title">资料库</h1>
            <p class="page-head__desc">分析产出的报告、演示文稿、表格和图片都在这里，可以预览、下载，并围绕它继续提问。</p>
          </div>
          <div v-if="canAnalyze" class="page-head__actions">
            <label class="btn btn--sm">
              <Icon name="upload" :size="14" />上传文件
              <input type="file" multiple hidden :disabled="uploading" @change="upload" />
            </label>
          </div>
        </header>

        <div class="toolbar">
          <div class="chip-group">
            <button class="chip" :class="{ active: category === '全部' }" @click="select('全部')">
              全部 {{ items.length }}
            </button>
            <button v-for="entry in categories" :key="entry.key" class="chip"
                    :class="{ active: category === entry.key }" @click="select(entry.key)">
              <Icon :name="entry.icon" :size="13" />{{ entry.key }} {{ entry.count }}
            </button>
          </div>
          <span class="toolbar__spacer"></span>
          <SearchInput v-model="query" placeholder="搜索资料" />
        </div>

        <div v-if="loading" class="grid grid--3">
          <div v-for="index in 6" :key="index" class="skeleton" style="height:150px"></div>
        </div>

        <EmptyState v-else-if="!filtered.length" icon="library"
                    :title="query || category !== '全部' ? '没有匹配的资料' : '资料库还是空的'"
                    text="完成一次分析后点击「生成报告 / PPT / Excel」，产出会自动出现在这里。" />

        <div v-else class="file-grid">
          <article v-for="file in filtered" :key="file.id" class="card card--interactive file-tile">
            <div class="row row--between">
              <span class="file-tile__icon"><Icon :name="iconFor(file)" :size="19" /></span>
              <button v-if="canAnalyze" class="icon-btn tip" :data-tip="file.favorite ? '取消收藏' : '收藏'"
                      :class="{ 'icon-btn--active': file.favorite }" @click="toggleFavorite(file)">
                <Icon name="star" :size="16" />
              </button>
            </div>
            <div>
              <div class="file-card__title truncate" :title="file.title">{{ file.title }}</div>
              <div class="file-card__meta">{{ file.category }} · {{ formatSize(file.size_bytes) }}</div>
            </div>
            <div class="xs faint">{{ formatDate(file.created_at) }}</div>
            <div class="row" style="margin-top:auto">
              <button v-if="file.previewable" class="btn btn--sm" @click="preview = file">
                <Icon name="eye" :size="14" />预览
              </button>
              <button v-if="canAnalyze" class="btn btn--sm" @click="download(file)"><Icon name="download" :size="14" />下载</button>
              <button v-if="canAnalyze && canAsk(file)" class="btn btn--sm" @click="askAbout(file)"><Icon name="chat" :size="14" />追问</button>
              <span class="grow"></span>
              <div v-if="canAnalyze" class="dropdown">
                <button class="icon-btn" :class="{ 'icon-btn--active': menuFor === file.id }"
                        aria-label="更多" @click="menuFor = menuFor === file.id ? '' : file.id">
                  <Icon name="sort" :size="16" />
                </button>
                <div v-if="menuFor === file.id" class="dropdown__menu">
                  <button @click="openRename(file); menuFor = ''"><Icon name="edit" :size="14" />重命名</button>
                  <button class="danger" @click="remove(file)"><Icon name="trash" :size="14" />删除</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>
    </div>

    <Modal :open="!!preview" :title="preview?.title || '预览'" wide @close="preview = null">
      <iframe v-if="preview" class="preview-frame" :src="'/api/library/' + preview.id + '/preview'"
              :title="preview.title"></iframe>
    </Modal>

    <Modal :open="!!rename" title="重命名" @close="rename = null">
      <label class="field">
        <span>名称</span>
        <input v-model.trim="renameValue" class="input" maxlength="120" @keyup.enter="saveRename" />
      </label>
      <template #footer>
        <button class="btn" @click="rename = null">取消</button>
        <button class="btn btn--primary" @click="saveRename">保存</button>
      </template>
    </Modal>`,
};
