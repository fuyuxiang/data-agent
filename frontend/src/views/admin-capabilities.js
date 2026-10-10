/**
 * 能力：模型、MCP 与集成。
 *
 * 三者的边界必须说清楚：
 *   模型 = 推理能力；MCP = 外部系统连接；集成 = 把数擎接到别的平台。
 * 没有真正实现的能力一律显示"未配置"，不做假开关。
 */

import { Icon } from '../components/icons.js';
import { EmptyState, Modal, Status, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';
import { navigate } from '../router.js';

const TOOL_RISK = {
  read: { label: '只读', tone: 'badge--success', hint: '可以自动执行' },
  write: { label: '写入', tone: 'badge--warning', hint: '执行前检查权限' },
  external: { label: '外部操作', tone: 'badge--warning', hint: '默认要求确认' },
  dangerous: { label: '危险操作', tone: 'badge--danger', hint: '必须明确确认' },
};

function riskOf(tool) {
  const text = `${tool.description || ''} ${(tool.name || '')}`.toLowerCase();
  if (/delete|drop|remove|删除|清除|truncate/.test(text)) return 'dangerous';
  if (/send|publish|notify|发送|推送|message|email/.test(text)) return 'external';
  if (/update|insert|write|create|修改|写入|新增/.test(text)) return 'write';
  return 'read';
}

/* ------------------------------------------------------------------ 模型 */

export const ModelsView = {
  name: 'ModelsView',
  components: { EmptyState, Icon, Modal, Status },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return {
      providers: [], loading: true, loadError: '', editor: null, detail: null, removeTarget: null,
      removeReferences: null, removeLoading: false, removeError: '',
      saving: false, deleting: false, testing: {}, result: {},
    };
  },
  computed: {
    canManageProviders() {
      return state.workspaceRole === 'owner';
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/providers');
        this.providers = response.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    create() {
      this.editor = { name: '', base_url: '', model: '', api_key: '', enabled: true };
    },
    edit(provider) {
      this.detail = null;
      this.editor = {
        id: provider.id,
        name: provider.name || '',
        base_url: provider.base_url || '',
        model: provider.model || '',
        api_key: '',
        enabled: provider.enabled !== false,
      };
    },
    async save() {
      if (!this.editor?.name || !this.editor?.model || this.saving) {
        if (this.editor && (!this.editor.name || !this.editor.model)) {
          toast('请填写名称和模型标识', '信息未填完整', 'error');
        }
        return;
      }
      this.saving = true;
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/providers/${this.editor.id}`, this.editor)
          : await actions.post('/api/providers', this.editor);
        this.providers = this.providers
          .filter(item => item.id !== response.item.id).concat(response.item);
        delete this.result[response.item.id];
        this.editor = null;
        toast('模型服务已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    async test(provider) {
      if (this.testing[provider.id]) return;
      this.testing[provider.id] = true;
      try {
        await actions.post(`/api/providers/${provider.id}/test`);
        this.result[provider.id] = { ok: true, text: '连接成功' };
      } catch (error) {
        this.result[provider.id] = { ok: false, text: error.message };
      } finally {
        delete this.testing[provider.id];
      }
    },
    async openRemove(provider) {
      this.removeTarget = provider;
      this.removeReferences = null;
      this.removeError = '';
      await this.loadRemoveReferences();
    },
    closeRemove() {
      if (!this.deleting) this.removeTarget = null;
    },
    async loadRemoveReferences() {
      const provider = this.removeTarget;
      if (!provider) return;
      this.removeLoading = true;
      this.removeError = '';
      try {
        const response = await actions.get(`/api/providers/${provider.id}/references`);
        if (this.removeTarget?.id === provider.id) this.removeReferences = response.agents || [];
      } catch (error) {
        if (this.removeTarget?.id === provider.id) this.removeError = error.message;
      } finally {
        if (this.removeTarget?.id === provider.id) this.removeLoading = false;
      }
    },
    async remove(provider) {
      if (!provider || this.deleting || this.removeLoading || !this.removeReferences
          || this.removeReferences.length) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/providers/${provider.id}`);
        this.providers = this.providers.filter(item => item.id !== provider.id);
        this.removeTarget = null;
        this.detail = null;
        toast('模型服务已删除', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
        await this.loadRemoveReferences();
      } finally {
        this.deleting = false;
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">模型</h1>
          <p class="page-head__desc">
            Agent 的推理能力来自这里。凭据加密存储，智能体可以指定主模型，缺省时使用工作空间默认模型。
          </p>
        </div>
        <div class="page-head__actions">
          <button v-if="canManageProviders" class="btn btn--primary btn--sm" @click="create"><Icon name="plus" :size="14" />添加模型服务</button>
        </div>
      </header>

      <p v-if="!canManageProviders" class="small muted" style="margin-bottom:16px">模型服务由工作空间所有者配置。</p>

      <div v-if="loading" class="stack">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:80px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>模型服务加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <EmptyState v-else-if="!providers.length" compact icon="cpu" title="还没有模型服务" text="添加模型服务后，智能体才能进行分析。" />
      <div v-else class="grid grid--2">
        <article v-for="item in providers" :key="item.id" class="card">
          <div class="card__head">
            <div class="grow">
              <h2 class="card__title">{{ item.name }}</h2>
              <p class="card__hint">{{ item.model || '未指定模型' }} · {{ item.base_url || '默认地址' }}</p>
            </div>
            <Status :status="item.enabled === false ? 'disabled' : (result[item.id]?.ok ? 'ready' : 'configured')" />
          </div>
          <div class="tag-row">
            <span class="badge">{{ item.secret_source === 'environment' ? '环境变量' : '工作空间凭据' }}</span>
            <span v-if="item.id === 'environment-default'" class="badge">系统内置，不可删除</span>
            <span v-if="item.context_window" class="badge">上下文 {{ item.context_window }}</span>
            <span v-if="item.tool_calling" class="badge badge--brand">支持工具调用</span>
          </div>
          <div v-if="result[item.id]" class="small" style="margin-top:8px"
               :style="result[item.id].ok ? 'color:var(--success)' : 'color:var(--danger)'">
            {{ result[item.id].text }}
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn btn--sm" @click="detail = item"><Icon name="fileText" :size="14" />查看</button>
            <button v-if="canManageProviders && (item.id !== 'environment-default' || state.user?.role === 'owner')"
                    class="btn btn--sm" @click="edit(item)"><Icon name="edit" :size="14" />编辑</button>
            <button v-if="canManageProviders" class="btn btn--sm" :disabled="testing[item.id]" @click="test(item)">
              <Icon name="play" :size="14" />{{ testing[item.id] ? '测试中…' : '测试连接' }}
            </button>
            <span class="grow"></span>
            <button v-if="canManageProviders && item.id !== 'environment-default'" class="btn btn--sm"
                    style="color:var(--danger)" @click="openRemove(item)"><Icon name="trash" :size="14" />删除</button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!detail" :title="detail?.name || '模型详情'" @close="detail = null">
      <dl v-if="detail" class="definition">
        <dt>名称</dt><dd>{{ detail.name }}</dd>
        <dt>模型标识</dt><dd>{{ detail.model || '未设置' }}</dd>
        <dt>接口地址</dt><dd>{{ detail.base_url || '默认地址' }}</dd>
        <dt>状态</dt><dd>{{ detail.enabled === false ? '已停用' : '已启用' }}</dd>
        <dt>API Key</dt><dd>{{ detail.has_api_key ? '已配置' : '未配置' }}</dd>
      </dl>
      <template #footer>
        <button class="btn" @click="detail = null">关闭</button>
        <button v-if="canManageProviders && detail && (detail.id !== 'environment-default' || state.user?.role === 'owner')"
                class="btn btn--primary" @click="edit(detail)">编辑</button>
      </template>
    </Modal>

    <Modal :open="!!removeTarget" title="删除模型服务" @close="closeRemove">
      <p v-if="removeLoading" class="small muted">正在检查智能体引用…</p>
      <div v-else-if="removeError" class="stack">
        <p class="small" style="color:var(--danger)">{{ removeError }}</p>
        <button class="btn btn--sm" @click="loadRemoveReferences">重试检查</button>
      </div>
      <p v-else-if="removeReferences?.length" class="small">
        「{{ removeTarget?.name }}」仍被智能体 {{ removeReferences.map(item => item.name).join('、') }} 使用。
        请先为这些智能体选择其他模型，再删除模型服务。
      </p>
      <p v-else class="small">确定删除模型服务「{{ removeTarget?.name }}」吗？关联会话的模型选择会清除；后续分析需要有其他可用模型。</p>
      <template #footer>
        <button class="btn" :disabled="deleting" @click="closeRemove">取消</button>
        <button v-if="removeReferences?.length" class="btn btn--primary"
                @click="closeRemove(); navigate('admin/agents')">管理智能体</button>
        <button v-else class="btn btn--danger" :disabled="deleting || removeLoading || !!removeError || !removeReferences"
                @click="remove(removeTarget)">
          {{ deleting ? '删除中…' : '确认删除' }}
        </button>
      </template>
    </Modal>

    <Modal :open="!!editor" :title="editor?.id ? '编辑模型服务' : '添加模型服务'" @close="!saving && (editor = null)">
      <div v-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" placeholder="生产模型" /></label>
        <label class="field"><span>接口地址</span>
          <input v-model.trim="editor.base_url" class="input" placeholder="https://api.openai.com/v1" /></label>
        <label class="field"><span>模型标识<em> *</em></span>
          <input v-model.trim="editor.model" class="input" placeholder="gpt-4.1-mini" /></label>
        <p v-if="editor.base_url?.includes('minimax')" class="xs faint">
          MiniMax 模型标识示例：MiniMax-M3.1-Flash-Preview。请使用连字符，不要写成空格。
        </p>
        <label class="field"><span>API Key</span>
          <input type="password" v-model="editor.api_key" class="input" autocomplete="new-password" /></label>
        <p class="xs faint">{{ editor.id ? '留空则保留已保存的 API Key。' : 'API Key 会加密保存，不会以明文回显。' }}</p>
        <label class="field"><span>状态</span>
          <select v-model="editor.enabled" class="select">
            <option :value="true">启用</option>
            <option :value="false">停用</option>
          </select></label>
      </div>
      <template #footer>
        <button class="btn" :disabled="saving" @click="editor = null">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>`,
};

/* ------------------------------------------------------------------ MCP */

export const McpView = {
  name: 'McpView',
  components: { EmptyState, Icon, Modal, Status },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return { servers: [], loading: true, loadError: '', editor: null, active: null, tools: [], testing: {},
      saving: false, toolsLoading: false, toolsError: '', toolsVersion: 0, removeTarget: null, deleting: false };
  },
  async mounted() {
    await this.load();
  },
  beforeUnmount() {
    this.toolsVersion += 1;
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/mcp/servers');
        this.servers = response.items || response.servers || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    create() {
      this.editor = { name: '', url: '', transport: 'http', command: '', args: [], enabled: true };
    },
    async save() {
      if (this.saving || !this.editor) return;
      if (!this.editor.name || (this.editor.transport === 'stdio' ? !this.editor.command?.trim() : !/^https?:\/\//.test(this.editor.url))) {
        toast(this.editor.transport === 'stdio' ? '请填写名称和执行命令' : '请填写名称和有效的 HTTP / HTTPS 服务地址', '信息未填完整', 'error');
        return;
      }
      this.saving = true;
      try {
        const payload = { ...this.editor, args: (this.editor.args || []).filter(argument => argument.trim() !== '') };
        const response = this.editor.id
          ? await actions.patch(`/api/mcp/servers/${this.editor.id}`, payload)
          : await actions.post('/api/mcp/servers', payload);
        this.servers = this.servers
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('MCP 服务已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    async test(server) {
      if (this.testing[server.id]) return;
      this.testing[server.id] = true;
      try {
        const response = await actions.post(`/api/mcp/servers/${server.id}/test`);
        server.status = 'connected';
        server.tool_count = (response.result?.tools || response.tools || []).length;
        toast('连接成功', '完成');
      } catch (error) {
        server.status = 'error';
        toast(error.message, '连接失败', 'error');
      } finally {
        delete this.testing[server.id];
      }
    },
    async open(server) {
      const version = ++this.toolsVersion;
      this.active = server;
      this.tools = [];
      this.toolsLoading = true;
      this.toolsError = '';
      try {
        const response = await actions.get(`/api/mcp/servers/${server.id}/tools`);
        if (version === this.toolsVersion) this.tools = response.items || response.tools || [];
      } catch (error) {
        if (version === this.toolsVersion) this.toolsError = error.message;
      } finally {
        if (version === this.toolsVersion) this.toolsLoading = false;
      }
    },
    closeTools() {
      this.toolsVersion += 1;
      this.active = null;
    },
    async remove(server) {
      if (!server || this.deleting) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/mcp/servers/${server.id}`);
        this.servers = this.servers.filter(item => item.id !== server.id);
        this.removeTarget = null;
        toast('MCP 服务已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      } finally {
        this.deleting = false;
      }
    },
    riskLabel(tool) {
      return TOOL_RISK[riskOf(tool)];
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">MCP</h1>
          <p class="page-head__desc">
            MCP 是外部系统与工具的连接能力，比如查询 CRM、读取飞书文档、调用业务 API。
            它和「技能」是两件事：技能是完成一项任务的方法，MCP 是这条方法能触达的外部世界。
          </p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--sm" @click="navigate('admin/trash', { collection: 'mcp_servers' })"><Icon name="trash" :size="14" />回收站</button>
          <button class="btn btn--primary btn--sm" @click="create"><Icon name="plus" :size="14" />添加 MCP 服务</button>
        </div>
      </header>

      <div class="card" style="margin-bottom:16px">
        <h2 class="card__title" style="margin-bottom:8px">风险等级</h2>
        <p class="small muted" style="margin-bottom:10px">
          Agent 可以自动执行只读工具；写入类要检查权限；外部操作默认要求确认；危险操作必须明确确认。
        </p>
        <div class="tag-row">
          <span v-for="(info, key) in riskLevels" :key="key" class="badge" :class="info.tone">
            {{ info.label }} · {{ info.hint }}
          </span>
        </div>
      </div>

      <div v-if="loading" class="stack">
        <div v-for="index in 2" :key="index" class="skeleton" style="height:80px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>MCP 服务加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <EmptyState v-else-if="!servers.length" icon="plug" title="还没有连接 MCP 服务"
                  text="连接后，智能体可以通过 MCP 读取外部系统的数据——但仍然受权限与确认策略约束。" />

      <div v-else class="grid grid--2">
        <article v-for="item in servers" :key="item.id" class="card">
          <div class="card__head">
            <div class="grow">
              <h2 class="card__title">{{ item.name }}</h2>
              <p class="card__hint mono xs">{{ item.url }}</p>
            </div>
            <Status :status="item.status || 'configured'" />
          </div>
          <div class="tag-row">
            <span class="badge">{{ item.transport || 'http' }}</span>
            <span v-if="item.tool_count !== undefined" class="badge">{{ item.tool_count }} 个工具</span>
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn btn--sm" :disabled="testing[item.id]" @click="test(item)">
              <Icon name="play" :size="14" />{{ testing[item.id] ? '连接中…' : '测试连接' }}
            </button>
            <button class="btn btn--sm" @click="open(item)"><Icon name="sort" :size="14" />查看工具</button>
            <span class="grow"></span>
            <button class="icon-btn icon-btn--danger" aria-label="删除" @click="removeTarget = item">
              <Icon name="trash" :size="15" />
            </button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!removeTarget" size="small" title="删除 MCP 服务" @close="!deleting && (removeTarget = null)">
      <p class="small">确定删除「{{ removeTarget?.name }}」吗？智能体将无法再调用此服务，可在回收站恢复。</p>
      <template #footer><button class="btn" :disabled="deleting" @click="removeTarget = null">取消</button><button class="btn btn--danger" :disabled="deleting" @click="remove(removeTarget)">{{ deleting ? '删除中…' : '确认删除' }}</button></template>
    </Modal>

    <Modal :open="!!editor" title="添加 MCP 服务" @close="!saving && (editor = null)">
      <div v-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" placeholder="CRM 只读接口" /></label>
        <label v-if="editor.transport !== 'stdio'" class="field"><span>服务地址<em> *</em></span>
          <input v-model.trim="editor.url" class="input" placeholder="https://mcp.example.com/sse" /></label>
        <label class="field"><span>传输方式</span>
          <select v-model="editor.transport" class="select">
            <option value="http">HTTP / SSE</option>
            <option value="stdio">本地 stdio</option>
          </select></label>
        <p class="xs faint">出站域名必须已在部署白名单内。</p>
        <template v-if="editor.transport === 'stdio'">
          <label class="field"><span>执行命令<em> *</em></span><input v-model.trim="editor.command" class="input" placeholder="npx / node / python3" /></label>
          <label class="field"><span>命令参数（每行一个）</span><textarea v-model="argumentText" class="textarea" rows="3"></textarea></label>
          <p class="xs faint">仅系统所有者可创建，且部署环境须已启用 stdio MCP。使用 PATH 中受支持的命令，脚本路径或包名填在参数中；空行会忽略。</p>
        </template>
      </div>
      <template #footer>
        <button class="btn" :disabled="saving" @click="editor = null">取消</button>
        <button class="btn btn--primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>

    <Modal :open="!!active" :title="(active?.name || '') + ' · 工具'" size="large" @close="closeTools">
      <p class="small muted" style="margin-bottom:12px">
        MCP 工具是外部系统提供的能力，不是技能。技能可以调用它们，但两者不是同一层概念。
      </p>
      <div class="stack" style="display:flex;flex-direction:column;gap:8px">
        <p v-if="toolsLoading" class="small muted" role="status">正在读取工具清单…</p>
        <div v-else-if="toolsError" class="stack"><p role="alert" class="small" style="color:var(--danger)">{{ toolsError }}</p><button class="btn btn--sm" @click="open(active)">重试</button></div>
        <EmptyState v-else-if="!tools.length" compact icon="plug" title="没有可用工具" text="先测试连接，确认服务可达。" />
        <article v-for="tool in tools" :key="tool.name" class="card" style="padding:12px 14px">
          <div class="row row--between">
            <b class="small">{{ tool.name }}</b>
            <span class="badge" :class="riskLabel(tool).tone">{{ riskLabel(tool).label }}</span>
          </div>
          <p class="small muted" style="margin-top:4px">{{ tool.description || '无描述' }}</p>
        </article>
      </div>
    </Modal>`,
  computed: {
    argumentText: {
      get() { return (this.editor?.args || []).join('\n'); },
      set(value) { if (this.editor) this.editor.args = String(value || '').split('\n'); },
    },
    riskLevels() {
      return TOOL_RISK;
    },
  },
};

/* ------------------------------------------------------------------ 集成 */

const CHANNELS = [
  { key: 'feishu', type: 'lark', name: '飞书', icon: 'cable', note: '通过群机器人 Webhook 推送分析结果' },
  { key: 'dingtalk', type: 'dingtalk', name: '钉钉', icon: 'cable', note: '通过群机器人 Webhook 推送' },
  { key: 'wecom', type: 'webhook', name: '企业微信', icon: 'cable', note: '通过适配网关接收通用 JSON，再转发到群机器人' },
  { key: 'teams', type: 'webhook', name: 'Teams', icon: 'cable', note: '通过适配网关接收通用 JSON，再转发到 Teams' },
  { key: 'api', name: '开放 API', icon: 'cable', note: '供外部系统以集成令牌调用分析结果' },
  { key: 'email', type: 'email', name: '邮件', icon: 'cable', note: '通过 SMTP 把报告与成果发送给收件人' },
];

export const IntegrationsView = {
  name: 'IntegrationsView',
  components: { EmptyState, Icon, Modal, Status },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return { connectors: [], loading: true, loadError: '', editor: null, saving: false, testing: {},
      removeTarget: null, deleting: false, apiOpen: false };
  },
  computed: {
    channels() {
      const assigned = new Set();
      const channels = CHANNELS.map(channel => {
        const existing = channel.type && this.connectors.find(item => item.type === channel.type && !assigned.has(item.id)
          && (item.channel ? item.channel === channel.key : channel.type !== 'webhook'));
        if (existing) assigned.add(existing.id);
        return {
          ...channel,
          connector: existing,
          state: existing
            ? (existing.enabled === false ? 'disabled' : existing.status || 'configured')
            : 'disabled',
        };
      });
      return channels.concat(this.connectors.filter(item => !assigned.has(item.id)).map(item => ({
        key: item.id, type: item.type, name: item.name, icon: 'cable',
        note: item.type === 'webhook' && !item.channel ? '通用 Webhook 连接器' : '已保存的连接器', connector: item,
        state: item.enabled === false ? 'disabled' : item.status || 'configured',
      })));
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/connectors');
        this.connectors = response.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    configure(channel) {
      if (channel.key === 'api') { this.apiOpen = true; return; }
      if (channel.connector) {
        this.editor = { ...channel.connector };
        return;
      }
      this.editor = { name: channel.name, type: channel.type, channel: channel.key, url: '', enabled: true,
        host: '', port: 587, username: '', password: '', sender: '', recipient: '', use_tls: true };
    },
    async save() {
      if (!this.editor || this.editor.id || this.saving) return;
      const email = this.editor.type === 'email';
      if (!this.editor.name || (email ? !this.editor.host || !this.editor.recipient : !/^https?:\/\//.test(this.editor.url))) {
        toast(email ? '请填写名称、SMTP 主机和收件人' : '请填写名称和有效的 HTTP / HTTPS Webhook 地址', '信息未填完整', 'error');
        return;
      }
      this.saving = true;
      try {
        const response = await actions.post('/api/connectors', this.editor);
        this.connectors = this.connectors
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('集成已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
    async test(connector) {
      if (this.testing[connector.id]) return;
      this.testing[connector.id] = true;
      try {
        await actions.post(`/api/connectors/${connector.id}/test`);
        connector.status = 'connected';
        toast('连接成功', '完成');
      } catch (error) {
        connector.status = 'error';
        toast(error.message, '连接失败', 'error');
      } finally {
        delete this.testing[connector.id];
      }
    },
    async remove(connector) {
      if (!connector || this.deleting) return;
      this.deleting = true;
      try {
        await actions.remove(`/api/connectors/${connector.id}`);
        this.connectors = this.connectors.filter(item => item.id !== connector.id);
        this.removeTarget = null;
        this.editor = null;
        toast('集成已移入回收站', '完成');
      } catch (error) {
        toast(error.message, '删除失败', 'error');
      } finally {
        this.deleting = false;
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">集成</h1>
          <p class="page-head__desc">把数擎的分析结果送到用户已经在用的地方。没有配置的一律显示"未配置"，不做假开关。</p>
        </div>
        <div class="page-head__actions"><button class="btn btn--sm" @click="navigate('admin/trash', { collection: 'connectors' })"><Icon name="trash" :size="14" />回收站</button></div>
      </header>

      <div v-if="loading" class="grid grid--2">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:110px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>集成加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <div v-else class="grid grid--2">
        <article v-for="channel in channels" :key="channel.key" class="card file-tile">
          <div class="row row--between">
            <div class="row">
              <span class="agent-card__mark" style="width:36px;height:36px">
                <Icon :name="channel.icon" :size="18" />
              </span>
              <div>
                <b>{{ channel.name }}</b>
                <p class="small muted">{{ channel.note }}</p>
              </div>
            </div>
            <Status :status="channel.state" />
          </div>
          <div class="row" style="margin-top:auto">
            <button v-if="channel.connector" class="btn btn--sm" :disabled="testing[channel.connector.id]" @click="test(channel.connector)">
              <Icon name="play" :size="14" />{{ testing[channel.connector.id] ? '测试中…' : '测试' }}
            </button>
            <button class="btn btn--sm" @click="configure(channel)">
              {{ channel.key === 'api' ? '查看接入说明' : channel.connector ? '查看配置' : '接入' }}
            </button>
            <span class="grow"></span>
            <button v-if="channel.connector" class="icon-btn icon-btn--danger" aria-label="移除"
                    @click="removeTarget = channel.connector"><Icon name="trash" :size="15" /></button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="apiOpen" title="开放 API 接入" size="small" @close="apiOpen = false">
      <p class="small">开放 API 使用工作空间集成令牌鉴权，不需要创建通知连接器。令牌由工作空间所有者通过管理 API 生成；当前用户管理界面不提供生成入口。</p>
      <p class="xs muted" style="margin-top:12px">请联系工作空间所有者获取接口地址与令牌。集成令牌应妥善保管。</p>
      <template #footer><button class="btn" @click="apiOpen = false">关闭</button></template>
    </Modal>

    <Modal :open="!!removeTarget" title="移除集成" size="small" @close="!deleting && (removeTarget = null)">
      <p class="small">确定移除「{{ removeTarget?.name }}」吗？此连接将无法再用于结果推送，可在回收站恢复。</p>
      <template #footer><button class="btn" :disabled="deleting" @click="removeTarget = null">取消</button><button class="btn btn--danger" :disabled="deleting" @click="remove(removeTarget)">{{ deleting ? '移除中…' : '确认移除' }}</button></template>
    </Modal>

    <Modal :open="!!editor" :title="(editor?.name || '') + ' 集成'" @close="!saving && (editor = null)">
      <div v-if="editor?.id" class="stack">
        <dl class="definition"><dt>名称</dt><dd>{{ editor.name }}</dd><dt>连接类型</dt><dd>{{ editor.type }}</dd><dt>状态</dt><dd><Status :status="editor.status || 'configured'" /></dd><dt>凭据</dt><dd>{{ editor.configured ? '已加密保存' : '未配置' }}</dd></dl>
        <p class="small muted">服务端不支持更新已保存的连接凭据。如需更换地址或凭据，请先移除该连接，再重新接入。</p>
      </div>
      <div v-else-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" /></label>
        <label v-if="editor.type !== 'email'" class="field">
          <span>Webhook 地址<em> *</em></span>
          <input v-model.trim="editor.url" class="input" placeholder="https://…" /></label>
        <p v-if="editor.type === 'webhook'" class="small muted">请填写能接收 message 与 metadata 字段的通用 JSON 适配网关地址。企业微信与 Teams 原生机器人地址需要协议转换，不能直接使用。</p>
        <template v-if="editor.type === 'email'">
          <div class="grid grid--2"><label class="field"><span>SMTP 主机<em> *</em></span><input v-model.trim="editor.host" class="input" placeholder="smtp.company.com" /></label><label class="field"><span>端口</span><input v-model.number="editor.port" class="input" type="number" min="1" max="65535" /></label></div>
          <label class="field"><span>用户名</span><input v-model.trim="editor.username" class="input" autocomplete="off" /></label>
          <label class="field"><span>密码</span><input v-model="editor.password" type="password" class="input" autocomplete="new-password" /></label>
          <label class="field"><span>发件人</span><input v-model.trim="editor.sender" class="input" type="email" /></label>
          <label class="field"><span>收件人<em> *</em></span><input v-model.trim="editor.recipient" class="input" type="email" placeholder="name@company.com" /></label>
          <label class="row small"><input v-model="editor.use_tls" type="checkbox" />使用 TLS 加密连接</label>
        </template>
        <p class="xs faint">外部地址必须已在部署的出站白名单内。</p>
      </div>
      <template #footer>
        <button class="btn" :disabled="saving" @click="editor = null">{{ editor?.id ? '关闭' : '取消' }}</button>
        <button v-if="editor?.id" class="btn" :disabled="testing[editor.id]" @click="test(editor)">{{ testing[editor.id] ? '测试中…' : '测试连接' }}</button>
        <button v-if="editor?.id" class="btn btn--danger" @click="removeTarget = editor; editor = null">移除并重新接入</button>
        <button v-else class="btn btn--primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>`,
};
