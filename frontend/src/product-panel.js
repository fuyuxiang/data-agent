import { api, withWorkspace } from './api.js';
import { Icon, StatusPill } from './components.js';

export const ProductPanel = {
  components: { Icon, StatusPill },
  props: { ctx: Object },
  data: () => ({
    loading: false,
    demoLoading: false,
    adminSaving: false,
    adminForm: { tenant_name: '', plan_id: 'enterprise', status: 'trialing', trial: true, period_days: 30 },
  }),
  computed: {
    state() { return this.ctx.state; },
    product() { return this.state.product || {}; },
    entitlements() { return this.state.entitlements || this.product.entitlements || {}; },
    onboarding() { return this.state.onboarding || this.product.onboarding || {}; },
    plans() { return this.product.plans || []; },
    solutions() { return this.product.solutions || []; },
    methodology() { return this.product.methodology || []; },
    isSystemOwner() { return !this.state.user || this.state.user.role === 'owner'; },
  },
  watch: {
    entitlements: {
      handler() { this.syncAdmin(); },
      deep: true,
    },
  },
  mounted() {
    this.syncAdmin();
  },
  methods: {
    percent(value) { return Math.round(Number(value || 0) * 100); },
    limitText(value) { return value === null || value === undefined ? '不限' : this.ctx.number(value); },
    featureLabel(value) {
      return ({
        data_sources: '数据连接',
        governed_agent: '可信分析',
        knowledge_base: '业务口径',
        semantic_layer: '指标治理',
        dashboards: '决策看板',
        result_delivery: '结果交付',
        automation: '报告工厂',
        feishu_bot: '飞书协同',
        mcp_integrations: 'MCP 集成',
        warehouse: '数仓引擎',
        workspace_governance: '空间治理',
        audit: '审计',
        lifecycle_management: '生命周期',
      })[value] || value;
    },
    syncAdmin() {
      const subscription = this.entitlements.subscription || {};
      this.adminForm = {
        tenant_name: this.entitlements.tenant_name || '',
        plan_id: this.entitlements.plan?.id || 'enterprise',
        status: subscription.status || 'trialing',
        trial: !!subscription.trial,
        period_days: this.adminForm.period_days || 30,
      };
    },
    async refresh() {
      this.loading = true;
      try {
        const result = await api(withWorkspace('/api/product', this.state.workspaceId));
        this.state.product = result.product;
        this.state.entitlements = result.product.entitlements;
        this.state.onboarding = result.product.onboarding;
      } catch (error) {
        this.ctx.fail(error);
      } finally {
        this.loading = false;
      }
    },
    async seedDemo() {
      if (this.demoLoading) return;
      this.demoLoading = true;
      try {
        await api(withWorkspace('/api/onboarding/demo', this.state.workspaceId), { method: 'POST' });
        await this.ctx.bootstrap();
        this.ctx.toast('已接入样例数据、业务口径和审批指标', '演示空间已准备');
      } catch (error) {
        this.ctx.fail(error);
      } finally {
        this.demoLoading = false;
      }
    },
    async saveSubscription() {
      this.adminSaving = true;
      try {
        const result = await api(withWorkspace('/api/product/subscription', this.state.workspaceId), {
          method: 'PATCH',
          body: this.adminForm,
        });
        this.state.product = result.product;
        this.state.entitlements = result.product.entitlements;
        this.state.onboarding = result.product.onboarding;
        this.ctx.toast('套餐、订阅状态和租户信息已更新', '商业配置已保存');
      } catch (error) {
        this.ctx.fail(error);
      } finally {
        this.adminSaving = false;
      }
    },
  },
  template: `
    <section class="workspace-page">
      <header class="surface-header">
        <div><span class="eyebrow">SaaS control plane</span><h1>产品总览</h1><p>把数据连接、业务口径、指标治理、可信分析、结果交付和自动化沉淀为一条可售卖的标准产品路径。</p></div>
        <div class="header-cluster">
          <span class="source-chip"><i :class="{on:entitlements.plan}"></i>{{ entitlements.tenant_name || '默认客户' }} · {{ entitlements.plan?.name || '未开通' }}</span>
          <button class="button" :disabled="loading" @click="refresh"><Icon name="refresh"/>{{ loading ? '刷新中' : '刷新' }}</button>
          <button class="button button--primary" :disabled="demoLoading" @click="seedDemo"><Icon name="database"/>{{ demoLoading ? '准备中' : '载入演示数据' }}</button>
        </div>
      </header>

      <div class="product-section">
        <div class="metric-strip">
          <div><small>当前租户</small><b>{{ entitlements.tenant_name || '默认客户' }}</b></div>
          <div><small>当前套餐</small><b>{{ entitlements.plan?.name || '未开通' }}</b></div>
          <div><small>已开通能力</small><b>{{ (entitlements.features || []).length }}</b></div>
          <div><small>产品开通进度</small><b>{{ percent(onboarding.score) }}<em>%</em></b></div>
        </div>
      </div>

      <section v-if="onboarding.steps" class="onboarding-card product-section">
        <header>
          <div><b>客户开通路径</b><small>{{ onboarding.complete ? '核心闭环已完成' : '下一步：' + (onboarding.next_step?.name || '继续配置') }}</small></div>
          <StatusPill :status="onboarding.complete ? 'completed' : 'running'" :label="onboarding.complete ? '可交付' : '配置中'"/>
        </header>
        <ol>
          <li v-for="step in onboarding.steps" :key="step.id" :class="{done:step.done}">
            <i></i><button @click="ctx.go(step.route)">{{ step.name }}</button><small>{{ step.done ? '已完成' : step.description }}</small>
          </li>
        </ol>
      </section>

      <section v-if="isSystemOwner" class="product-section">
        <div class="settings-card">
          <h3>商业配置</h3>
          <p>仅系统所有者可调整。配置保存后会立即影响 bootstrap 能力、前端入口状态和后端 API 权益校验。</p>
          <div class="form-grid">
            <label><span>租户名称</span><input v-model.trim="adminForm.tenant_name"></label>
            <label><span>套餐</span><select v-model="adminForm.plan_id"><option v-for="plan in plans" :key="plan.id" :value="plan.id">{{ plan.name }}</option></select></label>
            <label><span>订阅状态</span><select v-model="adminForm.status"><option value="trialing">试用中</option><option value="active">已激活</option><option value="inactive">未激活</option><option value="canceled">已取消</option></select></label>
            <label><span>从今天起延长天数</span><input type="number" min="1" max="3660" v-model.number="adminForm.period_days"></label>
            <label class="check-control span-2"><input type="checkbox" v-model="adminForm.trial">标记为试用订阅</label>
          </div>
          <button class="button button--primary" :disabled="adminSaving" @click="saveSubscription">{{ adminSaving ? '保存中' : '保存商业配置' }}</button>
        </div>
      </section>

      <section class="product-section">
        <div class="section-heading"><h2>标准方法论</h2><p>销售和交付都按同一套闭环讲：接入 → 定义 → 分析 → 验证 → 交付 → 自动化。</p></div>
        <div class="card-grid">
          <article v-for="item in methodology" :key="item.id" class="entity-card">
            <div class="entity-card__top"><span class="record-icon"><Icon name="check"/></span><span class="count-badge">{{ item.id }}</span></div>
            <h2>{{ item.name }}</h2>
            <p>{{ item.description }}</p>
          </article>
        </div>
      </section>

      <section class="product-section">
        <div class="section-heading"><h2>可售卖解决方案</h2><p>不是分散功能清单，而是按客户问题、产品流程和成功指标组织。</p></div>
        <div class="card-grid">
          <article v-for="item in solutions" :key="item.id" class="entity-card">
            <div class="entity-card__top"><span class="record-icon"><Icon name="dashboard"/></span><span class="count-badge">{{ item.target_customer }}</span></div>
            <h2>{{ item.name }}</h2>
            <p>{{ item.customer_problem }}</p>
            <div class="tag-row"><span v-for="feature in item.required_features" :key="feature">{{ featureLabel(feature) }}</span></div>
            <footer><span>{{ item.success_metrics.join(' / ') }}</span></footer>
          </article>
        </div>
      </section>

      <section class="product-section">
        <div class="section-heading"><h2>套餐与权益边界</h2><p>前端展示、后端 API 和测试共用同一套套餐能力与配额定义。</p></div>
        <div class="card-grid">
          <article v-for="plan in plans" :key="plan.id" class="entity-card" :class="{active:plan.id===entitlements.plan?.id}">
            <div class="entity-card__top"><span class="record-icon"><Icon name="bolt"/></span><StatusPill :status="plan.id===entitlements.plan?.id ? 'active' : 'configured'"/></div>
            <h2>{{ plan.name }}</h2>
            <p>{{ plan.positioning }}</p>
            <div class="tag-row"><span v-for="feature in plan.features" :key="feature">{{ featureLabel(feature) }}</span></div>
            <div class="product-limit-list">
              <div v-for="(value,key) in plan.limits" :key="key"><span>{{ key }}</span><b>{{ limitText(value) }}</b></div>
            </div>
          </article>
        </div>
      </section>
    </section>`,
};
