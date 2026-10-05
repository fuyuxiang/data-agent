/**
 * 应用外壳：侧栏、顶栏、路由出口、命令面板与登录。
 *
 * 用户端与管理后台共用一个壳，用前缀区分。用户端不出现 MCP、模型服务、
 * Run、Contract、Plan、Action、Tool、审计这些内部概念。
 */

import { Icon } from './components/icons.js';
import { Modal, Toasts } from './components/ui.js';
import {
  ADMIN_NAV, applyHash, commandItems, currentTitle, isAdminRoute, navigate, USER_NAV,
} from './router.js';
import {
  activeSession, bootstrap, canAdmin, deleteSession, logout, newSession, openSession, renameSession,
  sendAuthCode, state, submitAuth, toggleTheme, userInitial,
} from './store.js';

import { AgentsView } from './views/agents.js';
import { AgentBuilderView } from './views/admin-agents.js';
import { IntegrationsView, McpView, ModelsView } from './views/admin-capabilities.js';
import { DataView } from './views/admin-data.js';
import { KnowledgeView } from './views/admin-knowledge.js';
import { EvaluationsView, RunsView } from './views/admin-operations.js';
import { SkillsView } from './views/admin-skills.js';
import { SystemSettingsView, UsersView } from './views/admin-system.js';
import { ConversationView } from './views/conversation.js';
import { LibraryView } from './views/library.js';
import { MetricsView } from './views/metrics.js';
import { WorkbenchView } from './views/workbench.js';

const { createApp } = Vue;

