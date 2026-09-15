import { api, withWorkspace } from './api.js';
import { DataTable, EmptyState, Icon, StatusPill } from './components.js';

export const SettingsPanel = {
  components: { DataTable, EmptyState, Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    tab: localStorage.getItem('meridian-settings-tab') || 'members',
    members: [], providers: [], tools: [], audit: [], inviteResult: null, providerTests: {},
    memberForm: { email: '', role: 'analyst' },
    providerForm: {
      name: 'OpenAI Compatible', base_url: 'https://api.openai.com/v1',
      model: 'gpt-4.1-mini', api_key: '', temperature: 0.2,
    },
    toolForm: {
      name: '工具服务', transport: 'streamable-http', url: '', command: '',
      argsText: '[]', headersText: '{}', envText: '{}',
    },
  }),
  mounted() {
    if (!['members', 'models', 'tools', 'audit'].includes(this.tab)) this.tab = 'members';
    this.load();
  },
  watch: { tab(value) { localStorage.setItem('meridian-settings-tab', value); } },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const [members, providers, tools, audit] = await Promise.all([
        api(`/api/workspaces/${wid}/members`), api('/api/providers'),
        api(withWorkspace('/api/mcp/servers', wid)), api(withWorkspace('/api/audit?limit=100', wid)),
      ]);
      this.members = members.items || [];
      this.providers = providers.items || [];
      this.tools = tools.items || [];
      this.audit = audit.items || [];
    },
    async addMember() {
      if (!this.memberForm.email.trim()) return;
      await this.ctx.run('正在添加成员', async () => {
        this.inviteResult = null;
        try {
          await api(`/api/workspaces/${this.ctx.state.workspaceId}/members`, { method: 'POST', body: this.memberForm });
        } catch (error) {
          if (error.status !== 404) throw error;
          const invitation = await api(`/api/workspaces/${this.ctx.state.workspaceId}/invitations`, { method: 'POST', body: this.memberForm });
          this.inviteResult = { ...invitation, invitation_url: new URL(invitation.registration_url, window.location.origin).href };
        }
        this.memberForm.email = '';
        await this.load();
      });
    },
    async setMemberRole(item) {
      await api(`/api/workspaces/${this.ctx.state.workspaceId}/members/${item.user_id}`, { method: 'PATCH', body: { role: item.role } });
      this.ctx.toast('', '成员角色已更新');
    },
    async removeMember(item) {
      if (!window.confirm(`移除成员 ${item.name || item.email}？`)) return;
      await api(`/api/workspaces/${this.ctx.state.workspaceId}/members/${item.user_id}`, { method: 'DELETE' });
      await this.load();
    },
    async saveProvider() {
      await this.ctx.run('正在保存模型配置', async () => {
        await api('/api/providers', { method: 'POST', body: this.providerForm });
        this.providerForm.api_key = '';
        await this.load();
      });
    },
    async testProvider(item) {
      this.providerTests[item.id] = { status: 'running', text: '正在连接模型服务…' };
      try {
        const result = await api(`/api/providers/${item.id}/test`, { method: 'POST' });
        this.providerTests[item.id] = {
          status: 'success', text: `连接正常 · ${result.result.model} · ${result.result.latency_ms} ms`,
        };
      } catch (error) {
        this.providerTests[item.id] = { status: 'error', text: error?.message || '模型连接失败' };
      }
    },
    async removeProvider(item) {
      if (item.id === 'environment-default') {
        this.ctx.fail(new Error('环境变量模型是系统保底配置，不能删除'));
        return;
      }
      if (!window.confirm(`删除模型服务「${item.name}」？删除后，已绑定该模型的分析会话会自动改回默认模型。`)) return;
      await this.ctx.run('正在删除模型服务', async () => {
        await api(`/api/providers/${item.id}`, { method: 'DELETE' });
        delete this.providerTests[item.id];
        await this.load();
        await this.ctx.bootstrap();
        this.ctx.toast('已删除模型服务，并清理相关会话引用', '删除完成');
      });
    },
    async saveTool() {
      let headers = {}, env = {}, args = [];
      try {
        headers = JSON.parse(this.toolForm.headersText || '{}');
        env = JSON.parse(this.toolForm.envText || '{}');
        args = JSON.parse(this.toolForm.argsText || '[]');
      } catch {
        return this.ctx.fail(new Error('请求头、环境变量和参数必须是 JSON'));
      }
      await api('/api/mcp/servers', {
        method: 'POST', body: { ...this.toolForm, headers, env, args, workspace_id: this.ctx.state.workspaceId },
      });
      await this.load();
    },
    async testTool(item) {
      await this.ctx.run('正在连接工具服务', async () => {
        const result = await api(`/api/mcp/servers/${item.id}/test`, { method: 'POST' });
        this.ctx.toast(`发现 ${result.result.tools.length} 个工具`, '工具服务可用');
        await this.load();
      });
    },
  },
  template: `
    <section class="workspace-page">
      <header class="surface-header page-heading enterprise-hero"><div><span class="eyebrow">ADMINISTRATION</span><h1>系统管理</h1><p>管理成员权限、模型服务、工具连接与审计记录，确保分析能力在受控环境中运行。</p></div></header>
      <section class="enterprise-kpis admin-kpis">
        <article><small>成员</small><b>{{ members.length || 1 }}</b><span>{{ members.length ? '当前工作空间' : '本地所有者' }}</span></article>
        <article><small>模型服务</small><b>{{ providers.length }}</b><span>{{ providers.filter(item=>item.has_api_key).length }} 个密钥就绪</span></article>
        <article><small>工具连接</small><b>{{ tools.length }}</b><span>受控 MCP 服务</span></article>
        <article><small>审计事件</small><b>{{ audit.length }}</b><span>最近 100 条</span></article>
      </section>
      <div class="settings-layout">
        <nav class="settings-nav">
          <button :class="{active:tab==='members'}" @click="tab='members'"><Icon name="users"/>成员与权限</button>
          <button :class="{active:tab==='models'}" @click="tab='models'"><Icon name="brain"/>模型服务</button>
          <button :class="{active:tab==='tools'}" @click="tab='tools'"><Icon name="bolt"/>工具连接</button>
          <button :class="{active:tab==='audit'}" @click="tab='audit'"><Icon name="table"/>审计日志</button>
        </nav>
        <main class="settings-content">
          <section v-if="tab==='members'">
            <div class="section-heading"><h2>成员与权限</h2><p>管理员维护数据与指标，分析成员使用已授权数据发起分析。</p></div>
            <div class="setting-list">
              <article v-for="item in members" :key="item.user_id"><div><b>{{ item.name||item.email||item.user_id }}</b><small>{{ item.email||'本地运行身份' }}</small></div><select v-model="item.role" :disabled="ctx.state.workspaceRole!=='owner'" @change="setMemberRole(item)"><option value="owner">所有者</option><option value="editor">管理员</option><option value="analyst">分析成员</option><option value="viewer">只读成员</option></select><button v-if="ctx.state.workspaceRole==='owner'" class="icon-button danger" @click="removeMember(item)"><Icon name="close"/></button></article>
              <EmptyState v-if="!members.length" icon="users" title="本地单用户模式" text="启用企业登录后可在此管理成员。"/>
            </div>
            <div v-if="ctx.state.workspaceRole==='owner'" class="settings-card"><h3>添加成员</h3><div class="form-grid"><label><span>企业邮箱</span><input v-model.trim="memberForm.email" type="email" placeholder="name@company.com"></label><label><span>角色</span><select v-model="memberForm.role"><option value="analyst">分析成员</option><option value="viewer">只读成员</option><option value="editor">管理员</option><option value="owner">所有者</option></select></label></div><button class="button button--primary" :disabled="!memberForm.email" @click="addMember">添加或生成邀请</button><div v-if="inviteResult" class="readiness-banner"><div><Icon name="check"/><span><b>邀请链接已生成</b><small>{{ inviteResult.invitation_url }}</small></span></div></div></div>
          </section>
          <section v-if="tab==='models'">
            <div class="section-heading"><h2>模型服务</h2><p>配置 Agent 使用的 OpenAI-Compatible 模型。</p></div>
            <div class="setting-list"><article v-for="item in providers" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.model || '继承环境变量' }} · {{ item.base_url || '环境默认地址' }}</small><small v-if="providerTests[item.id]" class="provider-test" :class="'provider-test--'+providerTests[item.id].status">{{ providerTests[item.id].text }}</small></div><StatusPill :status="item.has_api_key?'ready':'configured'" :label="item.has_api_key?'密钥就绪':'待配置密钥'"/><button class="button button--small" :disabled="providerTests[item.id]?.status==='running'" @click="testProvider(item)">{{ providerTests[item.id]?.status==='running'?'测试中…':'测试' }}</button><button v-if="item.id!=='environment-default'" class="icon-button danger" title="删除模型服务" @click="removeProvider(item)"><Icon name="close"/></button></article></div>
            <div class="settings-card"><h3>添加模型</h3><div class="form-grid"><label><span>名称</span><input v-model="providerForm.name"></label><label><span>模型 ID</span><input v-model="providerForm.model"></label><label class="span-2"><span>Base URL</span><input v-model="providerForm.base_url"></label><label><span>API Key</span><input type="password" v-model="providerForm.api_key"></label><label><span>Temperature</span><input type="number" min="0" max="2" step="0.1" v-model.number="providerForm.temperature"></label></div><button class="button button--primary" @click="saveProvider">保存模型</button></div>
          </section>
          <section v-if="tab==='tools'">
            <div class="section-heading"><h2>工具连接</h2><p>连接 Agent 在分析时可以调用的受控 MCP 工具。</p></div>
            <div class="setting-list"><article v-for="item in tools" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.transport }} · {{ item.url || item.command }} · {{ item.tools?.length||0 }} 个工具</small></div><StatusPill :status="item.status"/><button class="button button--small" @click="testTool(item)">测试</button></article></div>
            <div class="settings-card"><h3>添加工具服务</h3><div class="form-grid"><label><span>名称</span><input v-model="toolForm.name"></label><label><span>传输方式</span><select v-model="toolForm.transport"><option value="streamable-http">Streamable HTTP</option><option value="sse">SSE</option><option value="http">HTTP</option><option value="stdio">stdio</option></select></label><template v-if="toolForm.transport==='stdio'"><label><span>命令</span><input v-model="toolForm.command"></label><label><span>参数 JSON</span><input v-model="toolForm.argsText"></label><label class="span-2"><span>环境变量 JSON</span><input v-model="toolForm.envText"></label></template><template v-else><label class="span-2"><span>服务 URL</span><input v-model="toolForm.url"></label><label class="span-2"><span>请求头 JSON</span><input v-model="toolForm.headersText"></label></template></div><button class="button button--primary" @click="saveTool">保存连接</button></div>
          </section>
          <section v-if="tab==='audit'"><div class="section-heading"><h2>审计日志</h2><p>回溯数据访问、Agent 分析和配置变更。</p></div><DataTable :rows="audit"/></section>
        </main>
      </div>
    </section>`,
};
