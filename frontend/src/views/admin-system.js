/**
 * 系统：用户管理与系统设置。
 *
 * 权限在这里授予，也在这里收回。V2 的六种角色对应到现有实现：
 * owner（所有者）· editor（构建者）· analyst（分析师）· viewer（只读）。
 */

import { Icon } from '../components/icons.js';
import { DataTable, EmptyState, Modal, Status, Tabs } from '../components/ui.js';
import { actions, state, toast } from '../store.js';
import { navigate } from '../router.js';

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
      members: [], loading: true, loadError: '', inviteOpen: false,
      invite: { email: '', role: 'analyst' }, inviteMode: 'new', invitationUrl: '',
      busy: false, tab: 'members',
      memberBusy: {},
    };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    roleLabel(role) {
      return ROLE_LABEL[role] || role;
    },
    openInvite() {
      this.invite = { email: '', role: 'analyst' };
      this.inviteMode = 'new';
      this.invitationUrl = '';
      this.inviteOpen = true;
    },
    closeInvite() {
      if (this.busy) return;
      this.inviteOpen = false;
      this.invitationUrl = '';
    },
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get(`/api/workspaces/${state.workspaceId}/members`);
        this.members = response.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    async inviteMember() {
      if (!this.invite.email || this.busy) return;
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(this.invite.email)) {
        toast('请填写有效邮箱地址', '邮箱格式不正确', 'error');
        return;
      }
      this.busy = true;
      try {
        if (this.inviteMode === 'new') {
          const response = await actions.post(
            `/api/workspaces/${state.workspaceId}/invitations`, this.invite,
          );
          this.invitationUrl = new URL(response.registration_url, window.location.origin).href;
          toast('注册链接已生成，请复制并发给对方', '完成');
        } else {
          await actions.post(`/api/workspaces/${state.workspaceId}/members`, this.invite);
          this.inviteOpen = false;
          this.invitationUrl = '';
          await this.load();
          toast('已有账号已加入工作空间', '完成');
        }
      } catch (error) {
        toast(error.message, '添加成员失败', 'error');
      } finally {
        this.busy = false;
      }
    },
    async copyInvitation() {
      try {
        await navigator.clipboard.writeText(this.invitationUrl);
        toast('注册链接已复制', '完成');
      } catch {
        toast('请选中链接手动复制', '复制失败', 'error');
      }
    },
    async changeRole(member, role, input) {
      if (this.memberBusy[member.user_id]) return;
      this.memberBusy[member.user_id] = true;
      try {
        await actions.patch(`/api/workspaces/${state.workspaceId}/members/${member.user_id}`, { role });
        member.role = role;
        toast('角色已更新', '完成');
      } catch (error) {
        if (input) input.value = member.role;
        toast(error.message, '更新失败', 'error');
      } finally {
        delete this.memberBusy[member.user_id];
      }
    },
    async remove(member) {
      if (this.memberBusy[member.user_id]) return;
      this.memberBusy[member.user_id] = true;
      try {
        await actions.remove(`/api/workspaces/${state.workspaceId}/members/${member.user_id}`);
        this.members = this.members.filter(item => item.user_id !== member.user_id);
        toast('成员已移除', '完成');
      } catch (error) {
        toast(error.message, '移除失败', 'error');
      } finally {
        delete this.memberBusy[member.user_id];
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
          <button v-if="state.workspaceRole === 'owner'" class="btn btn--primary btn--sm" @click="openInvite"><Icon name="plus" :size="14" />添加用户</button>
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
            <p v-if="!loading && !loadError" class="card__hint">{{ members.length }} 位成员</p>
          </div>
        </div>
        <div v-if="loading" class="stack">
          <div v-for="index in 3" :key="index" class="skeleton" style="height:44px"></div>
        </div>
        <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>成员列表加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>
        <EmptyState v-else-if="!members.length" compact icon="users" title="还没有其他成员"
                    text="邀请同事加入，他们只能看到自己有权限的数据、指标和成果。" />
        <div v-else class="stack" style="display:flex;flex-direction:column;gap:8px">
          <div v-for="member in members" :key="member.user_id" class="card row row--between"
               style="padding:12px 14px">
            <div class="grow">
              <b>{{ member.name || member.username || member.email }}</b>
              <p class="small muted">{{ member.email || member.user_id }}</p>
            </div>
            <div class="row" style="gap:8px">
              <select v-if="state.workspaceRole === 'owner'" class="select input--sm" style="width:120px" :value="member.role"
                      :disabled="memberBusy[member.user_id]" :aria-label="'修改' + (member.name || member.email) + '的角色'" @change="changeRole(member, $event.target.value, $event.target)">
                <option v-for="role in roles" :key="role.key" :value="role.key">{{ role.label }}</option>
              </select>
              <span v-else class="badge">{{ roleLabel(member.role) }}</span>
              <button v-if="state.workspaceRole === 'owner'" :disabled="memberBusy[member.user_id]" class="icon-btn icon-btn--danger" aria-label="移除成员" @click="remove(member)">
                <Icon name="trash" :size="15" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <Modal :open="inviteOpen" size="small" :title="invitationUrl ? '注册链接' : '添加用户'" @close="closeInvite">
      <div v-if="invitationUrl" class="stack">
        <p class="small">链接在 24 小时内有效。请复制并发给对方；对方打开后使用受邀邮箱设置账号和密码。</p>
        <input class="input" :value="invitationUrl" readonly aria-label="注册链接"
               @focus="$event.target.select()" />
        <p class="xs faint">系统不会自动发送邀请邮件。关闭窗口后，链接将不再显示。</p>
      </div>
      <div v-else class="stack">
        <label class="field"><span>添加方式</span>
          <select v-model="inviteMode" class="select">
            <option value="new">邀请新用户注册</option>
            <option value="existing">添加已有账号</option>
          </select></label>
        <label class="field"><span>邮箱<em> *</em></span>
          <input v-model.trim="invite.email" type="email" class="input" placeholder="name@company.com" /></label>
        <label class="field"><span>角色</span>
          <select v-model="invite.role" class="select">
            <option v-for="role in roles" :key="role.key" :value="role.key">{{ role.label }}</option>
          </select></label>
        <p class="xs faint">{{ inviteMode === 'new'
          ? '创建后复制注册链接并发给对方，链接 24 小时有效。'
          : '该邮箱须已注册数擎账号；添加后即可访问当前工作空间。' }}</p>
      </div>
      <template #footer>
        <button class="btn" @click="closeInvite">{{ invitationUrl ? '完成' : '取消' }}</button>
        <button v-if="invitationUrl" class="btn btn--primary" @click="copyInvitation">复制链接</button>
        <button v-else class="btn btn--primary" :disabled="busy || !invite.email" @click="inviteMember">
          {{ busy ? '处理中…' : inviteMode === 'new' ? '生成注册链接' : '添加成员' }}
        </button>
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
  { key: '7', label: '7 天' },
  { key: '14', label: '14 天' },
  { key: '30d', label: '30 天' },
  { key: '90d', label: '90 天' },
  { key: '180d', label: '180 天' },
  { key: 'custom', label: '自定义' },
];
const RETENTION_DAYS = { '30d': 30, '90d': 90, '180d': 180 };

export const SystemSettingsView = {
  name: 'SystemSettingsView',
  components: { EmptyState, Icon, Status, Tabs },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return { tab: 'storage', settings: null, retentionChoice: 'forever', audit: [], usage: null, loading: true, loadError: '', saving: false, trash: [] };
  },
  async mounted() {
    await this.load();
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const [settings, audit] = await Promise.all([
          actions.get('/api/lifecycle/settings'),
          actions.get('/api/audit?limit=200'),
        ]);
        this.applySettings(settings.settings || {});
        this.audit = audit.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    applySettings(settings, choice = '') {
      this.settings = { ...settings };
      this.retentionChoice = choice === 'custom' && settings.retention_preset === 'custom'
        ? 'custom'
        : settings.retention_preset === 'custom'
          ? Object.keys(RETENTION_DAYS).find(key => RETENTION_DAYS[key] === settings.retention_custom_days) || 'custom'
          : settings.retention_preset;
    },
    selectRetention(choice) {
      if (this.saving || !this.settings) return;
      this.retentionChoice = choice;
      this.settings.retention_preset = RETENTION_DAYS[choice] ? 'custom' : choice;
      if (RETENTION_DAYS[choice]) this.settings.retention_custom_days = RETENTION_DAYS[choice];
    },
    auditTime(value) {
      if (!value) return '—';
      try {
        return new Intl.DateTimeFormat('zh-CN', {
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit',
          hourCycle: 'h23', timeZoneName: 'shortOffset',
        }).format(new Date(value));
      } catch {
        return String(value);
      }
    },
    async save() {
      if (this.saving || !this.settings) return;
      if (this.settings.retention_preset === 'custom'
          && (!Number.isInteger(this.settings.retention_custom_days) || this.settings.retention_custom_days < 1 || this.settings.retention_custom_days > 3650)) {
        toast('请输入 1 至 3650 之间的整数', '保留天数不正确', 'error');
        return;
      }
      this.saving = true;
      try {
        const response = await actions.put('/api/lifecycle/settings', { ...this.settings });
        this.applySettings(response.settings, this.retentionChoice);
        toast('设置已保存', '完成');
      } catch (error) {
        toast(error.message, '保存失败', 'error');
      } finally {
        this.saving = false;
      }
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">系统设置</h1>
          <p class="page-head__desc">数据保留与审计。误删内容可在回收站恢复，永久清除需要显式确认。</p>
        </div>
        <button class="btn btn--sm" @click="navigate('admin/trash')"><Icon name="trash" :size="14" />打开回收站</button>
      </header>

      <Tabs v-model="tab" :items="[
        { key: 'storage', label: '数据保留' },
        { key: 'audit', label: '审计日志', count: loading || loadError ? null : audit.length },
      ]" />

      <div v-if="loading" class="stack">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:80px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>系统设置加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <div v-else-if="tab === 'storage' && settings" class="card" style="margin-top:18px">
        <h2 class="card__title" style="margin-bottom:4px">成果与会话保留</h2>
        <p class="card__hint" style="margin-bottom:14px">
          按最后活动或保存时间计算，服务每分钟检查。执行中或待继续的分析不会回收；
          到期内容进入回收站，仍可恢复，只有显式确认才会永久清除。
        </p>
        <div class="chip-group">
          <button v-for="option in retentionOptions" :key="option.key" class="chip"
                  :class="{ active: retentionChoice === option.key }" :disabled="saving"
                  @click="selectRetention(option.key)">{{ option.label }}</button>
        </div>
        <label v-if="retentionChoice === 'custom'" class="field" style="margin-top:12px;width:180px">
          <span>保留天数</span>
          <input type="number" v-model.number="settings.retention_custom_days" :disabled="saving" min="1" max="3650" class="input" />
        </label>
        <div class="row" style="margin-top:16px">
          <button class="btn btn--primary btn--sm" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存设置' }}</button>
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
            <time class="timeline__time" :datetime="item.created_at" :title="item.created_at">{{ auditTime(item.created_at) }}</time>
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
