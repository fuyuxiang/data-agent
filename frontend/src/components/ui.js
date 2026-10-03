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

function useDismissable(open, close, root) {
  let previous = null;
  watch(open, async (value) => {
    if (value) {
      previous = document.activeElement;
      await nextTick();
      const target = root.value;
      target?.querySelector('[autofocus], input, textarea, select, button')?.focus();
      document.addEventListener('keydown', onKeydown, true);
    } else {
      document.removeEventListener('keydown', onKeydown, true);
      if (previous?.isConnected) {
        await nextTick();
        previous?.focus();
      }
      previous = null;
    }
  });
  function onKeydown(event) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = [...(root.value?.querySelectorAll(
      'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]',
    ) || [])];
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
  onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown, true));
}

export const Modal = {
  name: 'Modal',
  components: { Icon },
  props: { open: Boolean, title: String, wide: Boolean },
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
          <section ref="root" class="modal" :class="{ 'modal--wide': wide }"
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
  props: { icon: { default: 'file' }, title: String, text: String },
  template: `
    <div class="empty">
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
  },
  template: `
    <div v-if="!rows.length" class="empty" style="padding:24px">
      <p class="small">{{ empty || '暂无数据' }}</p>
    </div>
    <div v-else class="table-wrap" :style="{ maxHeight }">
      <table class="table">
        <thead><tr>
          <th v-for="column in shownColumns" :key="key(column)" :class="{ 'is-numeric': numeric(column) }">
            {{ label(column) }}
          </th>
        </tr></thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="index">
            <td v-for="column in shownColumns" :key="key(column)"
                :class="{ 'is-numeric': numeric(column) }"
                :title="format(row[key(column)])">{{ format(row[key(column)]) }}</td>
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
  template: `
    <div class="tabs" role="tablist">
      <button v-for="item in items" :key="item.key" role="tab"
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
      <input class="input input--sm" :value="modelValue" :placeholder="placeholder"
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
