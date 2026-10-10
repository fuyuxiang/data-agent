/**
 * 全局状态与领域动作。
 *
 * 单一 reactive 对象 + 显式 action，取代 V1 那种把 ctx 到处手传的做法。
 * 所有后端调用集中在这里，页面只调用动作，不直接碰 fetch。
 */

import { api, download, get, patch, post, rememberWorkspace, remove, withWorkspace } from './api.js';

const { computed, reactive } = Vue;

export const state = reactive({
  ready: false,
  authChecking: true,
  authRequired: false,
  authMode: 'login',
  authError: '',
  authNotice: '',
  bootstrapRequired: false,
  registrationOpen: false,
  emailCodeRequired: false,
  user: null,

  route: 'workbench',
  routeParams: {},
  sidebarCollapsed: false,

  workspaceId: localStorage.getItem('shuqing-workspace') || 'default',
  workspaceRole: '',
  workspaces: [],

  sessions: [],
  activeSessionId: '',
  sources: [],
  skills: [],
  skillCategories: [],
  agents: [],
  metrics: [],
  recommendedQuestions: [],
  providers: [],
  onboarding: null,
  capabilities: {},

  theme: document.documentElement.dataset.theme || 'light',
  toasts: [],
  busy: false,
  busyLabel: '',
  commandOpen: false,
  commandQuery: '',
});

/* ------------------------------------------------------------------ 反馈 */

export function toast(message, title = '完成', tone = 'success') {
  const item = { id: `${Date.now()}-${Math.random()}`, title, message, tone };
  state.toasts.push(item);
  setTimeout(() => { state.toasts = state.toasts.filter(entry => entry.id !== item.id); }, 3800);
}

export function fail(error) {
  console.error(error);
  toast(error?.message || '操作未完成', '出现问题', 'error');
}

export async function run(label, action) {
  state.busy = true;
  state.busyLabel = label;
  try {
    return await action();
  } catch (error) {
    fail(error);
    throw error;
  } finally {
    state.busy = false;
    state.busyLabel = '';
  }
}

export function toggleTheme() {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = state.theme;
  localStorage.setItem('shuqing-theme', state.theme);
}

/* ------------------------------------------------------------------ 派生 */

export const activeSession = computed(
  () => state.sessions.find(item => item.id === state.activeSessionId) || null,
);

export const canAdmin = computed(() => ['owner', 'editor'].includes(state.workspaceRole));

export const userInitial = computed(() => {
  const name = state.user?.name || state.user?.username || state.user?.email || '我';
  return name.slice(0, 1).toUpperCase();
});

export const readySources = computed(
  () => state.sources.filter(item => item.status === 'ready'),
);

/* ------------------------------------------------------------------ 会话 */

export async function bootstrap({ quiet = false } = {}) {
  if (!quiet) state.authChecking = true;
  try {
    const identity = await get('/api/auth/me');
    state.user = identity.user;
    state.registrationOpen = !!identity.registration_open;
    state.bootstrapRequired = !!identity.bootstrap_required;
    state.emailCodeRequired = !!identity.email_code_required;
    if (!identity.authenticated && !identity.local_mode) {
      state.authRequired = true;
      state.authMode = identity.bootstrap_required
        ? 'bootstrap'
        : new URLSearchParams(location.search).has('invite') ? 'register' : 'login';
      state.ready = true;
      return;
    }
    state.authRequired = false;
    const data = await api(withWorkspace('/api/bootstrap', state.workspaceId));
    state.workspaces = data.workspaces;
    state.workspaceId = data.active_workspace?.id || 'default';
    rememberWorkspace(state.workspaceId);
    state.workspaceRole = data.active_membership?.role || (!state.user ? 'owner' : '');
    state.sessions = data.sessions;
    state.sources = data.sources;
    state.providers = data.providers;
    state.skills = data.skills || [];
    state.skillCategories = data.skill_categories || [];
    state.agents = data.agents || [];
    state.metrics = data.metrics || [];
    state.recommendedQuestions = data.recommended_questions || [];
    state.onboarding = data.onboarding;
    state.capabilities = data.capabilities || {};
    state.activeSessionId = quiet && state.sessions.some(item => item.id === state.activeSessionId)
      ? state.activeSessionId : data.active_session?.id || state.sessions[0]?.id || '';
    state.ready = true;
  } catch (error) {
    if (error?.status === 401) {
      state.authRequired = true;
      state.ready = true;
    } else {
      fail(error);
    }
  } finally {
    if (!quiet) state.authChecking = false;
  }
}

