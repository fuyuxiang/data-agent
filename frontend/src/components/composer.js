/**
 * Composer —— 整个用户端最重要的控件。
 *
 * 原则：默认自动选择技能，需要时显式指定。用户在输入框里打 @ 会浮出技能与
 * 智能体候选；不指定也能正常提问，系统会自动挑能力。
 */

import { Icon } from './icons.js';

const { nextTick } = Vue;
import { Drawer } from './ui.js';
import { state, toast } from '../store.js';

const ACCEPTED_FILES = new Set(['docx', 'xlsx', 'pdf', 'md', 'txt']);

const CAPABILITIES = [
  { label: '问数据', icon: 'search' },
  { label: '数据分析', icon: 'chart2' },
  { label: '深度研究', icon: 'searchDeep' },
  { label: '预测分析', icon: 'trendUp' },
  { label: '生成报告', icon: 'fileText' },
  { label: 'Excel 分析', icon: 'fileSpreadsheet' },
];

export const Composer = {
  name: 'Composer',
  components: { Drawer, Icon },
  props: {
    placeholder: { default: '问数据、做分析、生成报告…' },
    disabled: Boolean,
    submitLabel: { default: '发送' },
  },
  emits: ['submit', 'cancel'],
  setup() {
    // Expose the shared bootstrap state to the template as well as computed
    // properties. Without this, the data-source drawer cannot render its list.
    return { state };
  },
  data() {
    return {
      text: '',
      sourceIds: [],
      scopeExplicit: false,
      fileScopeAuto: false,
      scopeOpen: false,
      agentOpen: false,
      agentId: '',
      pendingFiles: [],
      mentionQuery: null,
      resolution: null,
      resolving: false,
      area: null,
    };
  },
  computed: {
    canSend() {
      return !this.disabled && (this.text.trim().length > 0 || this.pendingFiles.length > 0);
    },
    visibleSources() {
      const ready = state.sources.filter(item => item.status === 'ready');
      const allowed = this.agentId && this.agent?.source_scope_mode !== 'authorized'
        ? new Set(this.agent?.source_ids || []) : null;
      const available = ready.filter(item => !allowed || allowed.has(item.id)).map(item => item.id);
      return this.scopeExplicit ? available.filter(id => this.sourceIds.includes(id)) : available;
    },
    chosenSources() {
      const ids = new Set(this.visibleSources);
      return state.sources.filter(item => ids.has(item.id));
    },
    availableSources() {
      const allowed = this.agent?.source_ids;
      return state.sources.filter(item => item.status === 'ready'
        && (!this.agentId || this.agent?.source_scope_mode === 'authorized' || allowed?.includes(item.id)));
    },
    availableAgents() {
      return state.agents.filter(item => item.status === 'published');
    },
    scopeLabel() {
      if (!this.chosenSources.length) return '选择数据';
      if (this.chosenSources.length === 1) return this.chosenSources[0].name;
      return `${this.chosenSources.length} 个数据源`;
    },
    agent() {
      return state.agents.find(item => item.id === this.agentId) || null;
    },
    mentionResults() {
      if (this.mentionQuery === null) return [];
      const query = this.mentionQuery.trim().toLowerCase();
      const skills = state.skills
        .map(item => ({ type: '技能', id: item.id, name: item.name, hint: item.description, icon: 'layers' }))
        .filter(item => !query || item.name.toLowerCase().includes(query) || (item.hint || '').toLowerCase().includes(query))
        .slice(0, 5);
      const agents = state.agents
        .filter(item => item.status === 'published')
        .map(item => ({ type: '智能体', id: item.id, name: item.name, hint: item.description, icon: 'robot' }))
        .filter(item => !query || item.name.toLowerCase().includes(query) || (item.hint || '').toLowerCase().includes(query))
        .slice(0, 3);
      return [...agents, ...skills];
    },
  },
  methods: {
    focus() {
      this.area?.focus();
    },
    onInput(event) {
      // 文本框用 :value 单向绑定，必须在 input 事件里回写模型，
      // 否则发送按钮永远处于禁用状态。
      this.text = event?.target?.value ?? this.text;
      const value = this.text;
      const caret = this.area?.selectionStart ?? value.length;
      const before = value.slice(0, caret);
      const match = before.match(/@([^\s@]*)$/);
      this.mentionQuery = match ? match[1] : null;
      this.resize();
      this.scheduleResolve();
    },
    onKeydown(event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        this.send();
      }
      if (event.key === 'Escape') {
        this.mentionQuery = null;
        this.scopeOpen = false;
        this.agentOpen = false;
      }
    },
    insertMention(item) {
      const value = this.text;
      const caret = this.area?.selectionStart ?? value.length;
      const next = `${value.slice(0, caret)}@${item.name} ${value.slice(caret)}`;
      this.text = next;
      this.mentionQuery = null;
      if (item.type === '智能体') {
        this.agentId = item.id;
        this.sourceIds = [];
        this.scopeExplicit = false;
      }
      nextTick(() => {
        this.resize();
        this.focus();
      });
      this.scheduleResolve();
    },
    send() {
      if (!this.canSend) return;
      const payload = {
        text: this.text.trim() || '分析上传的文件',
        sourceIds: this.visibleSources,
        agentId: this.agentId,
        files: this.pendingFiles,
        skillHint: this.resolution?.selected?.[0]?.id || '',
      };
      this.$emit('submit', payload);
      this.mentionQuery = null;
    },
    reset() {
      this.text = '';
      this.pendingFiles = [];
      if (this.fileScopeAuto) {
        this.scopeExplicit = false;
        this.sourceIds = [];
        this.fileScopeAuto = false;
      }
      this.resolution = null;
      this.mentionQuery = null;
      nextTick(() => this.resize());
    },
    resize() {
      if (!this.area) return;
      this.area.style.height = 'auto';
      this.area.style.height = `${Math.min(this.area.scrollHeight, 260)}px`;
    },
    toggleSource(id) {
      const current = this.chosenSources.map(item => item.id);
      this.scopeExplicit = true;
      this.fileScopeAuto = false;
      this.sourceIds = current.includes(id)
        ? current.filter(value => value !== id)
        : [...current, id];
    },
    selectAgent(id) {
      this.agentId = id;
      this.agentOpen = false;
      if (id) {
        // An explicitly selected agent owns the data scope for this question.
        this.scopeExplicit = false;
        this.sourceIds = [];
      }
    },
    pickFiles(event) {
      this.addFiles(Array.from(event.target.files || []));
      event.target.value = '';
    },
    dropFile(event) {
      this.addFiles(Array.from(event.dataTransfer?.files || []));
    },
    addFiles(files) {
      const accepted = files.filter(file => ACCEPTED_FILES.has(file.name.split('.').pop().toLowerCase())
        && file.size <= 50 * 1024 * 1024);
      if (accepted.length !== files.length) {
        toast('仅支持不超过 50 MB 的 PDF、Word、Excel 和文本文件', '附件未添加', 'error');
      }
      if (this.pendingFiles.length + accepted.length > 20) {
        toast('每次最多添加 20 个文件', '附件未添加', 'error');
        return;
      }
      if (accepted.length && !this.pendingFiles.length && !this.scopeExplicit && !this.agentId) {
        this.scopeExplicit = true;
        this.sourceIds = [];
        this.fileScopeAuto = true;
      }
      this.pendingFiles = [...this.pendingFiles, ...accepted];
    },
    removeFile(index) {
      this.pendingFiles.splice(index, 1);
      if (!this.pendingFiles.length && this.fileScopeAuto) {
        this.scopeExplicit = false;
        this.sourceIds = [];
        this.fileScopeAuto = false;
      }
    },
    scheduleResolve() {
      clearTimeout(this.timer);
      const question = this.text.trim();
      if (question.length < 4) {
        this.resolution = null;
        return;
      }
      // 让用户看到"系统会用哪个技能"，但绝不阻塞发送。
      this.timer = setTimeout(async () => {
        this.resolving = true;
        try {
          const response = await fetch(`/api/skills/resolve`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Workspace-Id': state.workspaceId,
              'X-CSRF-Token': sessionStorage.getItem('shuqing-csrf') || '',
            },
            body: JSON.stringify({ question }),
          });
          if (response.ok) this.resolution = await response.json();
        } catch {
          this.resolution = null;
        } finally {
          this.resolving = false;
        }
      }, 320);
    },
  },
  watch: {
    sourceIds(value) {
      this.$emit('update:sources', value);
    },
  },
  mounted() {
    this.resize();
  },
  beforeUnmount() {
    clearTimeout(this.timer);
  },
  template: `
    <div class="composer" @dragover.prevent @drop.prevent="dropFile">
      <div v-if="pendingFiles.length" class="composer__attachments">
        <span v-for="(file, index) in pendingFiles" :key="index" class="attachment-chip">
          <Icon name="file" :size="13" />{{ file.name }}
          <button class="icon-btn" style="width:18px;height:18px" :aria-label="'移除 ' + file.name"
                  @click="removeFile(index)"><Icon name="close" :size="12" /></button>
        </span>
      </div>

      <textarea ref="area" class="composer__input" rows="1" :value="text" :disabled="disabled"
                :placeholder="placeholder" @input="onInput" @keydown="onKeydown"></textarea>

      <div class="composer__bar">
        <button type="button" class="composer__scope" :class="{ 'is-on': chosenSources.length }"
                title="选择本次分析的数据范围" @click="scopeOpen = true">
          <Icon name="database" :size="15" />
          <span class="truncate" style="max-width:160px">{{ scopeLabel }}</span>
        </button>
        <label class="composer__scope" title="附加文件（PDF、Word、Excel、文本）">
          <Icon name="upload" :size="15" />
          <input type="file" multiple hidden accept=".docx,.xlsx,.pdf,.md,.txt" @change="pickFiles" />
        </label>
        <button type="button" class="composer__scope" :class="{ 'is-on': agentId }"
                title="选择本次使用的智能体" @click="agentOpen = true">
          <Icon name="robot" :size="15" />
          <span class="truncate" style="max-width:120px">{{ agent ? agent.name : '自动选择智能体' }}</span>
        </button>
        <span class="composer__spacer"></span>
        <span v-if="resolving" class="composer__scope xs faint">识别中…</span>
        <span v-else-if="resolution && resolution.selected && resolution.selected.length" class="composer__scope xs">
          <Icon name="layers" :size="13" />
          将使用 {{ resolution.selected.map(item => item.name).join('、') }}
        </span>
        <button class="composer__send" :disabled="!canSend" :aria-label="submitLabel" @click="send">
          <Icon name="send" :size="16" />
        </button>
      </div>

      <div v-if="resolution && resolution.selected && resolution.selected.length" class="composer__hint">
        默认自动选择技能；需要固定能力时用 <span class="mono">@技能名</span> 显式指定。
      </div>

      <Drawer :open="scopeOpen" title="选择数据范围" subtitle="本次分析只能使用你有权访问的数据"
              @close="scopeOpen = false">
        <label v-for="item in availableSources"
               :key="item.id" class="checkbox" style="padding:8px 0">
          <input type="checkbox" :value="item.id" :checked="visibleSources.includes(item.id)"
                 @change="toggleSource(item.id)" />
          <span class="grow">
            <b>{{ item.name }}</b>
            <small class="faint" style="display:block">{{ item.kind }} · {{ item.status }}</small>
          </span>
        </label>
        <p v-if="!availableSources.length" class="muted small">
          {{ agentId ? '当前智能体没有可用的数据源。请先在管理后台配置并发布数据源。' : '还没有可用数据源。可以先到「管理后台 → 数据」接入，或在工作台载入演示数据。' }}
        </p>
      </Drawer>

      <Drawer :open="agentOpen" title="选择智能体" subtitle="固定智能体后，本次提问会使用它已配置的能力和数据范围"
              @close="agentOpen = false">
        <button type="button" class="command__item" :class="{ 'is-selected': !agentId }"
                @click="selectAgent('')">
          <Icon name="sparkle" :size="15" />
          <span class="grow"><b>自动选择智能体</b><small>由系统根据问题自动匹配</small></span>
          <Icon v-if="!agentId" name="check" :size="15" />
        </button>
        <button v-for="item in availableAgents" :key="item.id" type="button"
                class="command__item" :class="{ 'is-selected': item.id === agentId }"
                @click="selectAgent(item.id)">
          <Icon :name="item.icon || 'robot'" :size="15" />
          <span class="grow"><b>{{ item.name }}</b><small>{{ item.source_scope_mode === 'authorized' ? '使用你当前有权分析的数据' : item.description || '已发布智能体' }}</small></span>
          <Icon v-if="item.id === agentId" name="check" :size="15" />
        </button>
        <p v-if="!availableAgents.length" class="muted small">当前没有已发布的智能体。</p>
      </Drawer>
    </div>

    <div v-if="mentionQuery !== null && mentionResults.length" class="card" style="margin-top:8px;padding:8px">
      <button v-for="item in mentionResults" :key="item.type + item.id" class="command__item"
              @click="insertMention(item)">
        <Icon :name="item.icon" :size="15" />
        <span class="grow">
          <b>{{ item.name }}</b>
          <small>{{ item.type }}{{ item.hint ? ' · ' + item.hint : '' }}</small>
        </span>
      </button>
    </div>`,
};

export { CAPABILITIES };
