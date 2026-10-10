/**
 * 路由与信息架构。
 *
 * 用户端与管理后台在同一个 hash 空间里，用前缀区分；V1 的 chat/sources/
 * semantic/knowledge/settings 五个旧 hash 全部重定向到工作台，不留两套产品。
 */

import { canAdmin, state, toast } from './store.js';

export const USER_NAV = [
  { key: 'workbench', label: '工作台', icon: 'home' },
  { key: 'agents', label: '智能体', icon: 'robot' },
  { key: 'library', label: '资料库', icon: 'library' },
  { key: 'metrics', label: '指标中心', icon: 'metric' },
];

export const ADMIN_NAV = [
  {
    label: '构建',
    items: [
      { key: 'admin/agents', label: '智能体', icon: 'robot' },
      { key: 'admin/skills', label: '技能', icon: 'layers' },
    ],
  },
  {
    label: '数据',
    items: [
      { key: 'admin/data', label: '数据', icon: 'database' },
      { key: 'admin/metrics', label: '指标中心', icon: 'metric' },
      { key: 'admin/knowledge', label: '知识', icon: 'book' },
    ],
  },
  {
    label: '能力',
    items: [
      { key: 'admin/models', label: '模型', icon: 'cpu' },
      { key: 'admin/mcp', label: 'MCP', icon: 'plug' },
      { key: 'admin/integrations', label: '集成', icon: 'cable' },
    ],
  },
  {
    label: '运营',
    items: [
      { key: 'admin/runs', label: '运行记录', icon: 'history' },
      { key: 'admin/evaluations', label: '评测', icon: 'chart2' },
    ],
  },
  {
    label: '系统',
    items: [
      { key: 'admin/users', label: '用户管理', icon: 'users' },
      { key: 'admin/settings', label: '系统设置', icon: 'settings' },
      { key: 'admin/trash', label: '回收站', icon: 'trash' },
    ],
  },
];

export const TITLES = {
  workbench: '工作台',
  conversation: '对话',
  agents: '智能体',
  library: '资料库',
  metrics: '指标中心',
  'admin/agents': '智能体',
  'admin/skills': '技能',
  'admin/data': '数据',
  'admin/metrics': '指标中心',
  'admin/knowledge': '知识',
  'admin/models': '模型',
  'admin/mcp': 'MCP',
  'admin/integrations': '集成',
  'admin/runs': '运行记录',
  'admin/evaluations': '评测',
  'admin/users': '用户管理',
  'admin/settings': '系统设置',
  'admin/trash': '回收站',
  trash: '回收站',
};

const LEGACY = {
  chat: 'workbench',
  sources: 'admin/data',
  knowledge: 'admin/knowledge',
  semantic: 'metrics',
  settings: 'admin/settings',
};

// 对话页是可达但不进导航的路由：它由提问动作进入，不在侧栏里出现。
const HIDDEN = ['conversation', 'trash'];

const KNOWN = new Set([
  ...USER_NAV.map(item => item.key),
  ...ADMIN_NAV.flatMap(group => group.items.map(item => item.key)),
  ...HIDDEN,
]);

function parse(hash) {
  const raw = (hash || '').replace(/^#\/?/, '');
  const [path, query] = raw.split('?');
  return {
    path: path || 'workbench',
    params: Object.fromEntries(new URLSearchParams(query || '')),
  };
}

function writeHash(path, params = {}) {
  const query = new URLSearchParams(params).toString();
  const next = `#/${path}${query ? `?${query}` : ''}`;
  if (location.hash !== next) location.hash = next;
}

export function navigate(path, params = {}) {
  if (!KNOWN.has(path)) {
    toast('页面不存在', '无法打开');
    writeHash('workbench');
    return;
  }
  if (path.startsWith('admin/') && !canAdmin.value) {
    toast('需要管理员权限', '无法打开');
    writeHash('workbench');
    return;
  }
  writeHash(path, params);
}

export function applyHash() {
  const { path, params } = parse(location.hash);
  const target = LEGACY[path];
  if (target) {
    // V1 链接不长期存在：直接落到新信息架构的对应位置。
    writeHash(target);
    return;
  }
  state.route = KNOWN.has(path) ? path : 'workbench';
  state.routeParams = params;
  if (state.route.startsWith('admin/') && !canAdmin.value) {
    writeHash('workbench');
  }
}

export function currentTitle() {
  return TITLES[state.route] || '数擎';
}

export function isAdminRoute(route = state.route) {
  return route.startsWith('admin/');
}

/** ⌘K 命令表：只列真实存在、用户有权进入的页面与动作。 */
export function commandItems() {
  const pages = [
    ...USER_NAV.map(item => ({ name: item.key, label: item.label, icon: item.icon, kind: '页面' })),
    ...(canAdmin.value ? ADMIN_NAV.flatMap(group => group.items) : [])
      .map(item => ({ name: item.key, label: item.label, icon: item.icon, kind: '管理后台' })),
  ];
  return [
    ...pages,
    { name: 'trash', label: '回收站', icon: 'trash', kind: '页面' },
    { name: 'new', label: '新建对话', icon: 'plus', kind: '动作' },
    { name: 'workbench', label: '回到工作台', icon: 'home', kind: '动作' },
    { name: 'theme', label: '切换深浅色', icon: state.theme === 'dark' ? 'sun' : 'moon', kind: '动作' },
  ];
}
