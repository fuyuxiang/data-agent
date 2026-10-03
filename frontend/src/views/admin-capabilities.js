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
    return { state, toast };
  },
  data() {
    return { providers: [], loading: true, editor: null, testing: '', result: {} };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/providers');
        this.providers = response.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    create() {
      this.editor = { name: '', base_url: '', model: '', api_key: '', enabled: true };
    },
    async save() {
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/providers/${this.editor.id}`, this.editor)
          : await actions.post('/api/providers', this.editor);
        this.providers = this.providers
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('模型服务已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      }
    },
    async test(provider) {
      this.testing = provider.id;
      try {
        await actions.post(`/api/providers/${provider.id}/test`);
        this.result[provider.id] = { ok: true, text: '连接成功' };
      } catch (error) {
        this.result[provider.id] = { ok: false, text: error.message };
      } finally {
        this.testing = '';
      }
    },
    async remove(provider) {
      try {
        await actions.remove(`/api/providers/${provider.id}`);
        this.providers = this.providers.filter(item => item.id !== provider.id);
      } catch (error) {
        toast(error.message, '删除失败', 'error');
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
          <button class="btn btn--primary btn--sm" @click="create"><Icon name="plus" :size="14" />添加模型服务</button>
        </div>
      </header>

      <div v-if="loading" class="stack">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:80px"></div>
      </div>

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
            <span v-if="item.context_window" class="badge">上下文 {{ item.context_window }}</span>
            <span v-if="item.tool_calling" class="badge badge--brand">支持工具调用</span>
          </div>
          <div v-if="result[item.id]" class="small" style="margin-top:8px"
               :style="result[item.id].ok ? 'color:var(--success)' : 'color:var(--danger)'">
            {{ result[item.id].text }}
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn btn--sm" :disabled="testing === item.id" @click="test(item)">
              <Icon name="play" :size="14" />{{ testing === item.id ? '测试中…' : '测试连接' }}
            </button>
            <span class="grow"></span>
            <button v-if="item.secret_source !== 'environment'" class="icon-btn icon-btn--danger"
                    aria-label="删除" @click="remove(item)"><Icon name="trash" :size="15" /></button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!editor" :title="editor?.id ? '编辑模型服务' : '添加模型服务'" @close="editor = null">
      <div v-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" placeholder="生产模型" /></label>
        <label class="field"><span>接口地址</span>
          <input v-model.trim="editor.base_url" class="input" placeholder="https://api.openai.com/v1" /></label>
        <label class="field"><span>模型标识<em> *</em></span>
          <input v-model.trim="editor.model" class="input" placeholder="gpt-4.1-mini" /></label>
        <label class="field"><span>API Key</span>
          <input type="password" v-model="editor.api_key" class="input" autocomplete="new-password" /></label>
        <p class="xs faint">凭据使用工作空间主密钥加密保存，不会以明文回显。</p>
      </div>
      <template #footer>
        <button class="btn" @click="editor = null">取消</button>
        <button class="btn btn--primary" @click="save">保存</button>
      </template>
    </Modal>`,
};

/* ------------------------------------------------------------------ MCP */

export const McpView = {
  name: 'McpView',
  components: { EmptyState, Icon, Modal, Status },
  setup() {
    return { state, toast };
  },
  data() {
    return { servers: [], loading: true, editor: null, active: null, tools: [], testing: '' };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/mcp/servers');
        this.servers = response.items || response.servers || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    create() {
      this.editor = { name: '', url: '', transport: 'http', enabled: true };
    },
    async save() {
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/mcp/servers/${this.editor.id}`, this.editor)
          : await actions.post('/api/mcp/servers', this.editor);
        this.servers = this.servers
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('MCP 服务已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      }
    },
    async test(server) {
      this.testing = server.id;
      try {
        const response = await actions.post(`/api/mcp/servers/${server.id}/test`);
        server.status = 'connected';
        server.tool_count = (response.tools || []).length;
        toast('连接成功', '完成');
      } catch (error) {
        server.status = 'error';
        toast(error.message, '连接失败', 'error');
      } finally {
        this.testing = '';
      }
    },
    async open(server) {
      this.active = server;
      this.tools = [];
      try {
        const response = await actions.get(`/api/mcp/servers/${server.id}/tools`);
        this.tools = response.items || response.tools || [];
      } catch (error) {
        toast(error.message, '无法读取工具清单', 'error');
      }
    },
    async remove(server) {
      try {
        await actions.remove(`/api/mcp/servers/${server.id}`);
        this.servers = this.servers.filter(item => item.id !== server.id);
      } catch (error) {
        toast(error.message, '删除失败', 'error');
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
            <button class="btn btn--sm" :disabled="testing === item.id" @click="test(item)">
              <Icon name="play" :size="14" />{{ testing === item.id ? '连接中…' : '测试连接' }}
            </button>
            <button class="btn btn--sm" @click="open(item)"><Icon name="sort" :size="14" />查看工具</button>
            <span class="grow"></span>
            <button class="icon-btn icon-btn--danger" aria-label="删除" @click="remove(item)">
              <Icon name="trash" :size="15" />
            </button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!editor" title="添加 MCP 服务" @close="editor = null">
      <div v-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" placeholder="CRM 只读接口" /></label>
        <label class="field"><span>服务地址<em> *</em></span>
          <input v-model.trim="editor.url" class="input" placeholder="https://mcp.example.com/sse" /></label>
        <label class="field"><span>传输方式</span>
          <select v-model="editor.transport" class="select">
            <option value="http">HTTP / SSE</option>
            <option value="stdio">本地 stdio</option>
          </select></label>
        <p class="xs faint">出站域名必须已在部署白名单内。</p>
      </div>
      <template #footer>
        <button class="btn" @click="editor = null">取消</button>
        <button class="btn btn--primary" @click="save">保存</button>
      </template>
    </Modal>

    <Modal :open="!!active" :title="(active?.name || '') + ' · 工具'" wide @close="active = null">
      <p class="small muted" style="margin-bottom:12px">
        MCP 工具是外部系统提供的能力，不是技能。技能可以调用它们，但两者不是同一层概念。
      </p>
      <div class="stack" style="display:flex;flex-direction:column;gap:8px">
        <EmptyState v-if="!tools.length" icon="plug" title="没有可用工具" text="先测试连接，确认服务可达。" />
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
    riskLevels() {
      return TOOL_RISK;
    },
  },
};

/* ------------------------------------------------------------------ 集成 */

const CHANNELS = [
  { key: 'feishu', name: '飞书', icon: 'cable', note: '通过应用机器人把分析结果推送到群聊' },
  { key: 'dingtalk', name: '钉钉', icon: 'cable', note: '通过群机器人 Webhook 推送' },
  { key: 'wecom', name: '企业微信', icon: 'cable', note: '通过群机器人 Webhook 推送' },
  { key: 'teams', name: 'Teams', icon: 'cable', note: '通过 Incoming Webhook 推送' },
  { key: 'api', name: '开放 API', icon: 'cable', note: '供外部系统以集成令牌调用分析结果' },
  { key: 'email', name: '邮件', icon: 'cable', note: '通过 SMTP 把报告与成果发送给收件人' },
];

export const IntegrationsView = {
  name: 'IntegrationsView',
  components: { EmptyState, Icon, Modal, Status },
  setup() {
    return { state, toast };
  },
  data() {
    return { connectors: [], loading: true, editor: null, sendOpen: false, sendForm: { url: '', payload: '' } };
  },
  computed: {
    channels() {
      return CHANNELS.map(channel => {
        const existing = this.connectors.find(item => item.type === channel.key);
        return {
          ...channel,
          connector: existing,
          state: existing
            ? (existing.status === 'connected' || existing.status === 'ready' ? 'connected' : 'configured')
            : 'disabled',
        };
      });
    },
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await actions.get('/api/connectors');
        this.connectors = response.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    configure(channel) {
      if (channel.connector) {
        this.editor = { ...channel.connector };
        return;
      }
      this.editor = { name: channel.name, type: channel.key, url: '', enabled: true };
    },
    async save() {
      try {
        const response = this.editor.id
          ? await actions.patch(`/api/connectors/${this.editor.id}`, this.editor)
          : await actions.post('/api/connectors', this.editor);
        this.connectors = this.connectors
          .filter(item => item.id !== response.item.id).concat(response.item);
        this.editor = null;
        toast('集成已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      }
    },
    async test(connector) {
      try {
        await actions.post(`/api/connectors/${connector.id}/test`);
        toast('连接成功', '完成');
      } catch (error) {
        toast(error.message, '连接失败', 'error');
      }
    },
    async remove(connector) {
      try {
        await actions.remove(`/api/connectors/${connector.id}`);
        this.connectors = this.connectors.filter(item => item.id !== connector.id);
      } catch (error) {
        toast(error.message, '删除失败', 'error');
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
      </header>

      <div v-if="loading" class="grid grid--2">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:110px"></div>
      </div>

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
            <button v-if="channel.connector" class="btn btn--sm" @click="test(channel.connector)">
              <Icon name="play" :size="14" />测试
            </button>
            <button class="btn btn--sm" @click="configure(channel)">
              {{ channel.connector ? '配置' : '接入' }}
            </button>
            <span class="grow"></span>
            <button v-if="channel.connector" class="icon-btn icon-btn--danger" aria-label="移除"
                    @click="remove(channel.connector)"><Icon name="trash" :size="15" /></button>
          </div>
        </article>
      </div>
    </div>

    <Modal :open="!!editor" :title="(editor?.name || '') + ' 集成'" @close="editor = null">
      <div v-if="editor" class="stack">
        <label class="field"><span>名称<em> *</em></span>
          <input v-model.trim="editor.name" class="input" /></label>
        <label v-if="['feishu', 'dingtalk', 'wecom', 'teams'].includes(editor.type)" class="field">
          <span>Webhook 地址<em> *</em></span>
          <input v-model.trim="editor.url" class="input" placeholder="https://…" /></label>
        <label v-else-if="editor.type === 'api'" class="field">
          <span>说明</span>
          <input v-model.trim="editor.description" class="input" placeholder="在「用户管理」生成集成令牌后调用开放 API" /></label>
        <label v-else class="field">
          <span>SMTP 配置</span>
          <input class="input" disabled placeholder="在部署环境变量中配置 SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD" />
        </label>
        <p class="xs faint">外部地址必须已在部署的出站白名单内。</p>
      </div>
      <template #footer>
        <button class="btn" @click="editor = null">取消</button>
        <button class="btn btn--primary" @click="save">保存</button>
      </template>
    </Modal>`,
};
