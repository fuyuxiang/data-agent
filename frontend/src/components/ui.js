/**
 * 统一基础组件。
 *
 * V1 的教训是每个面板各自复制一套按钮、表格和弹窗；V2 把它们收在这里，
 * 页面只做组合，不再重画 UI。
 */

const { computed, nextTick, onBeforeUnmount, ref, watch } = Vue;

import { Icon } from './icons.js';

/* ------------------------------------------------------------------ 状态 */

const STATUS_LABELS = {
  ready: '就绪',
  active: '使用中',
  configured: '已配置',
  connected: '已连接',
  online: '在线',
  offline: '离线',
  error: '异常',
  running: '运行中',
  queued: '排队中',
  waiting_input: '待确认',
  waiting_job: '远程作业中',
  waiting_approval: '待审批',
  paused: '已暂停',
  cancelling: '取消中',
  finished: '已结束',
  completed: '已完成',
  published: '已发布',
  approved: '已认证',
  deprecated: '已停用',
  disabled: '已停用',
  draft: '草稿',
  failed: '失败',
  cancelled: '已取消',
  partial: '部分完成',
  passed: '已通过',
  blocked: '未通过',
  no_data: '无数据',
};

export const Status = {
  name: 'Status',
  components: { Icon },
  props: { status: String, label: String, icon: String },
  computed: {
    text() {
      return this.label || STATUS_LABELS[this.status] || this.status || '未知';
    },
  },
  template: `
    <span class="status" :data-status="status">
      <Icon v-if="icon" :name="icon" :size="14" />
      <i v-else></i>{{ text }}
    </span>`,
};

/* ------------------------------------------------------------------ 弹窗 */

const dismissableStack = [];
let bodyOverflow = '';
let bodyPadding = '';

/** Only the topmost layer owns keyboard/focus; background scroll resumes after the last closes. */
export function useDismissable(open, close, root) {
  let previous = null;
  let generation = 0;
  const layer = {};
  const focusable = () => [...(root.value?.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex]',
  ) || [])].filter(item => item.tabIndex >= 0 && item.getClientRects().length && !item.closest('[inert]'));
  const isTop = () => dismissableStack.at(-1) === layer;
  function release() {
    generation += 1;
    document.removeEventListener('keydown', onKeydown, true);
    document.removeEventListener('focusin', onFocus, true);
    const index = dismissableStack.indexOf(layer);
    if (index < 0) return false;
    const wasTop = isTop();
    dismissableStack.splice(index, 1);
    if (!dismissableStack.length) {
      document.body.style.overflow = bodyOverflow;
      document.body.style.paddingInlineEnd = bodyPadding;
    }
    return wasTop;
  }
  watch(open, async (value) => {
    if (value) {
      previous = document.activeElement;
      const current = ++generation;
      await nextTick();
      if (current !== generation || !open.value || !root.value) return;
      if (!dismissableStack.length) {
        bodyOverflow = document.body.style.overflow;
        bodyPadding = document.body.style.paddingInlineEnd;
        const scrollbar = innerWidth - document.documentElement.clientWidth;
        if (scrollbar > 0) document.body.style.paddingInlineEnd = `${parseFloat(getComputedStyle(document.body).paddingInlineEnd) + scrollbar}px`;
        document.body.style.overflow = 'hidden';
      }
      dismissableStack.push(layer);
      const target = root.value;
      document.addEventListener('keydown', onKeydown, true);
      document.addEventListener('focusin', onFocus, true);
      const items = focusable();
      (items.find(item => item.hasAttribute('autofocus')) ||
        items.find(item => ['INPUT', 'TEXTAREA', 'SELECT'].includes(item.tagName)) || items[0] || target).focus();
    } else {
      const wasTop = release();
      if (wasTop && previous?.isConnected) {
        await nextTick();
        previous?.focus();
      }
      previous = null;
    }
  }, { immediate: true });
  function onFocus(event) {
    if (isTop() && root.value && !root.value.contains(event.target)) {
      (focusable()[0] || root.value).focus();
    }
  }
  function onKeydown(event) {
    if (!isTop() || event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = focusable();
    if (!items.length) {
      event.preventDefault();
      return;
    }
    if (event.shiftKey && document.activeElement === items[0]) {
      event.preventDefault();
      items.at(-1).focus();
    } else if (!event.shiftKey && document.activeElement === items.at(-1)) {
      event.preventDefault();
      items[0].focus();
    }
  }
  onBeforeUnmount(() => {
    const wasTop = release();
    if (wasTop && previous?.isConnected) previous.focus();
  });
}

