import { api, withWorkspace } from './api.js';
import { Icon, Modal, StatusPill, ToastStack } from './components.js';
import { AutomationPanel, ChatPanel, DashboardsPanel, FeishuBotPanel, KnowledgePanel, SemanticPanel, SettingsPanel, SourcesPanel } from './panels.js';

const { computed, createApp, onBeforeUnmount, onMounted, reactive } = Vue;

const routes = [
  { id: 'chat', label: '分析会话', icon: 'chat' },
  { id: 'sources', label: '数据目录', icon: 'database' },
  { id: 'knowledge', label: '业务知识', icon: 'book' },
  { id: 'semantic', label: '指标中心', icon: 'chart' },
  { id: 'automation', label: '自动化中心', icon: 'workflow' },
  { id: 'dashboards', label: '分析看板', icon: 'dashboard' },
  { id: 'feishu', label: '飞书机器人', icon: 'workflow' },
  { id: 'settings', label: '系统设置', icon: 'settings' },
];

const Root = {
  components: { AutomationPanel, ChatPanel, DashboardsPanel, FeishuBotPanel, Icon, KnowledgePanel, Modal, SemanticPanel, SettingsPanel, SourcesPanel, StatusPill, ToastStack },
  setup() {
    const state = reactive({
      ready: false, authChecking: true, authRequired: false, registrationOpen: false,
      authMode: 'login', authError: '', bootstrapRequired: false, auth: { email:'', password:'', name:'', invitation_token:new URLSearchParams(location.search).get('invite') || '', bootstrap_token:'' }, user: null,
      route: location.hash.slice(1) || 'chat', sidebarOpen: false,
      workspaceId: localStorage.getItem('meridian-workspace') || 'default', workspaces: [], workspaceRole: '',
      sessions: [], activeSessionId: '', sources: [], providers: [], skills: [], analysisMethods: [], agentProfiles: [],
      busy: false, busyLabel: '', toasts: [], jobsOpen: false, jobs: [], activeJobs: 0,
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
        const [data, skills, methods, profiles, commands] = await Promise.all([
          api(withWorkspace('/api/bootstrap', state.workspaceId)), api(withWorkspace('/api/skills', state.workspaceId)),
          api('/api/analysis/methods'), api(withWorkspace('/api/agent-profiles', state.workspaceId)), api('/api/commands'),
        ]);
        state.workspaces = data.workspaces; state.workspaceId = data.active_workspace?.id || 'default';
        state.workspaceRole = data.active_membership?.role || (!state.user ? 'owner' : '');
        state.sessions = data.sessions; state.sources = data.sources; state.providers = data.providers;
        state.activeSessionId = data.active_session?.id || data.sessions[0]?.id || '';
        state.skills = skills.items; state.analysisMethods = methods.items; state.agentProfiles = profiles.items; state.commands = commands.items;
        localStorage.setItem('meridian-workspace', state.workspaceId);
        await loadJobs(); state.ready = true;
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
    const go = (route) => { state.route = route; location.hash = route; state.sidebarOpen = false; };
    const switchWorkspace = async () => { localStorage.setItem('meridian-workspace', state.workspaceId); await bootstrap(); };
    const switchSession = async (id) => { state.activeSessionId = id; go('chat'); };
    const newSession = async (name = '新分析会话') => {
      const result = await api('/api/sessions', { method:'POST', body:{ name, workspace_id:state.workspaceId, source_ids:state.sources.slice(0,1).map(item=>item.id) } });
      state.sessions.forEach(item => item.status = 'idle'); state.sessions.unshift(result.item); state.activeSessionId = result.item.id; go('chat');
    };
    const command = async (raw) => {
      let [name,...rest]=raw.slice(1).trim().split(/\s+/); const arg=rest.join(' ');
      const aliases={n:'new',cp:'checkpoint',c:'compact',h:'help','?':'help',i:'instruction',kb:'knowledge',bot:'robot',session:'sessions',sk:'skills',s:'status',ws:'workspace'};name=aliases[name]||name;
      if(name==='new') return newSession(arg||'新分析会话');
      if(name==='data'||name==='sources'||name==='profile') return go('sources');
      if(name==='knowledge') return go('knowledge');
      if(name==='jobs') { state.jobsOpen=true; return loadJobs(); }
      if(name==='clear') { const session=activeSession();if(session)await api(`/api/sessions/${session.id}/clear`,{method:'POST'});toast('','会话上下文已清除');return; }
      if(name==='compact'){const session=activeSession();if(!session)return;await api(`/api/sessions/${session.id}/commands/compact/execute`,{method:'POST',body:{arguments:arg}});toast('','上下文已压缩');return;}
      if(name==='save') { const session=activeSession(); if(session){await api(`/api/sessions/${session.id}/save`,{method:'POST',body:{name:arg||session.name}});toast('当前消息与分析证据已保存','会话已保存');} return; }
      if(name==='instruction'){const session=activeSession();if(!session)return;const value=arg||prompt('输入仅对当前会话生效的指令：',session.temporary_instruction||'')||'';if(value){session.temporary_instruction=value;session.temp_prompt_enabled=true;await api(`/api/sessions/${session.id}`,{method:'PATCH',body:{temporary_instruction:value,temp_prompt_enabled:true}});toast('','临时指令已更新');}return;}
      if(name==='checkpoint'){const result=await api(`/api/workspaces/${state.workspaceId}/checkpoints`);toast(`共 ${result.items.length} 个可恢复快照`,'快照与历史');return;}
      if(name==='teams'){localStorage.setItem('meridian-automation-tab','teams');return go('automation');}
      if(name==='robot') return go('feishu');
      if(name==='mcp'||name==='skills'||name==='workspace'){localStorage.setItem('meridian-settings-tab',name==='skills'?'skills':name==='mcp'?'mcp':'compute');return go('settings');}
      if(name==='sessions'){if(arg==='new')return newSession();toast(`${state.sessions.length} 个当前工作空间会话`,'会话');return;}
      if(name==='status'){const session=activeSession();toast(`${selectedSources().length} 个数据源 · ${session?.provider_id||'默认模型'}`,'当前状态');return;}
      if(name==='help'){state.commandQuery=arg;state.commandOpen=true;return;}
      state.commandQuery=name;state.commandOpen=true;
    };
    const loadJobs = async () => { try { const result=await api(withWorkspace('/api/jobs?limit=50',state.workspaceId));state.jobs=result.items;state.activeJobs=state.jobs.filter(item=>['queued','running','waiting_approval'].includes(item.status)).length; } catch{/* background refresh */} };
    const toggleTheme = () => { state.theme=state.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=state.theme;localStorage.setItem('meridian-theme',state.theme); };
    const ctx = { state, toast, fail, run, activeSession, selectedSources, time, number, go, command };

    let jobTimer;
    const keydown = (event) => {
      if ((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k') { event.preventDefault();state.commandOpen=true; }
      if (event.key==='Escape') { state.commandOpen=false;state.jobsOpen=false; }
    };
    onMounted(()=>{bootstrap();window.addEventListener('keydown',keydown);window.addEventListener('hashchange',()=>{state.route=location.hash.slice(1)||'chat'});jobTimer=setInterval(loadJobs,4000);});
    onBeforeUnmount(()=>{clearInterval(jobTimer);window.removeEventListener('keydown',keydown);});
    const filteredCommands=computed(()=>state.commands.filter(item=>(item.name+' '+item.description).toLowerCase().includes(state.commandQuery.toLowerCase())));
    return { state, routes, ctx, activeSession, selectedSources, filteredCommands, go, switchWorkspace, switchSession, newSession, loadJobs, toggleTheme, command, submitAuth, sendAuthCode, logout };
  },
  template: `
    <div v-if="state.authChecking" class="boot-screen"><span class="boot-mark">经纬</span><p>正在验证会话…</p></div>
    <main v-else-if="state.authRequired" class="auth-screen">
      <form class="auth-panel" @submit.prevent="submitAuth">
        <header><span class="brand__mark"><i></i><i></i><i></i></span><div><h1>经纬</h1><p>企业数据分析工作台</p></div></header>
        <div class="segmented" v-if="state.registrationOpen"><button type="button" :class="{active:state.authMode==='login'}" @click="state.authMode='login';state.authError=''">登录</button><button type="button" :class="{active:state.authMode==='register'}" @click="state.authMode='register';state.authError=''">创建所有者</button></div>
        <label v-if="state.authMode==='register'"><span>姓名</span><input v-model.trim="state.auth.name" autocomplete="name" required maxlength="80"></label>
        <label v-if="state.authMode==='register' && state.bootstrapRequired && !state.auth.invitation_token"><span>初始化令牌</span><input v-model="state.auth.bootstrap_token" type="password" autocomplete="off" required><small>由部署管理员从 MERIDIAN_BOOTSTRAP_TOKEN 安全交付。</small></label>
        <label><span>邮箱</span><input v-model.trim="state.auth.email" type="email" autocomplete="email" required></label>
        <label v-if="state.authMode!=='login'"><span>邮箱验证码</span><span class="auth-code"><input v-model.trim="state.auth.code" inputmode="numeric" maxlength="6"><button class="button button--small" type="button" @click="sendAuthCode">发送验证码</button></span></label>
        <label><span>密码</span><input v-model="state.auth.password" type="password" :autocomplete="state.authMode==='login'?'current-password':'new-password'" required minlength="12"></label>
        <p v-if="state.authError" class="auth-error">{{ state.authError }}</p>
        <button class="button button--primary" type="submit">{{ state.authMode==='register' ? '创建并进入' : state.authMode==='reset' ? '重置密码' : '登录' }}</button>
        <button v-if="state.authMode==='login'" class="text-button" type="button" @click="state.authMode='reset';state.authError=''">忘记密码</button>
        <button v-else-if="state.authMode==='reset'" class="text-button" type="button" @click="state.authMode='login';state.authError=''">返回登录</button>
      </form>
    </main>
    <div v-else class="app-shell" :class="{ 'sidebar-visible': state.sidebarOpen }">
      <aside class="app-sidebar">
        <header class="brand"><span class="brand__mark"><i></i><i></i><i></i></span><div><b>经纬</b><small>ANALYTICS WORKBENCH</small></div><button class="sidebar-close" @click="state.sidebarOpen=false"><Icon name="close"/></button></header>
        <div class="workspace-switcher"><label>分析空间</label><select v-model="state.workspaceId" @change="switchWorkspace"><option v-for="item in state.workspaces" :key="item.id" :value="item.id">{{ item.name }}</option></select></div>
        <nav class="main-nav"><button v-for="item in routes" :key="item.id" :class="{active:state.route===item.id}" @click="go(item.id)"><Icon :name="item.icon"/><span>{{ item.label }}</span><b v-if="item.id==='automation'&&state.activeJobs">{{ state.activeJobs }}</b></button></nav>
        <section class="sidebar-sessions"><header><span>最近会话</span><button @click="newSession()" title="新会话"><Icon name="plus"/></button></header><button v-for="session in state.sessions.slice(0,6)" :key="session.id" :class="{active:session.id===state.activeSessionId}" @click="switchSession(session.id)"><i></i><span>{{ session.name }}</span><small>{{ ctx.time(session.updated_at) }}</small></button></section>
        <footer class="sidebar-footer"><button @click="state.commandOpen=true"><span>⌘K</span>命令面板</button><button v-if="state.user" @click="logout"><span>{{ state.user.name }}</span>退出</button><div><i :class="{on:state.sources.length}"></i>{{ state.sources.length }} 个数据源已登记</div></footer>
      </aside>
      <main class="app-main">
        <div class="mobile-bar"><button class="icon-button" @click="state.sidebarOpen=true" aria-label="打开导航">☰</button><b>经纬分析工作台</b><button class="icon-button" @click="toggleTheme"><Icon :name="state.theme==='dark'?'sun':'moon'"/></button></div>
        <ChatPanel v-if="state.route==='chat'" :ctx="ctx"/>
        <SourcesPanel v-else-if="state.route==='sources'" :ctx="ctx"/>
        <KnowledgePanel v-else-if="state.route==='knowledge'" :ctx="ctx" :key="state.workspaceId"/>
        <SemanticPanel v-else-if="state.route==='semantic'" :ctx="ctx" :key="state.workspaceId"/>
        <AutomationPanel v-else-if="state.route==='automation'" :ctx="ctx" :key="state.workspaceId"/>
        <DashboardsPanel v-else-if="state.route==='dashboards'" :ctx="ctx" :key="state.workspaceId"/>
        <FeishuBotPanel v-else-if="state.route==='feishu'" :ctx="ctx" :key="state.workspaceId+'-'+state.activeSessionId"/>
        <SettingsPanel v-else :ctx="ctx" :key="state.workspaceId"/>
      </main>
      <button class="theme-fab" @click="toggleTheme" :title="state.theme==='dark'?'切换浅色':'切换深色'"><Icon :name="state.theme==='dark'?'sun':'moon'"/></button>
      <Transition name="slide"><aside v-if="state.jobsOpen" class="task-drawer"><header><div><span class="eyebrow">Background activity</span><h2>任务与运行</h2></div><button class="icon-button" @click="state.jobsOpen=false"><Icon name="close"/></button></header><button class="button button--small" @click="loadJobs"><Icon name="refresh"/>刷新</button><div class="drawer-list"><article v-for="job in state.jobs" :key="job.id"><div><b>{{ job.title }}</b><StatusPill :status="job.status"/></div><p>{{ job.message }}</p><div class="progress"><i :style="{width:(job.progress||0)+'%'}"></i></div><small>{{ Math.round(job.progress||0) }}% · {{ ctx.time(job.updated_at) }}</small><pre v-if="job.error">{{ job.error }}</pre></article><p v-if="!state.jobs.length" class="drawer-empty">当前没有后台任务。</p></div></aside></Transition>
      <Transition name="fade"><div v-if="state.commandOpen" class="command-backdrop" @click.self="state.commandOpen=false"><section class="command-palette"><div class="command-search"><Icon name="search"/><input autofocus v-model="state.commandQuery" placeholder="搜索命令…" @keyup.esc="state.commandOpen=false"></div><div class="command-list"><button v-for="item in filteredCommands" :key="item.name" @click="command('/'+item.name);state.commandOpen=false"><span>/{{ item.name }}</span><div><b>{{ item.description }}</b><small>{{ item.usage }}</small></div><Icon name="chevron"/></button></div></section></div></Transition>
      <Transition name="fade"><div v-if="state.busy" class="busy-overlay"><span class="spinner"></span><b>{{ state.busyLabel || '正在处理' }}</b></div></Transition>
      <ToastStack :items="state.toasts"/>
    </div>`,
};

createApp(Root).mount('#app');