const Shell = {
  name: 'Shell',
  components: {
    AgentsView, AgentBuilderView, ConversationView, DataView, EvaluationsView, Icon,
    IntegrationsView, KnowledgeView, LibraryView, McpView, MetricsView, Modal, ModelsView,
    RunsView, SkillsView, SystemSettingsView, Toasts, UsersView, WorkbenchView,
  },
  setup() {
    return {
      activeSession, canAdmin, commandItems, currentTitle, deleteSession, isAdminRoute, logout,
      navigate, newSession, openSession, sendAuthCode, state, submitAuth,
      toggleTheme, userInitial, USER_NAV, ADMIN_NAV,
    };
  },
  data() {
    return { sessionDialog: null, sessionName: '', sessionSaving: false, commandQuery: '' };
  },
  computed: {
    filteredCommands() {
      const keyword = this.commandQuery.trim().toLowerCase();
      return this.commandItems
        .filter(item => !keyword || `${item.label} ${item.name}`.toLowerCase().includes(keyword))
        .slice(0, 10);
    },
    recentSessions() {
      return this.state.sessions.slice(0, 8);
    },
  },
  mounted() {
    window.addEventListener('hashchange', applyHash);
    window.addEventListener('keydown', this.onKeydown);
  },
  beforeUnmount() {
    window.removeEventListener('hashchange', applyHash);
    window.removeEventListener('keydown', this.onKeydown);
  },
  methods: {
    onKeydown(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        state.commandOpen = !state.commandOpen;
      }
      if (event.key === 'Escape') state.commandOpen = false;
    },
    runCommand(item) {
      state.commandOpen = false;
      this.commandQuery = '';
      if (item.name === 'new') {
        this.startSession();
        return;
      }
      if (item.name === 'theme') {
        toggleTheme();
        return;
      }
      navigate(item.name);
    },
    async startSession() {
      await newSession();
      navigate('workbench');
    },
    async openSession(id) {
      await openSession(id);
      navigate('workbench');
    },
    openConversation(session) {
      navigate('conversation', { id: session.id });
    },
    openSessionRename(session) {
      this.sessionDialog = session;
      this.sessionName = session.name || '';
    },
    closeSessionRename() {
      if (!this.sessionSaving) this.sessionDialog = null;
    },
    async saveSessionRename() {
      const session = this.sessionDialog;
      const name = this.sessionName.trim();
      if (!session || !name || this.sessionSaving) return;
      this.sessionSaving = true;
      try {
        await renameSession(session, name);
        this.sessionDialog = null;
        state.toasts.push({ id: Date.now(), title: '已重命名', message: '', tone: 'success' });
      } catch (error) {
        state.toasts.push({ id: Date.now(), title: error.message, message: '', tone: 'error' });
      } finally {
        this.sessionSaving = false;
      }
    },
    async removeSession(session) {
      if (!window.confirm(`删除「${session.name}」？数据源和已发布成果不受影响。`)) return;
      await deleteSession(session);
    },
  },
  template: `
    <div class="app" :class="{ 'app--collapsed': state.sidebarCollapsed }">
      <aside class="sidebar">
        <header class="sidebar__brand">
          <span class="sidebar__logo"><img src="/src/assets/logo-shuqing.png" alt="" /></span>
          <span class="sidebar__name"><b>数擎</b><small>Data Agent</small></span>
        </header>

        <div class="sidebar__scroll">
          <button class="sidebar__new" @click="startSession">
            <Icon name="plus" :size="16" /><span>新对话</span>
          </button>

          <nav class="main-nav">
            <div class="nav-group">
              <button v-for="item in USER_NAV" :key="item.key" class="nav-item"
                      :class="{ active: state.route === item.key }" @click="navigate(item.key)">
                <Icon :name="item.icon" :size="16" /><span>{{ item.label }}</span>
              </button>
            </div>
          </nav>

          <div v-if="recentSessions.length" class="sidebar__recent">
            <div class="nav-group__label">最近对话</div>
            <div v-for="session in recentSessions" :key="session.id" class="sidebar__session">
              <button class="recent-item" :class="{ active: state.activeSessionId === session.id }"
                      :title="session.name || '新对话'" @click="openConversation(session)">
                <Icon name="chat" :size="13" /><span class="truncate">{{ session.name || '新对话' }}</span>
              </button>
              <span v-if="canAdmin || (state.user && session.owner_id === state.user.id)"
                    class="sidebar__session-actions">
                <button class="icon-btn" aria-label="重命名会话" title="重命名会话"
                        @click="openSessionRename(session)"><Icon name="edit" :size="13" /></button>
                <button class="icon-btn icon-btn--danger" aria-label="删除会话" title="删除会话"
                        @click="removeSession(session)"><Icon name="trash" :size="12" /></button>
              </span>
            </div>
          </div>
        </div>

        <footer class="sidebar__foot">
          <button v-if="canAdmin" class="recent-item" :class="{ active: isAdminRoute() }"
                  @click="navigate('admin/agents')">
            <Icon name="settings" :size="15" /><span>管理后台</span>
          </button>
          <button class="recent-item" @click="state.commandOpen = true">
            <Icon name="search" :size="15" /><span>全局搜索</span>
          </button>
          <div v-if="state.user" class="sidebar__user" style="margin-top:4px">
            <span class="avatar">{{ userInitial }}</span>
            <span class="sidebar__user-meta">
              <b>{{ state.user.name || state.user.username || '企业用户' }}</b>
              <small>{{ state.workspaceRole || '成员' }}</small>
            </span>
            <span class="grow"></span>
            <button class="icon-btn" style="color:var(--sidebar-text)" aria-label="退出登录" @click="logout">
              <Icon name="close" :size="14" />
            </button>
          </div>
        </footer>
      </aside>

      <main class="main">
        <header class="topbar">
          <button class="icon-btn" aria-label="折叠导航" @click="state.sidebarCollapsed = !state.sidebarCollapsed">
            <Icon name="panelLeft" :size="17" />
          </button>
          <span class="topbar__title">{{ currentTitle() }}</span>
          <span class="topbar__sub" v-if="state.route === 'conversation' && activeSession">
            {{ activeSession.name }}
          </span>
          <span class="topbar__spacer"></span>
          <button class="topbar__search" @click="state.commandOpen = true">
            <Icon name="search" :size="15" /><span>搜索页面与动作</span><kbd>⌘K</kbd>
          </button>
          <button class="icon-btn" :aria-label="state.theme === 'dark' ? '切换浅色' : '切换深色'" @click="toggleTheme">
            <Icon :name="state.theme === 'dark' ? 'sun' : 'moon'" :size="17" />
          </button>
        </header>

        <div class="view">
          <WorkbenchView v-if="state.route === 'workbench'" :key="JSON.stringify(state.routeParams)" />
          <ConversationView v-else-if="state.route === 'conversation'" :key="JSON.stringify(state.routeParams)"
                            :session-id="state.routeParams.id" />
          <AgentsView v-else-if="state.route === 'agents'" />
          <LibraryView v-else-if="state.route === 'library'" />
          <MetricsView v-else-if="state.route === 'metrics'" />

          <div v-else-if="state.route.startsWith('admin/')" class="view--page">
            <div class="admin">
              <nav class="admin__nav">
                <div v-for="group in ADMIN_NAV" :key="group.label" class="admin__nav-group">
                  <div class="admin__nav-label">{{ group.label }}</div>
                  <button v-for="item in group.items" :key="item.key" class="admin__nav-item"
                          :class="{ active: state.route === item.key }" @click="navigate(item.key)">
                    <Icon :name="item.icon" :size="15" />{{ item.label }}
                  </button>
                </div>
              </nav>
              <div>
                <AgentBuilderView v-if="state.route === 'admin/agents'" />
                <SkillsView v-else-if="state.route === 'admin/skills'" />
                <DataView v-else-if="state.route === 'admin/data'" />
                <MetricsView v-else-if="state.route === 'admin/metrics'" :admin="true" />
                <KnowledgeView v-else-if="state.route === 'admin/knowledge'" />
                <ModelsView v-else-if="state.route === 'admin/models'" />
                <McpView v-else-if="state.route === 'admin/mcp'" />
                <IntegrationsView v-else-if="state.route === 'admin/integrations'" />
                <RunsView v-else-if="state.route === 'admin/runs'" />
                <EvaluationsView v-else-if="state.route === 'admin/evaluations'" />
                <UsersView v-else-if="state.route === 'admin/users'" />
                <SystemSettingsView v-else-if="state.route === 'admin/settings'" />
              </div>
            </div>
          </div>
        </div>
      </main>

      <Transition name="fade">
        <div v-if="state.commandOpen" class="command" @click.self="state.commandOpen = false">
          <section class="command__box">
            <div class="command__search">
              <Icon name="search" :size="17" />
              <input v-model="commandQuery" autofocus placeholder="搜索页面或动作…" @keyup.esc="state.commandOpen = false" />
            </div>
            <div class="command__list">
              <button v-for="item in filteredCommands" :key="item.kind + item.name" class="command__item"
                      @click="runCommand(item)">
                <Icon :name="item.icon" :size="15" />
                <span class="grow"><b>{{ item.label }}</b><small>{{ item.kind }}</small></span>
                <Icon name="arrowRight" :size="14" />
              </button>
            </div>
          </section>
        </div>
      </Transition>

      <Modal :open="!!sessionDialog" title="重命名会话" @close="closeSessionRename">
        <label class="field">
          <span>会话名称</span>
          <input v-model="sessionName" class="input" maxlength="100" autofocus
                 @keyup.enter="saveSessionRename" />
        </label>
        <template #footer>
          <button class="btn" :disabled="sessionSaving" @click="closeSessionRename">取消</button>
          <button class="btn btn--primary" :disabled="sessionSaving || !sessionName.trim()"
                  @click="saveSessionRename">保存</button>
        </template>
      </Modal>

      <Transition name="fade">
        <div v-if="state.busy" class="busy"><span class="spinner"></span>{{ state.busyLabel || '正在处理' }}</div>
      </Transition>
      <Toasts :items="state.toasts" />
    </div>`,
};

