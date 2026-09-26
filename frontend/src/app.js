import { api, withWorkspace } from './api.js';
import { Icon, Modal, StatusPill, ToastStack } from './components.js';
import { ChatPanel, KnowledgePanel, SemanticPanel, SourcesPanel } from './panels.js';
import { SettingsPanel } from './settings-panel.js';

const { computed, createApp, onBeforeUnmount, onMounted, reactive } = Vue;

const productRoutes = [
  { id: 'chat', label: '智能分析', icon: 'chat' },
  { id: 'sources', label: '数据资产', icon: 'database', adminOnly: true },
  { id: 'semantic', label: '指标中心', icon: 'chart', adminOnly: true },
  { id: 'knowledge', label: '知识库', icon: 'book', adminOnly: true },
  { id: 'settings', label: '系统管理', icon: 'settings', adminOnly: true, utility: true },
];

const Root = {
  components: { ChatPanel, Icon, KnowledgePanel, Modal, SemanticPanel, SettingsPanel, SourcesPanel, StatusPill, ToastStack },
  setup() {
    const state = reactive({
      ready: false, authChecking: true, authRequired: false, registrationOpen: false, emailCodeRequired: false,
      authMode: 'login', authError: '', authNotice: '', bootstrapRequired: false,
      auth: { username:'', email:'', name:'', password:'', bootstrap_token:'', code:'' }, user: null,
      route: location.hash.slice(1) || 'chat', sidebarOpen: false,
      workspaceId: localStorage.getItem('meridian-workspace') || 'default', workspaces: [], workspaceRole: '',
      sessions: [], activeSessionId: '', sources: [], providers: [],
      pendingPrompt: '',
      busy: false, busyLabel: '', toasts: [],
      sessionDialog: { mode: '', id: '', name: '' },
      commandOpen: false, commands: [], commandQuery: '', theme: document.documentElement.dataset.theme || 'light',
    });

    const toast = (message, title = '完成', tone = 'success') => {
      const item = { id: Date.now() + Math.random(), title, message, tone };
      state.toasts.push(item); setTimeout(() => { state.toasts = state.toasts.filter(value => value.id !== item.id); }, 3800);
    };
    const fail = (error) => { console.error(error); toast(error?.message || '操作未完成', '出现问题', 'error'); };
    const run = async (label, action, announce = true) => {
      state.busy = true; state.busyLabel = label;
      try { const result = await action(); if (announce && label) toast('', label.replace(/^正在/, '').replace(/中$/, '') + '完成'); return result; }
      catch (error) { fail(error); throw error; }
      finally { state.busy = false; state.busyLabel = ''; }
    };
    const activeSession = () => state.sessions.find(item => item.id === state.activeSessionId) || null;
    const pruneSessionSourceIds = (sessions = state.sessions, sources = state.sources) => {
      const visibleSourceIds = new Set((sources || []).map(item => String(item.id)));
      (sessions || []).forEach(session => {
        session.source_ids = [...new Set((session.source_ids || []).map(String).filter(id => visibleSourceIds.has(id)))];
      });
    };
    const selectedSources = () => { const ids = new Set(activeSession()?.source_ids || []); return state.sources.filter(item => ids.has(item.id)); };
    const time = (value) => {
      if (!value) return '';
      try { return new Intl.DateTimeFormat('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' }).format(new Date(value)); } catch { return value; }
    };
    const number = (value) => new Intl.NumberFormat('zh-CN').format(value || 0);

    const bootstrap = async () => {
      state.busy = true; state.busyLabel = '正在准备工作空间';
      try {
        const identity = await api('/api/auth/me');
        state.user = identity.user; state.registrationOpen = !!identity.registration_open; state.bootstrapRequired = !!identity.bootstrap_required; state.emailCodeRequired = !!identity.email_code_required;
        if (identity.csrf_token) sessionStorage.setItem('meridian-csrf', identity.csrf_token);
        if (!identity.authenticated && !identity.local_mode) {
          state.authRequired = true;
          state.authMode = identity.bootstrap_required ? 'bootstrap' : new URLSearchParams(location.search).has('invite') ? 'register' : 'login';
          state.ready = true;
          return;
        }
        state.authRequired = false;
        const [data, commands] = await Promise.all([
          api(withWorkspace('/api/bootstrap', state.workspaceId)), api('/api/commands'),
        ]);
        state.workspaces = data.workspaces; state.workspaceId = data.active_workspace?.id || 'default';
        state.workspaceRole = data.active_membership?.role || (!state.user ? 'owner' : '');
        state.sessions = data.sessions; state.sources = data.sources; state.providers = data.providers;
        pruneSessionSourceIds();
        state.activeSessionId = data.active_session?.id || data.sessions[0]?.id || '';
        state.commands = commands.items;
        const canAdmin = ['owner','editor'].includes(state.workspaceRole);
        const allowed = productRoutes.filter(item => canAdmin || !item.adminOnly);
        if (!allowed.some(item=>item.id===state.route)) { state.route = 'chat'; location.hash = state.route; }
        localStorage.setItem('meridian-workspace', state.workspaceId);
        state.ready = true;
      } catch (error) {
        if (error?.status === 401) state.authRequired = true; else fail(error);
      }
      finally { state.busy = false; state.busyLabel = ''; state.authChecking = false; }
    };
    const submitAuth = async () => {
      state.authError = ''; state.authNotice = '';
      try {
        const registering = state.authMode !== 'login';
        const response = await api(registering ? '/api/auth/register' : '/api/auth/login', {
          method:'POST',
          body: registering ? {
            email: state.auth.email, name: state.auth.name, username: state.auth.username,
            password: state.auth.password, bootstrap_token: state.auth.bootstrap_token,
            invitation_token: new URLSearchParams(location.search).get('invite') || '', code: state.auth.code,
          } : { username: state.auth.username, password: state.auth.password },
        });
        if (response.active_workspace_id) {
          state.workspaceId = response.active_workspace_id;
          localStorage.setItem('meridian-workspace', state.workspaceId);
        }
        if (registering && new URLSearchParams(location.search).has('invite')) history.replaceState(null, '', location.pathname + location.hash);
        state.authRequired = false; state.authChecking = true;
        state.auth.password = ''; state.auth.bootstrap_token = ''; state.auth.code = '';
        await bootstrap();
      } catch (error) { state.authError = error?.message || '认证失败'; }
    };
    const sendAuthCode = async () => {
      state.authError = ''; state.authNotice = '';
      try {
        const response = await api('/api/auth/send-code', { method:'POST', body:{ email:state.auth.email } });
        state.authNotice = response.message || '验证码已发送，请检查邮箱';
      }
      catch (error) { state.authError = error?.message || '验证码发送失败'; }
    };
    const logout = async () => {
      await api('/api/auth/logout', { method:'POST' });
      sessionStorage.removeItem('meridian-csrf');
      state.user = null; state.authRequired = true; state.authMode = 'login';
    };
    const go = (route) => {
      const item = productRoutes.find(value=>value.id===route);
      if (!item) return;
      if (item.adminOnly && !['owner','editor'].includes(state.workspaceRole)) return;
      state.route = route; location.hash = route; state.sidebarOpen = false;
    };
    const switchWorkspace = async () => { localStorage.setItem('meridian-workspace', state.workspaceId); await bootstrap(); };
    const switchSession = async (id) => { state.activeSessionId = id; go('chat'); };
    const openSessionDialog = (mode, session) => {
      state.sessionDialog = { mode, id: session.id, name: session.name || '' };
    };
    const closeSessionDialog = () => { state.sessionDialog = { mode: '', id: '', name: '' }; };
    const confirmSessionDialog = async () => {
      const dialog = state.sessionDialog;
      const session = state.sessions.find(item => item.id === dialog.id);
      if (!session) return closeSessionDialog();
      if (dialog.mode === 'rename') {
        const name = dialog.name.trim();
        if (!name) return fail(new Error('分析记录名称不能为空'));
        try {
          const response = await api(`/api/sessions/${session.id}`, { method: 'PATCH', body: { name } });
          Object.assign(session, response.item); closeSessionDialog(); toast('', '已重命名');
        } catch (error) { fail(error); }
        return;
      }
      if (dialog.mode === 'delete') {
        try {
          await api(`/api/sessions/${session.id}`, { method: 'DELETE' });
          state.sessions = state.sessions.filter(item => item.id !== session.id);
          const deletedActive = state.activeSessionId === session.id;
          closeSessionDialog();
          if (deletedActive) {
            if (state.sessions.length) await switchSession(state.sessions[0].id);
            else await newSession();
          }
          toast('数据源和已发布成果不受影响', '分析记录已删除');
        } catch (error) { fail(error); }
      }
    };
    const newSession = async (name = '新分析') => {
      const previous = activeSession();
      const visibleSourceIds = new Set(state.sources.map(item => String(item.id)));
      const inheritedSourceIds = [...(previous?.source_ids || [])].map(String).filter(id => visibleSourceIds.has(id));
      const result = await api('/api/sessions', { method:'POST', body:{
        name, workspace_id:state.workspaceId, source_ids:inheritedSourceIds,
      } });
      state.sessions.forEach(item => item.status = 'idle'); state.sessions.unshift(result.item); state.activeSessionId = result.item.id; go('chat');
      return result.item;
    };
    const startAnalysis = async (question = '') => {
      const current = activeSession();
      if (!current) await newSession('新分析');
      state.pendingPrompt = question; go('chat');
    };
    const openAnalysis = (item) => {
      const session = state.sessions.find(value=>value.id===item.session_id);
      if (session) state.activeSessionId = session.id;
      state.pendingPrompt = ''; go('chat');
    };
    const command = async (raw) => {
      let [name,...rest]=raw.slice(1).trim().split(/\s+/); const arg=rest.join(' ');
      const aliases={n:'new',c:'compact',h:'help','?':'help',i:'instruction',kb:'knowledge',session:'sessions',s:'status',ws:'workspace'};name=aliases[name]||name;
      if(name==='new') return newSession(arg||'新分析会话');
      if(name==='data'||name==='sources'||name==='profile') return go('sources');
      if(name==='knowledge') return go('knowledge');
      if(name==='clear') { const session=activeSession();if(session)await api(`/api/sessions/${session.id}/clear`,{method:'POST'});toast('','会话上下文已清除');return; }
      if(name==='compact'){const session=activeSession();if(!session)return;await api(`/api/sessions/${session.id}/commands/compact/execute`,{method:'POST',body:{arguments:arg}});toast('','上下文已压缩');return;}
      if(name==='save') { const session=activeSession(); if(session){await api(`/api/sessions/${session.id}/save`,{method:'POST',body:{name:arg||session.name}});toast('当前消息与分析证据已保存','会话已保存');} return; }
      if(name==='instruction'){const session=activeSession();if(!session)return;const value=arg||prompt('输入仅对当前会话生效的指令：',session.temporary_instruction||'')||'';if(value){session.temporary_instruction=value;session.temp_prompt_enabled=true;await api(`/api/sessions/${session.id}`,{method:'PATCH',body:{temporary_instruction:value,temp_prompt_enabled:true}});toast('','临时指令已更新');}return;}
      if(name==='mcp'||name==='workspace'){localStorage.setItem('meridian-settings-tab',name==='mcp'?'tools':'members');return go('settings');}
      if(name==='sessions'){if(arg==='new')return newSession();toast(`${state.sessions.length} 个当前工作空间会话`,'会话');return;}
      if(name==='status'){const session=activeSession();toast(`${selectedSources().length} 个数据源 · ${session?.provider_id||'默认模型'}`,'当前状态');return;}
      if(name==='help'){state.commandQuery=arg;state.commandOpen=true;return;}
      state.commandQuery=name;state.commandOpen=true;
    };
    const toggleTheme = () => { state.theme=state.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=state.theme;localStorage.setItem('meridian-theme',state.theme); };
    const ctx = { state, toast, fail, run, activeSession, selectedSources, pruneSessionSourceIds, time, number, go, command, bootstrap, newSession, startAnalysis, openAnalysis };

    const keydown = (event) => {
      if ((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k') { event.preventDefault();state.commandOpen=true; }
      if (event.key==='Escape') state.commandOpen=false;
    };
    const syncHash=()=>go(location.hash.slice(1)||'chat');
    onMounted(()=>{bootstrap();window.addEventListener('keydown',keydown);window.addEventListener('hashchange',syncHash);});
    onBeforeUnmount(()=>{window.removeEventListener('keydown',keydown);window.removeEventListener('hashchange',syncHash);});
    const filteredCommands=computed(()=>state.commands.filter(item=>(item.name+' '+item.description).toLowerCase().includes(state.commandQuery.toLowerCase())));
    const canAdmin=computed(()=>['owner','editor'].includes(state.workspaceRole));
    const routes=computed(()=>productRoutes.filter(item=>canAdmin.value||!item.adminOnly));
    const routeGroups=computed(()=>[
      {id:'analysis',label:'分析',items:routes.value.filter(item=>item.id==='chat')},
      {id:'configuration',label:'数据治理',items:routes.value.filter(item=>item.id!=='chat'&&!item.utility)},
    ]);
    const activeRoute=computed(()=>productRoutes.find(item=>item.id===state.route)||productRoutes[0]);
    const userInitial=computed(()=>(state.user?.name||state.user?.username||state.user?.email||'本')[0].toUpperCase());
    return { state, routes, routeGroups, activeRoute, userInitial, canAdmin, ctx, activeSession, selectedSources, filteredCommands, go, switchWorkspace, switchSession, newSession, openSessionDialog, closeSessionDialog, confirmSessionDialog, toggleTheme, command, submitAuth, sendAuthCode, logout };
  },
  template: `
    <div v-if="state.authChecking" class="boot-screen"><span class="boot-mark boot-mark--logo"><img src="/src/assets/logo-shuqing.png" alt="数擎" /></span><p>正在验证会话…</p></div>
    <main v-else-if="state.authRequired" class="auth-screen portal-screen">
      <section class="portal-shell">
        <div class="portal-hero">
          <header class="portal-brand"><span class="brand__mark brand__mark--image"><img src="/src/assets/logo-shuqing.png" alt="数擎" /></span><div><b>数擎 Data Agent</b><small>教育数据智能分析平台</small></div></header>
          <p class="portal-eyebrow">EDU DATA INTELLIGENCE PLATFORM</p>
          <h1>统一数据、知识与指标<br>构建可信分析闭环</h1>
          <p class="portal-summary">面向成绩分析、课程评价与就业质量分析，统一数据资产、指标口径、业务知识和智能分析流程，让结论可核验、过程可追踪、成果可交付。</p>
          <div class="portal-workflow">
            <span><i>01</i><b>多源数据接入</b></span>
            <span><i>02</i><b>指标口径治理</b></span>
            <span><i>03</i><b>智能分析执行</b></span>
            <span><i>04</i><b>报告成果交付</b></span>
          </div>
          <div class="portal-metrics">
            <span><b>成绩分析</b><small>分布、排名、班级对比与薄弱项识别</small></span>
            <span><b>课程评价</b><small>课程质量、教学反馈与改进建议</small></span>
            <span><b>就业质量</b><small>岗位去向、专业匹配与就业趋势研判</small></span>
          </div>
          <div class="portal-proof">
            <em>SQL 只读查询</em><em>证据单元格校验</em><em>指标版本留痕</em><em>报告一键生成</em>
          </div>
        </div>
        <form class="auth-panel portal-login" @submit.prevent="submitAuth">
          <div class="login-product-mark"><span class="brand__mark brand__mark--image"><img src="/src/assets/logo-shuqing.png" alt="数擎" /></span><b>数擎</b></div>
          <div class="login-heading">
            <span>Secure Workspace</span>
            <h2>{{ state.authMode==='bootstrap' ? '创建首位系统所有者' : state.authMode==='register' ? '创建账号' : '登录数擎平台' }}</h2>
            <p>{{ state.authMode==='bootstrap' ? '输入部署时设置的一次性初始化令牌，创建至少 12 位密码。' : state.authMode==='register' ? '使用邀请邮箱或开放注册的邮箱创建账号。' : '请输入授权账号，进入数据智能分析工作空间。' }}</p>
          </div>
          <label v-if="state.authMode!=='login'"><span>企业邮箱</span><input v-model.trim="state.auth.email" type="email" autocomplete="email" placeholder="name@company.com" required autofocus></label>
          <label><span>用户名</span><input v-model.trim="state.auth.username" autocomplete="username" placeholder="请输入用户名" :required="state.authMode==='login'"></label>
          <label v-if="state.authMode!=='login'"><span>姓名</span><input v-model.trim="state.auth.name" autocomplete="name" placeholder="请输入姓名"></label>
          <label><span>密码</span><input v-model="state.auth.password" type="password" :autocomplete="state.authMode==='login'?'current-password':'new-password'" :minlength="state.authMode==='login'?undefined:12" placeholder="请输入密码" required></label>
          <label v-if="state.authMode==='bootstrap'"><span>初始化令牌</span><input v-model.trim="state.auth.bootstrap_token" type="password" autocomplete="off" placeholder="部署时设置的 MERIDIAN_BOOTSTRAP_TOKEN" required></label>
          <label v-if="state.authMode!=='login' && state.emailCodeRequired"><span>邮箱验证码</span><input v-model.trim="state.auth.code" inputmode="numeric" autocomplete="one-time-code" placeholder="请输入邮件中的验证码" required></label>
          <p v-if="state.authError" class="auth-error">{{ state.authError }}</p>
          <p v-if="state.authNotice" class="auth-notice" role="status">{{ state.authNotice }}</p>
          <button class="button button--primary portal-submit" type="submit">{{ state.authMode==='login'?'进入数擎平台':'创建账号并进入' }}</button>
          <button v-if="state.authMode!=='login' && state.emailCodeRequired" class="button" type="button" @click="sendAuthCode">发送邮箱验证码</button>
          <button v-if="state.authMode==='login' && state.registrationOpen" class="button" type="button" @click="state.authMode='register'">创建账号</button>
          <button v-if="state.authMode!=='login' && !state.bootstrapRequired" class="button" type="button" @click="state.authMode='login'">已有账号？登录</button>
          <div class="portal-login-assurance"><span>受控数据访问</span><span>可验证分析结论</span><span>全流程审计留痕</span></div>
          <p class="portal-login-note">登录后可使用智能分析、数据资产、指标中心、知识库和系统管理功能。</p>
        </form>
      </section>
    </main>
    <div v-else class="app-shell" :class="{ 'sidebar-visible': state.sidebarOpen }">
      <aside class="app-sidebar">
        <header class="brand"><span class="brand__mark brand__mark--image" aria-hidden="true"><img src="/src/assets/logo-shuqing.png" alt="" /></span><div><b>数擎</b><small>Data Agent</small></div><button class="sidebar-close" @click="state.sidebarOpen=false" aria-label="关闭导航"><Icon name="close"/></button></header>
        <nav class="main-nav">
          <section v-for="group in routeGroups" :key="group.id" class="nav-group">
            <header>{{ group.label }}</header>
            <button v-for="item in group.items" :key="item.id" :class="{active:state.route===item.id}" @click="go(item.id)"><Icon :name="item.icon"/><span>{{ item.label }}</span></button>
          </section>
        </nav>
        <section class="sidebar-sessions"><header><span>分析记录</span><button @click="newSession()" title="新建分析"><Icon name="plus"/></button></header><div class="sidebar-session-list"><div v-for="session in state.sessions" :key="session.id" class="sidebar-session-row" :class="{active:session.id===state.activeSessionId}"><button class="sidebar-session-main" :class="{active:session.id===state.activeSessionId}" @click="switchSession(session.id)"><i></i><span>{{ session.name }}</span><small>{{ ctx.time(session.updated_at) }}</small></button><span class="sidebar-session-actions"><button @click.stop="openSessionDialog('rename',session)" :aria-label="'重命名 '+session.name" title="重命名"><Icon name="edit" :size="14"/></button><button class="danger" @click.stop="openSessionDialog('delete',session)" :aria-label="'删除 '+session.name" title="删除"><Icon name="trash" :size="14"/></button></span></div></div></section>
        <footer class="sidebar-footer">
          <button v-if="canAdmin" class="sidebar-utility" :class="{active:state.route==='settings'}" @click="go('settings')"><Icon name="settings" :size="16"/><span>系统管理</span></button>
          <button class="command-entry" @click="state.commandOpen=true"><Icon name="search" :size="15"/><span>全局搜索</span><kbd>⌘ K</kbd></button>
          <div v-if="state.user" class="sidebar-profile"><span class="user-avatar">{{ userInitial }}</span><div><b>{{ state.user.name || state.user.username || '企业用户' }}</b><small>{{ state.workspaceRole || '成员' }}</small></div><button @click="logout" title="退出登录"><Icon name="chevron" :size="15"/></button></div>
        </footer>
      </aside>
      <main class="app-main">
        <header class="global-header">
          <div class="global-context"><b>{{ state.route==='chat' ? (activeSession()?.name || '新分析') : activeRoute.label }}</b><span>{{ state.workspaces.find(item=>item.id===state.workspaceId)?.name || '企业工作空间' }}<template v-if="state.route==='chat'"> · {{ selectedSources().length ? selectedSources().length+' 个数据源' : '尚未选择数据' }}</template></span></div>
          <button class="global-search" @click="state.commandOpen=true"><Icon name="search" :size="16"/><span>搜索分析、指标和数据资产</span><kbd>⌘ K</kbd></button>
          <div class="global-actions"><button class="icon-button" @click="toggleTheme" :aria-label="state.theme==='dark'?'切换浅色模式':'切换深色模式'"><Icon :name="state.theme==='dark'?'sun':'moon'" :size="17"/></button><span class="top-avatar">{{ userInitial }}</span></div>
        </header>
        <div class="mobile-bar"><button class="icon-button" @click="state.sidebarOpen=true" aria-label="打开导航">☰</button><b>{{ activeRoute.label }}</b><button class="icon-button" @click="toggleTheme"><Icon :name="state.theme==='dark'?'sun':'moon'"/></button></div>
        <div class="app-view">
          <ChatPanel v-if="state.route==='chat'" :ctx="ctx"/>
          <SourcesPanel v-else-if="state.route==='sources'" :ctx="ctx"/>
          <KnowledgePanel v-else-if="state.route==='knowledge'" :ctx="ctx" :key="state.workspaceId"/>
          <SemanticPanel v-else-if="state.route==='semantic'" :ctx="ctx" :key="state.workspaceId"/>
          <SettingsPanel v-else-if="state.route==='settings'" :ctx="ctx" :key="state.workspaceId"/>
          <ChatPanel v-else :ctx="ctx"/>
        </div>
      </main>
      <Transition name="fade"><div v-if="state.commandOpen" class="command-backdrop" @click.self="state.commandOpen=false"><section class="command-palette"><div class="command-search"><Icon name="search"/><input autofocus v-model="state.commandQuery" placeholder="搜索命令…" @keyup.esc="state.commandOpen=false"></div><div class="command-list"><button v-for="item in filteredCommands" :key="item.name" @click="command('/'+item.name);state.commandOpen=false"><span>/{{ item.name }}</span><div><b>{{ item.description }}</b><small>{{ item.usage }}</small></div><Icon name="chevron"/></button></div></section></div></Transition>
      <Modal :open="!!state.sessionDialog.mode" :title="state.sessionDialog.mode==='rename' ? '重命名分析记录' : '删除分析记录'" @close="closeSessionDialog">
        <label v-if="state.sessionDialog.mode==='rename'" class="dialog-field"><span>记录名称</span><input v-model.trim="state.sessionDialog.name" maxlength="100" autofocus @keyup.enter="confirmSessionDialog"></label>
        <div v-else class="delete-session-copy"><p>确定删除“{{ state.sessionDialog.name }}”吗？</p><small>该记录将从分析列表中移除，不会删除关联数据源。</small></div>
        <template #footer><button class="button" @click="closeSessionDialog">取消</button><button class="button" :class="state.sessionDialog.mode==='delete' ? 'button--danger' : 'button--primary'" @click="confirmSessionDialog">{{ state.sessionDialog.mode==='delete' ? '删除' : '保存' }}</button></template>
      </Modal>
      <Transition name="fade"><div v-if="state.busy" class="busy-overlay"><span class="spinner"></span><b>{{ state.busyLabel || '正在处理' }}</b></div></Transition>
      <ToastStack :items="state.toasts"/>
    </div>`,
};

createApp(Root).mount('#app');
