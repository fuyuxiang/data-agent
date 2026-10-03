/**
 * 系统：用户管理与系统设置。
 *
 * 权限在这里授予，也在这里收回。V2 的六种角色对应到现有实现：
 * owner（所有者）· editor（构建者）· analyst（分析师）· viewer（只读）。
 */

import { Icon } from '../components/icons.js';
import { DataTable, EmptyState, Modal, Status, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';

const ROLES = [
  { key: 'owner', label: '所有者', hint: '全部权限，含成员与系统设置' },
  { key: 'editor', label: '构建者', hint: '可配置数据、指标、知识、模型与智能体' },
  { key: 'analyst', label: '分析师', hint: '可提问分析并查看数据，不可改配置' },
  { key: 'viewer', label: '只读', hint: '只能查看与下载成果' },
];

const ROLE_LABEL = Object.fromEntries(ROLES.map(role => [role.key, role.label]));

export const UsersView = {
  name: 'UsersView',
  components: { DataTable, EmptyState, Icon, Modal, Status, Tabs },
  setup() {
    return { state, toast };
  },
  data() {
    return {
      members: [], loading: true, inviteOpen: false,
      invite: { email: '', role: 'analyst' }, busy: false, tab: 'members',
    };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await actions.get(`/api/workspaces/${state.workspaceId}/members`);
        this.members = response.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    async inviteMember() {
      this.busy = true;
      try {
        await actions.post(`/api/workspaces/${state.workspaceId}/members`, this.invite);
        this.inviteOpen = false;
        this.invite = { email: '', role: 'analyst' };
        await this.load();
        toast('邀请已创建', '完成');
      } catch (error) {
        toast(error.message, '邀请失败', 'error');
      } finally {
        this.busy = false;
      }
    },
    async changeRole(member, role) {
      try {
        await actions.patch(`/api/workspaces/${state.workspaceId}/members/${member.user_id}`, { role });
        member.role = role;
        toast('角色已更新', '完成');
      } catch (error) {
        toast(error.message, '更新失败', 'error');
      }
    },
    async remove(member) {
      try {
        await actions.remove(`/api/workspaces/${state.workspaceId}/members/${member.user_id}`);
        this.members = this.members.filter(item => item.user_id !== member.user_id);
        toast('成员已移除', '完成');
      } catch (error) {
        toast(error.message, '移除失败', 'error');
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">用户管理</h1>
          <p class="page-head__desc">
            权限先于执行。所有自动能力——技能、指标、MCP、导出——都会在执行前按角色过滤。
          </p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--primary btn--sm" @click="inviteOpen = true"><Icon name="plus" :size="14" />邀请成员</button>
        </div>
      </header>

      <div class="card" style="margin-bottom:16px">
        <h2 class="card__title" style="margin-bottom:8px">角色</h2>
        <div class="grid grid--2" style="gap:10px">
          <div v-for="role in roles" :key="role.key" class="row" style="gap:8px">
            <span class="badge badge--brand">{{ role.label }}</span>
            <span class="small muted">{{ role.hint }}</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card__head">
          <div class="grow">
            <h2 class="card__title">成员</h2>
            <p class="card__hint">{{ members.length }} 位成员</p>
          </div>
        </div>
        <div v-if="loading" class="stack">
          <div v-for="index in 3" :key="index" class="skeleton" style="height:44px"></div>
        </div>
        <EmptyState v-else-if="!members.length" icon="users" title="还没有其他成员"
                    text="邀请同事加入，他们只能看到自己有权限的数据、指标和成果。" />
        <div v-else class="stack" style="display:flex;flex-direction:column;gap:8px">
          <div v-for="member in members" :key="member.user_id" class="card row row--between"
               style="padding:12px 14px">
            <div>
              <b>{{ member.name || member.username || member.email }}</b>
              <p class="small muted">{{ member.email || member.user_id }}</p>
            </div>
            <div class="row" style="gap:8px">
              <select class="select input--sm" style="width:120px" :value="member.role"
                      @change="changeRole(member, $event.target.value)">
                <option v-for="role in roles" :key="role.key" :value="role.key">{{ role.label }}</option>
              </select>
              <button class="icon-btn icon-btn--danger" aria-label="移除成员" @click="remove(member)">
                <Icon name="trash" :size="15" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <Modal :open="inviteOpen" title="邀请成员" @close="inviteOpen = false">
      <div class="stack">
        <label class="field"><span>邮箱<em> *</em></span>
          <input v-model.trim="invite.email" type="email" class="input" placeholder="name@company.com" /></label>
        <label class="field"><span>角色</span>
          <select v-model="invite.role" class="select">
            <option v-for="role in roles" :key="role.key" :value="role.key">{{ role.label }}</option>
          </select></label>
        <p class="xs faint">被邀请人会收到一次性邀请链接，只能访问自己有权的数据。</p>
      </div>
      <template #footer>
        <button class="btn" @click="inviteOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="busy || !invite.email" @click="inviteMember">创建邀请</button>
      </template>
    </Modal>`,
  computed: {
    roles() {
      return ROLES;
    },
  },
};

/* ------------------------------------------------------------------ 系统设置 */

const RETENTION = [
  { key: 'forever', label: '永久保留' },
  { key: '30d', label: '30 天' },
  { key: '90d', label: '90 天' },
  { key: '180d', label: '180 天' },
];

export const SystemSettingsView = {
  name: 'SystemSettingsView',
  components: { EmptyState, Icon, Status, Tabs },
  setup() {
    return { state, toast };
  },
  data() {
    return { tab: 'storage', settings: null, audit: [], usage: null, loading: true, trash: [] };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      try {
        const [settings, audit] = await Promise.all([
          actions.get('/api/lifecycle/settings'),
          actions.get('/api/audit?limit=200'),
        ]);
        this.settings = settings.settings || {};
        this.audit = audit.items || [];
      } catch (error) {
        toast(error.message, '加载失败', 'error');
      } finally {
        this.loading = false;
      }
    },
    async save() {
      try {
        await actions.patch('/api/lifecycle/settings', this.settings);
        toast('设置已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">系统设置</h1>
          <p class="page-head__desc">数据保留、审计与回收站。删除都是可恢复的，永久清除需要显式确认。</p>
        </div>
      </header>

      <Tabs v-model="tab" :items="[
        { key: 'storage', label: '数据保留' },
        { key: 'audit', label: '审计日志', count: audit.length },
      ]" />

      <div v-if="loading" class="stack">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:80px"></div>
      </div>

      <div v-else-if="tab === 'storage' && settings" class="card" style="margin-top:18px">
        <h2 class="card__title" style="margin-bottom:4px">成果与会话保留</h2>
        <p class="card__hint" style="margin-bottom:14px">
          到期后进入回收站，仍可恢复；只有显式确认才会永久清除。
        </p>
        <div class="chip-group">
          <button v-for="option in retentionOptions" :key="option.key" class="chip"
                  :class="{ active: settings.retention_preset === option.key }"
                  @click="settings.retention_preset = option.key">{{ option.label }}</button>
        </div>
        <label v-if="settings.retention_preset === 'custom'" class="field" style="margin-top:12px;width:180px">
          <span>保留天数</span>
          <input type="number" v-model.number="settings.retention_custom_days" min="1" max="3650" class="input" />
        </label>
        <div class="row" style="margin-top:16px">
          <button class="btn btn--primary btn--sm" @click="save">保存设置</button>
        </div>
      </div>

      <div v-else-if="tab === 'audit'" class="card" style="margin-top:18px">
        <h2 class="card__title" style="margin-bottom:4px">审计日志</h2>
        <p class="card__hint" style="margin-bottom:14px">数据访问、分析执行与配置变更的完整留痕。</p>
        <EmptyState v-if="!audit.length" icon="shield" title="暂无审计记录" />
        <div v-else class="timeline">
          <div v-for="item in audit" :key="item.id" class="timeline__item">
            <Icon name="shield" :size="15" />
            <div>
              <b class="small">{{ item.event_type }}</b>
              <div class="xs faint">{{ item.object_type }} · {{ item.actor || 'system' }}</div>
            </div>
            <span class="timeline__time">{{ item.created_at }}</span>
          </div>
        </div>
      </div>
    </div>`,
  computed: {
    retentionOptions() {
      return RETENTION;
    },
  },
};
