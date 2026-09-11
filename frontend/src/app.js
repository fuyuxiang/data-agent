import { api, withWorkspace } from './api.js';
import { Icon, Modal, StatusPill, ToastStack } from './components.js';
import { ChatPanel, KnowledgePanel, SemanticPanel, SourcesPanel } from './panels.js';
import { SettingsPanel } from './settings-panel.js';

const { computed, createApp, onBeforeUnmount, onMounted, reactive } = Vue;

const productRoutes = [
  { id: 'chat', label: '智能分析', icon: 'chat' },
  { id: 'sources', label: '数据管理', icon: 'database', adminOnly: true },
  { id: 'semantic', label: '指标中心', icon: 'chart', adminOnly: true },
  { id: 'knowledge', label: '业务知识', icon: 'book', adminOnly: true },
  { id: 'settings', label: '系统设置', icon: 'settings', adminOnly: true, utility: true },
];

const Root = {
  components: { ChatPanel, Icon, KnowledgePanel, Modal, SemanticPanel, SettingsPanel, SourcesPanel, StatusPill, ToastStack },
  setup() {
    const state = reactive({
      ready: false, authChecking: true, authRequired: false, registrationOpen: false,
      authMode: 'login', authError: '', bootstrapRequired: false, auth: { email:'', password:'', name:'', invitation_token:new URLSearchParams(location.search).get('invite') || '', bootstrap_token:'' }, user: null,
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
        state.user = identity.user; state.registrationOpen = !!identity.registration_open || !!state.auth.invitation_token; state.bootstrapRequired = !!identity.bootstrap_required;
        if (identity.csrf_token) sessionStorage.setItem('meridian-csrf', identity.csrf_token);
        if (!identity.authenticated && !identity.local_mode) {
          state.authRequired = true;
          state.authMode = state.auth.invitation_token ? 'register' : (state.registrationOpen ? 'register' : 'login');
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
      state.authError = '';
      try {
        const path = state.authMode === 'register' ? '/api/auth/register' : state.authMode === 'reset' ? '/api/auth/reset-password' : '/api/auth/login';
        await api(path, { method:'POST', body:state.auth });
        if (state.authMode === 'reset') {
          state.authMode = 'login'; state.auth.password = ''; state.auth.code = '';
          return;
        }
        state.authRequired = false; state.authChecking = true;
        state.auth.password = ''; state.auth.bootstrap_token = '';
        await bootstrap();
      } catch (error) { state.authError = error?.message || '认证失败'; }
    };
    const sendAuthCode = async () => {
      state.authError = '';
      try { await api('/api/auth/send-code', { method:'POST', body:{ email:state.auth.email } }); }
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
      const inheritedSourceIds = [...(previous?.source_ids || [])];
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
    const ctx = { state, toast, fail, run, activeSession, selectedSources, time, number, go, command, bootstrap, newSession, startAnalysis, openAnalysis };

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
      {id:'configuration',label:'配置',items:routes.value.filter(item=>item.id!=='chat'&&!item.utility)},
    ]);
    const activeRoute=computed(()=>productRoutes.find(item=>item.id===state.route)||productRoutes[0]);
    const userInitial=computed(()=>(state.user?.name||state.user?.email||'本')[0].toUpperCase());
    return { state, routes, routeGroups, activeRoute, userInitial, canAdmin, ctx, activeSession, selectedSources, filteredCommands, go, switchWorkspace, switchSession, newSession, openSessionDialog, closeSessionDialog, confirmSessionDialog, toggleTheme, command, submitAuth, sendAuthCode, logout };
  },
  template: `
    <div v-if="state.authChecking" class="boot-screen"><span class="boot-mark">经纬</span><p>正在验证会话…</p></div>
    <main v-else-if="state.authRequired" class="auth-screen">
      <form class="auth-panel" @submit.prevent="submitAuth">
        <header><span class="brand__mark"><i></i><i></i><i></i></span><div><h1>经纬</h1><p>企业数据分析工作台</p></div></header>
        <div class="segmented" v-if="state.registrationOpen&&!state.auth.invitation_token"><button type="button" :class="{active:state.authMode==='login'}" @click="state.authMode='login';state.authError=''">登录</button><button type="button" :class="{active:state.authMode==='register'}" @click="state.authMode='register';state.authError=''">创建所有者</button></div>
        <label v-if="state.authMode==='register'"><span>姓名</span><input v-model.trim="state.auth.name" autocomplete="name" required maxlength="80"></label>
        <label v-if="state.authMode==='register' && state.bootstrapRequired && !state.auth.invitation_token"><span>初始化令牌</span><input v-model="state.auth.bootstrap_token" type="password" autocomplete="off" required><small>由部署管理员从 MERIDIAN_BOOTSTRAP_TOKEN 安全交付。</small></label>
        <label><span>邮箱</span><input v-model.trim="state.auth.email" type="email" autocomplete="email" required></label>
        <label v-if="state.authMode!=='login'"><span>邮箱验证码</span><span class="auth-code"><input v-model.trim="state.auth.code" inputmode="numeric" maxlength="6"><button class="button button--small" type="button" @click="sendAuthCode">发送验证码</button></span></label>
        <label><span>密码</span><input v-model="state.auth.password" type="password" :autocomplete="state.authMode==='login'?'current-password':'new-password'" required minlength="12"></label>
        <p v-if="state.authError" class="auth-error">{{ state.authError }}</p>
        <button class="button button--primary" type="submit">{{ state.authMode==='register' ? (state.auth.invitation_token?'加入企业':'创建并进入') : state.authMode==='reset' ? '重置密码' : '登录' }}</button>
        <button v-if="state.authMode==='login'" class="text-button" type="button" @click="state.authMode='reset';state.authError=''">忘记密码</button>
        <button v-else-if="state.authMode==='reset'" class="text-button" type="button" @click="state.authMode='login';state.authError=''">返回登录</button>
      </form>
    </main>
    <div v-else class="app-shell" :class="{ 'sidebar-visible': state.sidebarOpen }">
      <aside class="app-sidebar">
        <header class="brand"><span class="brand__mark" aria-hidden="true"><i></i><i></i><i></i></span><div><b>经纬</b><small>Data Agent</small></div><button class="sidebar-close" @click="state.sidebarOpen=false" aria-label="关闭导航"><Icon name="close"/></button></header>
        <nav class="main-nav">
          <section v-for="group in routeGroups" :key="group.id" class="nav-group">
            <header>{{ group.label }}</header>
            <button v-for="item in group.items" :key="item.id" :class="{active:state.route===item.id}" @click="go(item.id)"><Icon :name="item.icon"/><span>{{ item.label }}</span></button>
          </section>
        </nav>
        <section class="sidebar-sessions"><header><span>分析记录</span><button @click="newSession()" title="新建分析"><Icon name="plus"/></button></header><div v-for="session in state.sessions.slice(0,5)" :key="session.id" class="sidebar-session-row" :class="{active:session.id===state.activeSessionId}"><button class="sidebar-session-main" :class="{active:session.id===state.activeSessionId}" @click="switchSession(session.id)"><i></i><span>{{ session.name }}</span><small>{{ ctx.time(session.updated_at) }}</small></button><span class="sidebar-session-actions"><button @click.stop="openSessionDialog('rename',session)" :aria-label="'重命名 '+session.name" title="重命名"><Icon name="edit" :size="14"/></button><button class="danger" @click.stop="openSessionDialog('delete',session)" :aria-label="'删除 '+session.name" title="删除"><Icon name="trash" :size="14"/></button></span></div></section>
        <footer class="sidebar-footer">
          <button v-if="canAdmin" class="sidebar-utility" :class="{active:state.route==='settings'}" @click="go('settings')"><Icon name="settings" :size="16"/><span>系统设置</span></button>
          <button class="command-entry" @click="state.commandOpen=true"><Icon name="search" :size="15"/><span>全局搜索</span><kbd>⌘ K</kbd></button>
          <div v-if="state.user" class="sidebar-profile"><span class="user-avatar">{{ userInitial }}</span><div><b>{{ state.user.name || '企业用户' }}</b><small>{{ state.workspaceRole || '成员' }}</small></div><button @click="logout" title="退出登录"><Icon name="chevron" :size="15"/></button></div>
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