/* ------------------------------------------------------------------ 登录 */

const AuthScreen = {
  name: 'AuthScreen',
  components: { Icon },
  setup() {
    return { sendAuthCode, state, submitAuth };
  },
  template: `
    <div class="auth-screen">
      <div class="auth-card">
        <div class="auth-card__brand">
          <span class="sidebar__logo"><img src="/src/assets/logo-shuqing.png" alt="" /></span>
          <div><b>数擎 Data Agent</b><p class="small faint">企业级数据智能体平台</p></div>
        </div>
        <h1 class="auth-card__title">
          {{ state.authMode === 'bootstrap' ? '创建首位系统所有者'
             : state.authMode === 'register' ? '创建账号' : '登录数擎' }}
        </h1>
        <p class="auth-card__desc">
          {{ state.authMode === 'bootstrap' ? '输入部署时设置的一次性初始化令牌，创建至少 12 位密码。'
             : state.authMode === 'register' ? '使用邀请邮箱或开放注册的邮箱创建账号。'
             : '登录后即可用自然语言提问企业数据。' }}
        </p>
        <form @submit.prevent="submitAuth">
          <label v-if="state.authMode !== 'login'" class="field">
            <span>企业邮箱</span>
            <input v-model.trim="state.authForm.email" type="email" autocomplete="email" required />
          </label>
          <label class="field">
            <span>{{ state.authMode === 'login' ? '用户名' : '用户名（可选）' }}</span>
            <input v-model.trim="state.authForm.username" autocomplete="username"
                   :required="state.authMode === 'login'" />
          </label>
          <label v-if="state.authMode !== 'login'" class="field">
            <span>姓名</span>
            <input v-model.trim="state.authForm.name" autocomplete="name" />
          </label>
          <label class="field">
            <span>密码</span>
            <input v-model="state.authForm.password" type="password"
                   :autocomplete="state.authMode === 'login' ? 'current-password' : 'new-password'"
                   :minlength="state.authMode === 'login' ? undefined : 12" required />
          </label>
          <label v-if="state.authMode === 'bootstrap'" class="field">
            <span>初始化令牌</span>
            <input v-model.trim="state.authForm.bootstrapToken" type="password" autocomplete="off" required />
          </label>
          <label v-if="state.authMode !== 'login' && state.emailCodeRequired" class="field">
            <span>邮箱验证码</span>
            <input v-model.trim="state.authForm.code" inputmode="numeric" autocomplete="one-time-code" required />
          </label>
          <p v-if="state.authError" class="auth-error" role="alert">{{ state.authError }}</p>
          <p v-if="state.authNotice" class="auth-notice" role="status">{{ state.authNotice }}</p>
          <button class="btn btn--primary" type="submit">
            {{ state.authMode === 'login' ? '进入数擎' : '创建账号并进入' }}
          </button>
          <button v-if="state.authMode !== 'login' && state.emailCodeRequired" class="btn" type="button"
                  @click="sendAuthCode">发送邮箱验证码</button>
        </form>
        <p class="auth-card__foot">
          <template v-if="state.authMode === 'login'">
            <button v-if="state.registrationOpen" class="btn btn--ghost btn--sm" @click="state.authMode = 'register'">创建账号</button>
          </template>
          <button v-else class="btn btn--ghost btn--sm" @click="state.authMode = 'login'">已有账号？登录</button>
        </p>
      </div>
    </div>`,
};

const Root = {
  name: 'Root',
  components: { AuthScreen, Shell },
  setup() {
    return { state };
  },
  // 生命周期钩子必须在组件实例内注册；挂在模块顶层会静默失效。
  async mounted() {
    await bootstrap();
    applyHash();
  },
  template: `
    <div v-if="state.authChecking" class="boot-screen">
      <span class="boot-mark"><img src="/src/assets/logo-shuqing.png" alt="" /></span>
      <p>正在准备工作空间…</p>
    </div>
    <AuthScreen v-else-if="state.authRequired" />
    <Shell v-else-if="state.ready" />
  `,
};

createApp(Root).mount('#app');