export const Modal = {
  name: 'Modal',
  components: { Icon },
  props: { open: Boolean, title: String, wide: Boolean, size: { type: String, default: 'medium' } },
  emits: ['close'],
  setup(props, { emit }) {
    const root = ref(null);
    useDismissable(computed(() => props.open), () => emit('close'), root);
    return { root };
  },
  template: `
    <Teleport to="body">
      <Transition name="fade">
        <div v-if="open" class="overlay overlay--center" @mousedown.self="$emit('close')">
          <section ref="root" class="modal" :class="{ 'modal--wide': wide }" :data-size="size"
                   role="dialog" aria-modal="true" :aria-label="title" tabindex="-1">
            <header class="modal__head">
              <h2>{{ title }}</h2>
              <button class="icon-btn" @click="$emit('close')" aria-label="关闭"><Icon name="close" /></button>
            </header>
            <div class="modal__body"><slot /></div>
            <footer v-if="$slots.footer" class="modal__foot"><slot name="footer" /></footer>
          </section>
        </div>
      </Transition>
    </Teleport>`,
};

/** 右侧抽屉：只在用户主动查看来源/口径/执行过程时出现，不常驻占位。 */
export const Drawer = {
  name: 'Drawer',
  components: { Icon },
  props: { open: Boolean, title: String, subtitle: String, width: { type: Number, default: 520 } },
  emits: ['close'],
  setup(props, { emit }) {
    const root = ref(null);
    useDismissable(computed(() => props.open), () => emit('close'), root);
    return { root };
  },
  template: `
    <Teleport to="body">
      <Transition name="fade">
        <div v-if="open" class="overlay overlay--right" @mousedown.self="$emit('close')">
          <section ref="root" class="drawer" :style="{ width: width + 'px' }"
                   role="dialog" aria-modal="true" :aria-label="title" tabindex="-1">
            <header class="drawer__head">
              <div class="grow">
                <h2>{{ title }}</h2>
                <p v-if="subtitle" class="xs faint">{{ subtitle }}</p>
              </div>
              <button class="icon-btn" @click="$emit('close')" aria-label="关闭"><Icon name="close" /></button>
            </header>
            <div class="drawer__body"><slot /></div>
          </section>
        </div>
      </Transition>
    </Teleport>`,
};

/* ------------------------------------------------------------------ 空态 */

export const EmptyState = {
  name: 'EmptyState',
  components: { Icon },
  props: { icon: { default: 'file' }, title: String, text: String, compact: Boolean },
  template: `
    <div class="empty" :class="{ 'empty--compact': compact }">
      <span class="empty__icon"><Icon :name="icon" :size="22" /></span>
      <h3>{{ title }}</h3>
      <p v-if="text">{{ text }}</p>
      <slot />
    </div>`,
};

export const Skeleton = {
  name: 'Skeleton',
  props: { height: { default: '16px' }, width: { default: '100%' } },
  template: `<div class="skeleton" :style="{ height, width }" aria-hidden="true"></div>`,
};

/* ------------------------------------------------------------------ 表格 */

const NUMERIC = /^-?\d+(?:,\d{3})*(?:\.\d+)?%?$/;

export const DataTable = {
  name: 'DataTable',
  props: {
    rows: { type: Array, default: () => [] },
    columns: { type: Array, default: () => [] },
    maxHeight: { default: '420px' },
    empty: String,
  },
  computed: {
    shownColumns() {
      if (this.columns.length) return this.columns;
      return this.rows[0] ? Object.keys(this.rows[0]) : [];
    },
    numericColumns() {
      return new Set(this.shownColumns.filter(column => this.numeric(column)).map(column => this.key(column)));
    },
  },
  methods: {
    key(column) { return typeof column === 'string' ? column : column.key; },
    label(column) { return typeof column === 'string' ? column : (column.label || column.key); },
    numeric(column) {
      if (column?.align) return column.align === 'right';
      const values = this.rows.slice(0, 40)
        .map(row => row[this.key(column)])
        .filter(value => value !== null && value !== undefined && value !== '');
      return values.length > 0 && values.every(value =>
        (typeof value === 'number' && Number.isFinite(value)) ||
        (typeof value === 'string' && NUMERIC.test(value.trim())));
    },
    format(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return '—';
      if (typeof value === 'object') return JSON.stringify(value);
      return String(value);
    },
    date(value) {
      return typeof value === 'string' && /^\d{4}[-/]\d{2}[-/]\d{2}(?:[T ][\d:.+Z-]+)?$/.test(value);
    },
  },
  template: `
    <div v-if="!rows.length" class="empty" style="padding:24px">
      <p class="small">{{ empty || '暂无数据' }}</p>
    </div>
    <div v-else class="table-wrap" :style="{ maxHeight }" tabindex="0" role="region" aria-label="数据明细，可滚动查看">
      <table class="table">
        <thead><tr>
          <th v-for="column in shownColumns" :key="key(column)" :class="{ 'is-numeric': numericColumns.has(key(column)) }" scope="col">
            {{ label(column) }}
          </th>
        </tr></thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="index">
            <td v-for="column in shownColumns" :key="key(column)"
                :class="{ 'is-numeric': numericColumns.has(key(column)) }"
                :title="format(row[key(column)])"><span class="table__value" :class="{ 'is-date': date(row[key(column)]) }">{{ format(row[key(column)]) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>`,
};

