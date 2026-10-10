/**
 * 资料库：报告、PPT、表格、网页、图片与上传文件的统一入口。
 *
 * 内部叫 Artifact，用户端叫资料库——行业成熟术语，不额外创造概念。
 * 每个文件都能预览、下载、收藏、删除，并能"围绕该文件继续提问"。
 */

import { Icon, iconForCategory } from '../components/icons.js';
import { DataTable, EmptyState, Modal, SearchInput } from '../components/ui.js';
import { actions, formatDate, formatSize, state, toast } from '../store.js';
import { navigate } from '../router.js';
import { withWorkspace } from '../api.js';
import { renderMarkdown } from '../components/result-blocks.js';

const { computed } = Vue;

/** CSV 预览保留原始字符串、重复表头和带引号的多行字段，不影响下载内容。 */
function csvPreview(value, limit = 200) {
  const text = String(value || '').replace(/^\uFEFF/, '');
  const records = [];
  let row = [], cell = '', quoted = false, recordStarted = false;
  const finishRow = () => {
    row.push(cell);
    // 显式空字段（包括单列的 ""）是一条记录；空白物理行不是。
    if (recordStarted || row.length > 1) records.push(row);
    row = []; cell = ''; recordStarted = false;
  };
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === '"') {
      recordStarted = true;
      if (quoted && text[index + 1] === '"') { cell += '"'; index += 1; }
      else if (quoted || !cell) quoted = !quoted;
      else cell += character;
    } else if (character === ',' && !quoted) {
      recordStarted = true;
      row.push(cell); cell = '';
    } else if ((character === '\n' || character === '\r') && !quoted) {
      finishRow();
      if (character === '\r' && text[index + 1] === '\n') index += 1;
      if (records.length > limit + 1) break;
    } else {
      recordStarted = true;
      cell += character;
    }
  }
  if (quoted) throw new Error('CSV 字段包含未闭合的引号，请下载后检查原文件');
  if (recordStarted || row.length) finishRow();
  const headers = records.shift() || [];
  const width = Math.max(headers.length, ...records.slice(0, limit).map(record => record.length), 0);
  return {
    columns: Array.from({ length: width }, (_, index) => ({ key: `column_${index}`, label: headers[index] || `第 ${index + 1} 列` })),
    rows: records.slice(0, limit).map(record => Object.fromEntries(Array.from({ length: width }, (_, index) => [`column_${index}`, record[index] ?? '']))),
    truncated: records.length > limit,
  };
}

