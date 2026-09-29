import { api, withWorkspace } from './api.js';
import { DataTable, EmptyState, Icon, StatusPill } from './components.js';

export const SettingsPanel = {
  components: { DataTable, EmptyState, Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    tab: localStorage.getItem('meridian-settings-tab') || 'members',
    members: [], providers: [], tools: [], connectors: [], feedback: [], audit: [], agents: [], agentKnowledge: [], agentSkills: [], inviteResult: null, providerTests: {},
    agentForm: { id: '', name: '', description: '', instruction: '', source_ids: [], knowledge_document_ids: [], provider_id: '', skill_id: '' },
    memberForm: { email: '', role: 'analyst' },
    providerForm: {
      name: 'OpenAI Compatible', base_url: 'https://api.openai.com/v1',
      model: 'gpt-4.1-mini', api_key: '', temperature: 0.2,
    },
    toolForm: {
      name: '工具服务', transport: 'streamable-http', url: '', command: '',
      argsText: '[]', headersText: '{}', envText: '{}',
    },
    mailForm: { name: '企业邮件', host: '', port: 587, username: '', password: '', sender: '', recipient: '', use_tls: true },
  }),
  mounted() {
    if (!['members', 'models', 'agents', 'tools', 'delivery', 'feedback', 'audit'].includes(this.tab)) this.tab = 'members';
    this.load();
  },
  watch: { tab(value) { localStorage.setItem('meridian-settings-tab', value); } },
  methods: {
    async load() {
      const wid = this.ctx.state.workspaceId;
      const [members, providers, tools, connectors, feedback, audit, agents, knowledge, skills] = await Promise.all([
        api(`/api/workspaces/${wid}/members`), api('/api/providers'),
        api(withWorkspace('/api/mcp/servers', wid)), api(withWorkspace('/api/connectors', wid)),
        api(withWorkspace('/api/feedback', wid)), api(withWorkspace('/api/audit?limit=100', wid)),
        api(withWorkspace('/api/agents', wid)), api(withWorkspace('/api/knowledge/documents', wid)),
        api(withWorkspace('/api/skills', wid)),
      ]);
      this.members = members.items || [];
      this.providers = providers.items || [];
      this.tools = tools.items || [];
      this.connectors = (connectors.items || []).filter(item => item.type === 'email');
      this.feedback = feedback.items || [];
      this.audit = audit.items || [];
      this.agents = agents.items || [];
      this.agentKnowledge = (knowledge.items || []).filter(item => item.enabled !== false);
      this.agentSkills = (skills.items || []).filter(item => !item.status || item.status === 'published');
    },
    editAgent(item) {
      this.agentForm = {
        id: item.id, name: item.name || '', description: item.description || '',
        instruction: item.instruction || '', source_ids: [...(item.source_ids || [])],
        knowledge_document_ids: [...(item.knowledge_document_ids || [])],
        provider_id: item.provider_id || '', skill_id: item.skill_id || '',
      };
    },
    resetAgent() {
      this.agentForm = { id: '', name: '', description: '', instruction: '', source_ids: [], knowledge_document_ids: [], provider_id: '', skill_id: '' };
    },
    async saveAgent() {
      if (!this.agentForm.name.trim() || !this.agentForm.source_ids.length) {
        return this.ctx.fail(new Error('请填写智能体名称并选择至少一个数据源'));
      }
      try {
        await this.ctx.run('正在保存智能体草稿', async () => {
          const path = this.agentForm.id ? `/api/agents/${this.agentForm.id}` : '/api/agents';
          await api(path, { method: this.agentForm.id ? 'PATCH' : 'POST', body: this.agentForm });
          this.resetAgent();
          await this.load();
        });
      } catch (error) { this.ctx.fail(error); }
    },
    async publishAgent(item) {
      try {
        await this.ctx.run('正在发布智能体', async () => {
          await api(`/api/agents/${item.id}/publish`, { method: 'POST' });
          await this.load();
        });
      } catch (error) { this.ctx.fail(error); }
    },
    async rollbackAgent(item) {
      const answer = await this.ctx.askForm({
        title: '从历史版本恢复草稿',
        fields: [{ key: 'version', label: '历史版本号', required: true }],
        submitLabel: '恢复为新草稿',
      });
      if (!answer) return;
      try {
        await api(`/api/agents/${item.id}/rollback`, { method: 'POST', body: { version: Number(answer.version) } });
        await this.load();
      } catch (error) { this.ctx.fail(error); }
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
      if (!await this.ctx.confirmAction({title:'移除成员',message:`移除成员 ${item.name || item.email}？`,submitLabel:'移除'})) return;
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
      if (!await this.ctx.confirmAction({title:'删除模型服务',message:`删除「${item.name}」？已绑定该模型的分析会话会自动改回默认模型。`,submitLabel:'删除'})) return;
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
    async approveMcpTool(item, tool, enabled) {
      const selected = new Set(item.formal_read_tools || []);
      enabled ? selected.add(tool.name) : selected.delete(tool.name);
      try {
        const response = await api(`/api/mcp/servers/${item.id}`, {
          method: 'PATCH', body: { formal_read_tools: [...selected] },
        });
        item.formal_read_tools = response.item.formal_read_tools || [];
        this.ctx.toast('正式 Agent 工具授权已更新');
      } catch (error) { this.ctx.fail(error); }
    },
    async toggleSessionMcp(enabled) {
      const session = this.ctx.activeSession();
      if (!session) return;
      try {
        const response = await api(`/api/sessions/${session.id}`, {
          method: 'PATCH', body: { agent_allow_mcp: enabled },
        });
        Object.assign(session, response.item);
        this.ctx.toast(enabled ? '当前会话已启用已批准的 MCP 工具' : '当前会话已停用 MCP 工具');
      } catch (error) { this.ctx.fail(error); }
    },
    async saveMail() {
      if (!this.mailForm.host.trim() || !this.mailForm.sender.trim() || !this.mailForm.recipient.trim()) {
        return this.ctx.fail(new Error('请填写 SMTP 主机、发件人和默认收件人'));
      }
      await this.ctx.run('正在保存邮件服务', async () => {
        await api('/api/connectors', { method:'POST', body:{ ...this.mailForm, type:'email', workspace_id:this.ctx.state.workspaceId } });
        this.mailForm.password = '';
        await this.load();
      });
    },
    async removeMail(item) {
      if (!await this.ctx.confirmAction({title:'移除邮件服务',message:`移除“${item.name}”？`,submitLabel:'移除'})) return;
      await api(`/api/connectors/${item.id}`, { method:'DELETE' });
      await this.load();
    },
    async reviewFeedback(item, status) {
      const entry = status === 'resolved' || status === 'dismissed'
        ? await this.ctx.askForm({
            title: status === 'resolved' ? '记录处理结果' : '记录未采纳原因',
            fields:[{ key:'review_note', label:'处理说明', required:true, multiline:true }],
            submitLabel:'保存处理结果',
          })
        : {};
      if (!entry) return;
      await api(`/api/feedback/${item.id}`, { method:'PATCH', body:{ status, review_note:entry.review_note || '' } });
      await this.load();
      this.ctx.toast('', '反馈状态已更新');
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
          <button :class="{active:tab==='agents'}" @click="tab='agents'"><Icon name="workflow"/>分析智能体</button>
          <button :class="{active:tab==='tools'}" @click="tab='tools'"><Icon name="bolt"/>工具连接</button>
          <button :class="{active:tab==='delivery'}" @click="tab='delivery'"><Icon name="chat"/>成果交付</button>
          <button :class="{active:tab==='feedback'}" @click="tab='feedback'"><Icon name="check"/>质量反馈</button>
          <button :class="{active:tab==='audit'}" @click="tab='audit'"><Icon name="table"/>审计日志</button>
        </nav>
        <main class="settings-content">
          <section v-if="tab==='members'">
            <div class="section-heading"><h2>成员与权限</h2><p>管理员维护数据与指标，分析成员使用已授权数据发起分析。</p></div>
            <div class="setting-list">
              <article v-for="item in members" :key="item.user_id"><div><b>{{ item.name||item.email||item.user_id }}</b><small>{{ item.email||'本地运行身份' }}</small></div><select v-model="item.role" :disabled="ctx.state.workspaceRole!=='owner'" @change="setMemberRole(item)"><option value="owner">所有者</option><option value="editor">管理员</option><option value="analyst">分析成员</option><option value="viewer">只读成员</option></select><button v-if="ctx.state.workspaceRole==='owner'" class="icon-button danger" :aria-label="'移除成员 '+(item.name||item.email||item.user_id)" @click="removeMember(item)"><Icon name="close"/></button></article>
              <EmptyState v-if="!members.length" icon="users" title="本地单用户模式" text="启用企业登录后可在此管理成员。"/>
            </div>
            <div v-if="ctx.state.workspaceRole==='owner'" class="settings-card"><h3>添加成员</h3><div class="form-grid"><label><span>企业邮箱</span><input v-model.trim="memberForm.email" type="email" placeholder="name@company.com"></label><label><span>角色</span><select v-model="memberForm.role"><option value="analyst">分析成员</option><option value="viewer">只读成员</option><option value="editor">管理员</option><option value="owner">所有者</option></select></label></div><button class="button button--primary" :disabled="!memberForm.email" @click="addMember">添加或生成邀请</button><div v-if="inviteResult" class="readiness-banner"><div><Icon name="check"/><span><b>邀请链接已生成</b><small>{{ inviteResult.invitation_url }}</small></span></div></div></div>
          </section>
          <section v-if="tab==='models'">
            <div class="section-heading"><h2>模型服务</h2><p>配置 Agent 使用的 OpenAI-Compatible 模型。</p></div>
            <div class="setting-list"><article v-for="item in providers" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.model || '继承环境变量' }} · {{ item.base_url || '环境默认地址' }}</small><small v-if="providerTests[item.id]" class="provider-test" :class="'provider-test--'+providerTests[item.id].status">{{ providerTests[item.id].text }}</small></div><StatusPill :status="item.has_api_key?'ready':'configured'" :label="item.has_api_key?'密钥就绪':'待配置密钥'"/><button class="button button--small" :disabled="providerTests[item.id]?.status==='running'" @click="testProvider(item)">{{ providerTests[item.id]?.status==='running'?'测试中…':'测试' }}</button><button v-if="item.id!=='environment-default'" class="icon-button danger" title="删除模型服务" @click="removeProvider(item)"><Icon name="close"/></button></article></div>
            <div class="settings-card"><h3>添加模型</h3><div class="form-grid"><label><span>名称</span><input v-model="providerForm.name"></label><label><span>模型 ID</span><input v-model="providerForm.model"></label><label class="span-2"><span>Base URL</span><input v-model="providerForm.base_url"></label><label><span>API Key</span><input type="password" v-model="providerForm.api_key"></label><label><span>Temperature</span><input type="number" min="0" max="2" step="0.1" v-model.number="providerForm.temperature"></label></div><button class="button button--primary" @click="saveProvider">保存模型</button></div>
          </section>
          <section v-if="tab==='agents'">
            <div class="section-heading"><h2>分析智能体</h2><p>把数据范围、知识、技能、模型和分析指令打包为可发布版本。</p></div>
            <div class="setting-list"><article v-for="item in agents" :key="item.id"><div><b>{{ item.name }} · v{{ item.version }}</b><small>{{ item.description || '未填写说明' }}</small><small>{{ item.source_ids?.length||0 }} 个数据源 · {{ item.status==='published'?'已发布':'草稿' }}</small></div><StatusPill :status="item.status"/><button class="button button--small" @click="editAgent(item)">编辑</button><button v-if="ctx.state.workspaceRole==='owner' && item.status!=='published'" class="button button--small" @click="publishAgent(item)">发布</button><button v-if="ctx.state.workspaceRole==='owner' && item.version>1" class="button button--small" @click="rollbackAgent(item)">恢复版本</button></article><EmptyState v-if="!agents.length" icon="workflow" title="尚无分析智能体" text="创建草稿并发布后，业务人员即可在分析页选择。"/></div>
            <div class="settings-card"><h3>{{ agentForm.id ? '编辑智能体草稿' : '创建分析智能体' }}</h3><div class="form-grid"><label><span>名称</span><input v-model.trim="agentForm.name" placeholder="例如 城市经营分析师"></label><label><span>模型服务</span><select v-model="agentForm.provider_id"><option value="">使用会话默认模型</option><option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.name }}</option></select></label><label class="span-2"><span>使用说明</span><input v-model.trim="agentForm.description" placeholder="向业务人员说明适用问题"></label><label class="span-2"><span>分析指令</span><textarea v-model.trim="agentForm.instruction" placeholder="说明分析口径与输出要求；权限和验证规则始终由系统执行"></textarea></label><label><span>分析技能</span><select v-model="agentForm.skill_id"><option value="">使用基础分析能力</option><option v-for="skill in agentSkills" :key="skill.id" :value="skill.id">{{ skill.name }}</option></select></label></div><h4>可用数据源</h4><div class="option-grid"><label v-for="source in ctx.state.sources" :key="source.id" class="check-control"><input v-model="agentForm.source_ids" type="checkbox" :value="source.id">{{ source.name }}</label></div><h4>业务知识</h4><div class="option-grid"><label v-for="document in agentKnowledge" :key="document.id" class="check-control"><input v-model="agentForm.knowledge_document_ids" type="checkbox" :value="document.id">{{ document.name || document.filename }}</label></div><button class="button button--primary" @click="saveAgent">保存草稿</button><button v-if="agentForm.id" class="button" @click="resetAgent">取消编辑</button></div>
          </section>
          <section v-if="tab==='tools'">
            <div class="section-heading"><h2>工具连接</h2><p>连接 Agent 在分析时可以调用的受控 MCP 工具。</p></div>
            <div v-if="ctx.state.workspaceRole==='owner' && ctx.activeSession()" class="settings-card"><h3>当前分析会话</h3><label class="check-control"><input type="checkbox" :checked="!!ctx.activeSession().agent_allow_mcp" @change="toggleSessionMcp($event.target.checked)">允许本会话的正式 Agent 使用下方已批准的 MCP 工具</label><p>工具结果会标记为外部未验证信息，不能直接成为数据结论。</p></div>
            <div class="setting-list"><article v-for="item in tools" :key="item.id"><div><b>{{ item.name }}</b><small>{{ item.transport }} · {{ item.url || item.command }} · {{ item.tools?.length||0 }} 个工具</small><div v-if="ctx.state.workspaceRole==='owner' && item.tools?.length" class="option-grid"><label v-for="tool in item.tools" :key="tool.name" class="check-control"><input type="checkbox" :checked="(item.formal_read_tools||[]).includes(tool.name)" @change="approveMcpTool(item,tool,$event.target.checked)">{{ tool.name }}<small>批准用于正式分析；请确认外部工具实际行为</small></label></div></div><StatusPill :status="item.status"/><button class="button button--small" @click="testTool(item)">测试</button></article></div>
            <div class="settings-card"><h3>添加工具服务</h3><div class="form-grid"><label><span>名称</span><input v-model="toolForm.name"></label><label><span>传输方式</span><select v-model="toolForm.transport"><option value="streamable-http">Streamable HTTP</option><option value="sse">SSE</option><option value="http">HTTP</option><option value="stdio">stdio</option></select></label><template v-if="toolForm.transport==='stdio'"><label><span>命令</span><input v-model="toolForm.command"></label><label><span>参数 JSON</span><input v-model="toolForm.argsText"></label><label class="span-2"><span>环境变量 JSON</span><input v-model="toolForm.envText"></label></template><template v-else><label class="span-2"><span>服务 URL</span><input v-model="toolForm.url"></label><label class="span-2"><span>请求头 JSON</span><input v-model="toolForm.headersText"></label></template></div><button class="button button--primary" @click="saveTool">保存连接</button></div>
          </section>
          <section v-if="tab==='delivery'"><div class="section-heading"><h2>成果交付</h2><p>为通过校验的分析成果配置真实 SMTP 邮件发送；用户也可下载带附件的邮件文件。</p></div><div class="setting-list"><article v-for="item in connectors" :key="item.id"><div><b>{{ item.name }}</b><small>SMTP · {{ item.configured ? '已配置' : '待配置' }}</small></div><StatusPill :status="item.enabled?'ready':'disabled'"/><button class="icon-button danger" :aria-label="'移除 '+item.name" @click="removeMail(item)"><Icon name="close"/></button></article></div><div class="settings-card"><h3>添加邮件服务</h3><div class="form-grid"><label><span>名称</span><input v-model.trim="mailForm.name"></label><label><span>SMTP 主机</span><input v-model.trim="mailForm.host"></label><label><span>端口</span><input v-model.number="mailForm.port" type="number" min="1" max="65535"></label><label><span>发件人</span><input v-model.trim="mailForm.sender" type="email"></label><label><span>登录用户名</span><input v-model.trim="mailForm.username"></label><label><span>登录密码</span><input v-model="mailForm.password" type="password"></label><label><span>默认收件人</span><input v-model.trim="mailForm.recipient" type="email"></label><label><span>传输加密</span><select v-model="mailForm.use_tls"><option :value="true">STARTTLS</option><option :value="false">由本地受控 SMTP 保证</option></select></label></div><button class="button button--primary" @click="saveMail">保存邮件服务</button></div></section>
          <section v-if="tab==='feedback'"><div class="section-heading"><h2>质量反馈</h2><p>查看分析成员提交的纠错意见，处理后再更新指标、知识或分析技能。</p></div><div class="setting-list"><article v-for="item in feedback" :key="item.id"><div><b>{{ {correct:'准确',partially_correct:'部分准确',incorrect:'需要纠正'}[item.rating] || item.rating }} · {{ item.run_id }}</b><small>{{ item.comment || '未补充说明' }} · {{ item.status }}</small><small v-if="item.review_note">处理记录：{{ item.review_note }}</small></div><button v-if="item.status!=='resolved'" class="button button--small" @click="reviewFeedback(item,'resolved')">标记已处理</button><button v-if="item.status==='open'" class="button button--small" @click="reviewFeedback(item,'reviewing')">处理中</button></article><EmptyState v-if="!feedback.length" icon="check" title="暂无质量反馈" text="分析成员提交的反馈会显示在这里。"/></div></section>
          <section v-if="tab==='audit'"><div class="section-heading"><h2>审计日志</h2><p>回溯数据访问、Agent 分析和配置变更。</p></div><DataTable :rows="audit"/></section>
        </main>
      </div>
    </section>`,
};