/* ------------------------------------------------------------------ 反馈 */

export const Toasts = {
  name: 'Toasts',
  components: { Icon },
  props: { items: { type: Array, default: () => [] } },
  template: `
    <Teleport to="body">
      <div class="toast-stack" aria-live="polite">
        <TransitionGroup name="toast">
          <div v-for="item in items" :key="item.id" class="toast" :data-tone="item.tone">
            <Icon :name="item.tone === 'error' ? 'error' : 'success'" :size="17" />
            <div class="grow">
              <strong>{{ item.title }}</strong>
              <p v-if="item.message">{{ item.message }}</p>
            </div>
          </div>
        </TransitionGroup>
      </div>
    </Teleport>`,
};

/* ------------------------------------------------------------------ 页面骨架 */

export const PageHead = {
  name: 'PageHead',
  props: { title: String, desc: String },
  template: `
    <header class="page-head">
      <div class="grow">
        <h1 class="page-head__title">{{ title }}</h1>
        <p v-if="desc" class="page-head__desc">{{ desc }}</p>
      </div>
      <div v-if="$slots.actions" class="page-head__actions"><slot name="actions" /></div>
    </header>`,
};

export const Tabs = {
  name: 'Tabs',
  props: { modelValue: String, items: { type: Array, default: () => [] } },
  emits: ['update:modelValue'],
  methods: {
    onKeydown(event, index) {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      const count = this.items.length;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + count) % count;
      this.$emit('update:modelValue', this.items[next].key);
      this.$el.querySelectorAll('[role="tab"]')[next]?.focus();
    },
  },
  template: `
    <div class="tabs" role="tablist">
      <button v-for="(item, index) in items" :key="item.key" role="tab" type="button"
              :tabindex="item.key === modelValue ? 0 : -1" @keydown="onKeydown($event, index)"
              :aria-selected="item.key === modelValue"
              :class="{ active: item.key === modelValue }"
              @click="$emit('update:modelValue', item.key)">
        {{ item.label }}<span v-if="item.count !== undefined" class="faint"> {{ item.count }}</span>
      </button>
    </div>`,
};

export const MetricCard = {
  name: 'MetricCard',
  props: { label: String, value: [String, Number], hint: String },
  template: `
    <div class="kpi">
      <div class="kpi__label">{{ label }}</div>
      <div class="kpi__value">{{ value }}</div>
      <div v-if="hint" class="kpi__meta">{{ hint }}</div>
    </div>`,
};

export const SearchInput = {
  name: 'SearchInput',
  components: { Icon },
  props: { modelValue: String, placeholder: { default: '搜索' } },
  emits: ['update:modelValue', 'search'],
  template: `
    <label class="search-field">
      <Icon name="search" :size="15" />
      <input class="input input--sm" :value="modelValue" :placeholder="placeholder" :aria-label="placeholder"
             @input="$emit('update:modelValue', $event.target.value)"
             @keyup.enter="$emit('search')" />
    </label>`,
};

export const Switch = {
  name: 'Switch',
  props: { modelValue: Boolean, label: String },
  emits: ['update:modelValue'],
  template: `
    <button type="button" class="switch" :class="{ on: modelValue }"
            role="switch" :aria-checked="modelValue" :aria-label="label"
            @click="$emit('update:modelValue', !modelValue)"><i></i></button>`,
};

export { STATUS_LABELS };