export const LibraryView = {
  name: 'LibraryView',
  components: { DataTable, EmptyState, Icon, Modal, SearchInput },
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
      loadError: '',
      preview: null,
      previewContent: '',
      previewCsv: null,
      previewImageUrl: '',
      previewLoading: false,
      previewError: '',
      previewRequest: 0,
      rename: null,
      renameValue: '',
      menuFor: '',
      uploading: false,
      renaming: false,
      busyFiles: [],
    };
  },
  computed: {
    isImagePreview() {
      return ['.png', '.jpg', '.jpeg', '.webp'].includes(this.preview?.extension);
    },
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
    document.addEventListener('pointerdown', this.dismissMenu);
    document.addEventListener('keydown', this.dismissMenu);
    await this.load();
  },
  beforeUnmount() {
    document.removeEventListener('pointerdown', this.dismissMenu);
    document.removeEventListener('keydown', this.dismissMenu);
    this.previewRequest += 1;
    this.releasePreviewImage();
  },
  methods: {
    previewUrl(file) {
      return withWorkspace(`/api/library/${encodeURIComponent(file.id)}/preview`, state.workspaceId);
    },
    releasePreviewImage() {
      if (this.previewImageUrl) URL.revokeObjectURL(this.previewImageUrl);
      this.previewImageUrl = '';
    },
    finishImagePreview(event, failed = false) {
      if (Number(event.currentTarget.dataset.previewRequest) !== this.previewRequest) return;
      this.previewLoading = false;
      if (failed) this.previewError = '图片无法读取，请重新加载或下载原文件';
    },
    closePreview() {
      this.previewRequest += 1;
      this.releasePreviewImage();
      this.preview = null;
      this.previewLoading = false;
    },
    async openPreview(file) {
      const requestId = ++this.previewRequest;
      this.releasePreviewImage();
      this.preview = file;
      this.previewContent = '';
      this.previewCsv = null;
      this.previewError = '';
      this.previewLoading = false;
      const imagePreview = ['.png', '.jpg', '.jpeg', '.webp'].includes(file.extension);
      if (!imagePreview && !['.md', '.txt', '.json', '.csv'].includes(file.extension)) return;
      this.previewLoading = true;
      try {
        const response = await fetch(this.previewUrl(file), { headers: { 'X-Workspace-Id': state.workspaceId } });
        if (!response.ok) {
          const text = await response.text();
          let message = `预览加载失败 (${response.status})`;
          try { message = JSON.parse(text).error || message; } catch { /* 非 JSON 错误仍提供可重试反馈。 */ }
          throw new Error(message);
        }
        if (imagePreview) {
          const image = await response.blob();
          if (requestId === this.previewRequest) this.previewImageUrl = URL.createObjectURL(image);
          return;
        }
        const text = await response.text();
        if (requestId !== this.previewRequest) return;
        if (file.extension === '.csv') this.previewCsv = csvPreview(text);
        else this.previewContent = file.extension === '.md' ? renderMarkdown(text) : text;
      } catch (error) {
        if (requestId === this.previewRequest) this.previewError = error.message;
      } finally {
        if (requestId === this.previewRequest && (!imagePreview || this.previewError)) this.previewLoading = false;
      }
    },
    dismissMenu(event) {
      if (event.key === 'Escape' || (event.type === 'pointerdown' && !event.target.closest('.dropdown'))) this.menuFor = '';
    },
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/library');
        this.items = response.items || [];
        this.categories = (response.categories || []).filter(entry => entry.key !== '全部');
      } catch (error) {
        this.loadError = error.message;
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
      if (!this.rename || this.renaming) return;
      const title = this.renameValue.trim();
      if (!title) return;
      this.renaming = true;
      try {
        await actions.patch(`/api/library/${this.rename.id}`, { title });
        this.rename.title = title;
        this.rename = null;
        toast('已重命名', '完成');
      } catch (error) {
        toast(error.message, '重命名失败', 'error');
      } finally {
        this.renaming = false;
      }
    },
    async toggleFavorite(file) {
      if (this.busyFiles.includes(file.id)) return;
      this.busyFiles.push(file.id);
      try {
        const response = await actions.patch(`/api/library/${file.id}`, {
          favorite: file.favorite ? '' : file.title,
        });
        file.favorite = response.item.favorite;
        file.title = response.item.title;
      } catch (error) {
        toast(error.message, '操作失败', 'error');
      } finally {
        this.busyFiles = this.busyFiles.filter(id => id !== file.id);
      }
    },
    async remove(file) {
      if (this.busyFiles.includes(file.id)) return;
      this.busyFiles.push(file.id);
      this.menuFor = '';
      try {
        await actions.remove(`/api/library/${file.id}`);
        this.items = this.items.filter(item => item.id !== file.id);
        this.categories = this.categories.map(entry => ({ ...entry, count: this.items.filter(item => item.category === entry.key).length }));
        toast('已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      } finally {
        this.busyFiles = this.busyFiles.filter(id => id !== file.id);
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
      if (!files.length || this.uploading) return;
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
              <Icon name="upload" :size="14" />{{ uploading ? '上传中…' : '上传文件' }}
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

        <div v-else-if="loadError" class="card stack"><p class="small" role="alert" style="color:var(--danger)">{{ loadError }}</p><button class="btn btn--sm" @click="load">重新加载资料</button></div>
        <EmptyState v-else-if="!filtered.length" icon="library"
                    :title="query || category !== '全部' ? '没有匹配的资料' : '资料库还是空的'"
                    :text="query || category !== '全部' ? '调整关键词或资料分类后重试。' : '完成一次分析后点击「生成报告 / PPT / Excel」，产出会自动出现在这里。'" />

        <div v-else class="file-grid">
          <article v-for="file in filtered" :key="file.id" class="card card--interactive file-tile">
            <div class="row row--between">
              <span class="file-tile__icon"><Icon :name="iconFor(file)" :size="19" /></span>
              <div v-if="canAnalyze" class="row">
              <button class="icon-btn tip" :data-tip="file.favorite ? '取消收藏' : '收藏'"
                      :aria-label="file.favorite ? '取消收藏' : '收藏'" :disabled="busyFiles.includes(file.id)"
                      :class="{ 'icon-btn--active': file.favorite }" @click="toggleFavorite(file)">
                <Icon name="star" :size="16" />
              </button>
              <div class="dropdown">
                <button class="icon-btn" :class="{ 'icon-btn--active': menuFor === file.id }"
                        aria-label="更多" :aria-expanded="menuFor === file.id" @click="menuFor = menuFor === file.id ? '' : file.id">
                  <Icon name="more" :size="16" />
                </button>
                <div v-if="menuFor === file.id" class="dropdown__menu">
                  <button @click="openRename(file); menuFor = ''"><Icon name="edit" :size="14" />重命名</button>
                  <button class="danger" @click="remove(file)"><Icon name="trash" :size="14" />删除</button>
                </div>
              </div>
              </div>
            </div>
            <div>
              <div class="file-card__title truncate" :title="file.title">{{ file.title }}</div>
              <div class="file-card__meta">{{ file.category }} · {{ formatSize(file.size_bytes) }}</div>
            </div>
            <div class="xs faint">{{ formatDate(file.created_at) }}</div>
            <div class="row" style="margin-top:auto">
              <button v-if="file.previewable" class="btn btn--sm" @click="openPreview(file)">
                <Icon name="eye" :size="14" />预览
              </button>
              <button v-if="canAnalyze" class="btn btn--sm" @click="download(file)"><Icon name="download" :size="14" />下载</button>
              <button v-if="canAnalyze && canAsk(file)" class="btn btn--sm" @click="askAbout(file)"><Icon name="chat" :size="14" />追问</button>
            </div>
          </article>
        </div>
      </div>
    </div>

    <Modal :open="!!preview" :title="preview?.title || '预览'" wide @close="closePreview">
      <div v-if="isImagePreview" class="library-image-preview">
        <p v-if="previewLoading" class="small muted" role="status">正在读取图片…</p>
        <div v-if="previewError" class="stack"><p class="small" role="alert" style="color:var(--danger)">{{ previewError }}</p><button class="btn btn--sm" @click="openPreview(preview)">重新加载预览</button></div>
        <img v-else-if="previewImageUrl" v-show="!previewLoading" :key="previewRequest" :src="previewImageUrl"
             :alt="preview?.title || '资料图片'" :data-preview-request="previewRequest"
             @load="finishImagePreview($event)" @error="finishImagePreview($event, true)" />
      </div>
      <p v-else-if="previewLoading" class="small muted" role="status">正在读取资料…</p>
      <div v-else-if="previewError" class="stack"><p class="small" role="alert" style="color:var(--danger)">{{ previewError }}</p><button class="btn btn--sm" @click="openPreview(preview)">重新加载预览</button></div>
      <div v-else-if="preview?.extension === '.md'" class="markdown" v-html="previewContent"></div>
      <div v-else-if="previewCsv" class="stack">
        <div class="row row--between"><p class="small muted">{{ previewCsv.truncated ? '展示前 200 行，更多内容请下载原文件' : '共 ' + previewCsv.rows.length + ' 行数据' }} · CSV 预览</p><button v-if="canAnalyze" class="btn btn--sm" @click="download(preview)"><Icon name="download" :size="14" />下载原文件</button></div>
        <DataTable :rows="previewCsv.rows" :columns="previewCsv.columns" max-height="min(65vh, 560px)" />
      </div>
      <pre v-else-if="['.txt', '.json'].includes(preview?.extension)" style="white-space:pre-wrap;overflow-wrap:anywhere">{{ previewContent }}</pre>
      <iframe v-else-if="preview" class="preview-frame" :src="previewUrl(preview)" :title="preview.title"></iframe>
    </Modal>

    <Modal :open="!!rename" title="重命名" size="small" @close="!renaming && (rename = null)">
      <label class="field">
        <span>名称</span>
        <input v-model.trim="renameValue" class="input" maxlength="120" :disabled="renaming" @keydown.enter="!$event.isComposing && $event.keyCode !== 229 && saveRename()" />
      </label>
      <template #footer>
        <button class="btn" :disabled="renaming" @click="rename = null">取消</button>
        <button class="btn btn--primary" :disabled="renaming || !renameValue.trim()" @click="saveRename">{{ renaming ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>`,
};