export async function submitAuth() {
  state.authError = '';
  state.authNotice = '';
  try {
    const registering = state.authMode !== 'login';
    const response = await post(
      registering ? '/api/auth/register' : '/api/auth/login',
      registering
        ? {
          email: state.authForm.email, name: state.authForm.name, username: state.authForm.username,
          password: state.authForm.password, bootstrap_token: state.authForm.bootstrapToken,
          invitation_token: new URLSearchParams(location.search).get('invite') || '',
          code: state.authForm.code,
        }
        : { username: state.authForm.username, password: state.authForm.password },
    );
    if (response.active_workspace_id) {
      state.workspaceId = response.active_workspace_id;
      rememberWorkspace(state.workspaceId);
    }
    if (registering && new URLSearchParams(location.search).has('invite')) {
      history.replaceState(null, '', location.pathname + location.hash);
    }
    state.authRequired = false;
    state.authChecking = true;
    state.authForm.password = '';
    state.authForm.bootstrapToken = '';
    state.authForm.code = '';
    await bootstrap();
  } catch (error) {
    state.authError = error?.message || '认证失败';
  }
}

export async function sendAuthCode() {
  state.authError = '';
  state.authNotice = '';
  try {
    const response = await post('/api/auth/send-code', { email: state.authForm.email });
    state.authNotice = response.message || '验证码已发送，请检查邮箱';
  } catch (error) {
    state.authError = error?.message || '验证码发送失败';
  }
}

export async function logout() {
  await post('/api/auth/logout');
  sessionStorage.removeItem('shuqing-csrf');
  state.user = null;
  state.authRequired = true;
  state.authMode = 'login';
}

state.authForm = reactive({ email: '', name: '', username: '', password: '', bootstrapToken: '', code: '' });

export async function newSession(name = '新对话') {
  const result = await post('/api/sessions', {
    name, workspace_id: state.workspaceId, source_ids: activeSession.value?.source_ids || [],
  });
  state.sessions.forEach(item => { item.status = 'idle'; });
  state.sessions.unshift(result.item);
  state.activeSessionId = result.item.id;
  return result.item;
}

export async function openSession(id) {
  state.activeSessionId = id;
  return id;
}

export async function renameSession(session, name) {
  const response = await patch(`/api/sessions/${session.id}`, { name });
  Object.assign(session, response.item);
}

export async function deleteSession(session) {
  await remove(`/api/sessions/${session.id}`);
  state.sessions = state.sessions.filter(item => item.id !== session.id);
  if (state.activeSessionId === session.id) {
    if (state.sessions.length) state.activeSessionId = state.sessions[0].id;
    else await newSession();
  }
}

/* ------------------------------------------------------------------ 演示数据 */

export async function loadDemo() {
  const result = await post('/api/demo/seed', { workspace_id: state.workspaceId });
  await bootstrap();
  return result;
}

/* ------------------------------------------------------------------ 通用动作 */

export const actions = {
  get: (path) => get(path),
  post: (path, body) => post(path, body),
  patch: (path, body) => patch(path, body),
  remove: (path, body) => remove(path, body),
  download,
  uploadAttachments(runId, files) {
    const form = new FormData();
    files.forEach(file => form.append('files', file));
    return api(`/api/analyses/${runId}/attachments`, { method: 'POST', body: form });
  },
  /** 预检技能选择，让 Composer 在用户按下发送前就能显示"会用哪个技能"。 */
  resolveSkills(question) {
    return post('/api/skills/resolve', { question });
  },
  agentById(id) {
    return state.agents.find(item => item.id === id) || null;
  },
  skillById(id) {
    return state.skills.find(item => item.id === id) || null;
  },
};

export function formatTime(value) {
  if (!value) return '';
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).format(new Date(value));
  } catch {
    return String(value);
  }
}

export function formatDate(value) {
  if (!value) return '';
  try {
    return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' }).format(new Date(value));
  } catch {
    return String(value);
  }
}

export function formatSize(bytes) {
  const value = Number(bytes || 0);
  if (!value) return '';
  if (value >= 1 << 20) return `${(value / (1 << 20)).toFixed(1)} MB`;
  if (value >= 1 << 10) return `${Math.round(value / (1 << 10))} KB`;
  return `${value} B`;
}
