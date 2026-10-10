/**
 * 运营：运行记录与评测。
 *
 * Run / Plan / Action / Trace 这些内部对象在这里被翻译成
 * "谁在什么时候问了什么、用了哪些技能、结果是否可信"。
 */

import { Icon } from '../components/icons.js';
import { DataTable, Drawer, EmptyState, Modal, SearchInput, Status, Tabs } from '../components/ui.js';
import { actions, formatDate, formatTime, state, toast } from '../store.js';

export const RunsView = {
  name: 'RunsView',
  components: { DataTable, Drawer, EmptyState, Icon, SearchInput, Status, Tabs },
  setup() {
    return { actions, formatDate, formatTime, state, toast };
  },
  data() {
    return {
      runs: [], loading: true, loadError: '', query: '', statusFilter: '',
      detail: null, detailTab: 'overview',
      detailOpen: false, detailLoading: false, detailError: '', activeRun: null, detailVersion: 0,
    };
  },
  computed: {
    filtered() {
      const keyword = this.query.trim().toLowerCase();
      return this.runs.filter(item => {
        if (this.statusFilter && item.execution_status !== this.statusFilter) return false;
        if (!keyword) return true;
        return `${item.question || ''} ${item.agent_name || ''} ${(item.skill_ids || []).join(' ')}`
          .toLowerCase().includes(keyword);
      });
    },
    columns() {
      return [
        { key: 'created_at', label: '时间' },
        { key: 'question', label: '问题' },
        { key: 'skill_ids', label: '技能' },
        { key: 'duration_seconds', label: '耗时', align: 'right' },
        { key: 'execution_status', label: '状态' },
      ];
    },
    rows() {
      return this.filtered.map(item => ({
        ...item,
        created_at: formatTime(item.created_at),
        skill_ids: (item.skill_ids || []).join('、') || '—',
        duration_seconds: item.duration_seconds != null ? `${item.duration_seconds}s` : '—',
      }));
    },
  },
  async mounted() {
    await this.load();
  },
  beforeUnmount() {
    this.detailVersion += 1;
  },
  methods: {
    async load() {
      this.loading = true;
      this.loadError = '';
      try {
        const response = await actions.get('/api/admin/runs?limit=300');
        this.runs = response.items || [];
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    async open(run) {
      const version = ++this.detailVersion;
      this.detailTab = 'overview';
      this.detail = null;
      this.activeRun = run;
      this.detailOpen = true;
      this.detailLoading = true;
      this.detailError = '';
      try {
        const response = await actions.get(`/api/admin/runs/${run.id}`);
        if (version === this.detailVersion) this.detail = response.item;
      } catch (error) {
        if (version === this.detailVersion) this.detailError = error.message;
      } finally {
        if (version === this.detailVersion) this.detailLoading = false;
      }
    },
    closeDetail() {
      this.detailVersion += 1;
      this.detailOpen = false;
      this.detail = null;
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">运行记录</h1>
          <p class="page-head__desc">每一次分析的执行轨迹：用了哪些技能、哪些工具、结果是否通过核验。</p>
        </div>
      </header>

      <div class="toolbar">
        <SearchInput v-model="query" placeholder="搜索问题、智能体或技能" style="width:280px" />
        <div class="segmented">
          <button :class="{ active: !statusFilter }" @click="statusFilter = ''">全部</button>
          <button :class="{ active: statusFilter === 'finished' }" @click="statusFilter = 'finished'">已完成</button>
          <button :class="{ active: statusFilter === 'failed' }" @click="statusFilter = 'failed'">失败</button>
          <button :class="{ active: statusFilter === 'cancelled' }" @click="statusFilter = 'cancelled'">已取消</button>
        </div>
        <span class="toolbar__spacer"></span>
        <span v-if="!loading && !loadError" class="small faint">{{ filtered.length }} 条记录</span>
      </div>

      <div v-if="loading" class="stack">
        <div v-for="index in 6" :key="index" class="skeleton" style="height:44px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>运行记录加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <EmptyState v-else-if="!filtered.length" icon="history" :title="runs.length ? '没有匹配的运行记录' : '还没有运行记录'"
                  :text="runs.length ? '调整搜索词或状态筛选后重试。' : '用户在工作台提问后，这里会记录完整的执行轨迹。'" />

      <div v-else class="table-wrap">
        <table class="table">
          <thead><tr>
            <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
            <th></th>
          </tr></thead>
          <tbody>
            <tr v-for="item in filtered" :key="item.id">
              <td class="small" style="white-space:nowrap" :title="item.created_at">{{ formatTime(item.created_at) }}</td>
              <td class="run-row__q" :title="item.question">{{ item.question || '—' }}<span v-if="item.archived_at" class="xs muted" style="display:block">已从会话删除 · 执行记录保留</span></td>
              <td>{{ (item.skill_ids || []).join('、') || '自动' }}</td>
              <td class="is-numeric">{{ item.duration_seconds != null ? item.duration_seconds + 's' : '—' }}</td>
              <td><Status :status="item.execution_status" /></td>
              <td><button class="btn btn--sm" @click="open(item)">详情</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <Drawer :open="detailOpen" :title="'运行详情'" :subtitle="detail?.contract?.payload?.objective || activeRun?.question" width="680"
            @close="closeDetail">
      <div v-if="detailLoading" class="stack"><p class="small muted" role="status">正在读取运行详情…</p><div class="skeleton" style="height:120px"></div></div>
      <div v-else-if="detailError" class="stack"><p class="small" role="alert" style="color:var(--danger)">{{ detailError }}</p><button class="btn btn--sm" @click="open(activeRun)">重试</button></div>
      <div v-if="detail">
        <Tabs v-model="detailTab" :items="[
          { key: 'overview', label: '概览' },
          { key: 'skills', label: '技能调用' },
          { key: 'actions', label: '工具调用' },
          { key: 'decisions', label: '模型调用' },
          { key: 'files', label: '文件' },
        ]" />

        <div v-if="detailTab === 'overview'" class="stack" style="margin-top:16px">
          <div class="metric-strip">
            <div class="metric-strip__item">
              <div class="metric-strip__label">状态</div>
              <div class="metric-strip__value" style="font-size:18px">
                <Status :status="detail.run.execution_status" />
              </div>
            </div>
            <div class="metric-strip__item">
              <div class="metric-strip__label">核验</div>
              <div class="metric-strip__value" style="font-size:18px">{{ detail.run.quality_status || '—' }}</div>
            </div>
            <div class="metric-strip__item">
              <div class="metric-strip__label">耗时</div>
              <div class="metric-strip__value">{{ detail.run.duration_seconds != null ? detail.run.duration_seconds + 's' : '—' }}</div>
            </div>
            <div class="metric-strip__item">
              <div class="metric-strip__label">Token</div>
              <div class="metric-strip__value">{{ detail.run.usage?.model_tokens || 0 }}</div>
            </div>
          </div>
          <dl class="definition">
            <template v-if="detail.run.archived_at"><dt>会话显示</dt><dd>已从会话删除，执行记录保留</dd></template>
            <dt>结束原因</dt><dd class="mono xs">{{ detail.run.stop_reason || '—' }}</dd>
            <dt>数据范围</dt><dd class="mono xs">{{ (detail.run.source_scope || []).join('、') || '—' }}</dd>
            <dt>起始</dt><dd :title="detail.run.started_at">{{ formatTime(detail.run.started_at) || '—' }}</dd>
            <dt>结束</dt><dd :title="detail.run.finished_at">{{ formatTime(detail.run.finished_at) || '—' }}</dd>
          </dl>
          <div v-if="detail.metrics && detail.metrics.length">
            <h3 class="card__title" style="margin:16px 0 8px">核验规则</h3>
            <DataTable :rows="detail.metrics.map(item => ({
              rule: item.rule_id, status: item.status, severity: item.severity || ''
            }))" />
          </div>
        </div>

        <div v-else-if="detailTab === 'skills'" class="stack" style="margin-top:16px">
          <dl class="definition">
            <dt>请求的技能</dt><dd>{{ detail.skills.requested.join('、') || '自动选择' }}</dd>
            <dt>实际使用</dt><dd>{{ detail.skills.used.join('、') || '—' }}</dd>
            <dt>可用工具</dt><dd class="mono xs">{{ detail.skills.allowed_tools.join('、') || '不限' }}</dd>
          </dl>
          <div v-for="item in detail.skills.warnings" :key="item.skill_id" class="insight insight--risk">
            <div><div class="insight__label">提示</div><p class="insight__text">{{ item.message }}</p></div>
          </div>
        </div>

        <div v-else-if="detailTab === 'actions'" class="timeline" style="margin-top:16px">
          <EmptyState v-if="!detail.actions.length" icon="bolt" title="没有工具调用" />
          <div v-for="item in detail.actions" :key="item.tool_id + item.created_at" class="timeline__item">
            <Icon :name="['failed', 'error'].includes(item.status) ? 'close' : 'bolt'" :size="15" />
            <div class="grow">
              <b class="small">{{ item.tool_id }}</b>
              <details v-if="item.arguments?.sql" class="run-sql"><summary class="xs muted">查看 SQL</summary><pre class="mono xs" tabindex="0">{{ item.arguments.sql }}</pre></details>
            </div>
            <span class="timeline__time">{{ item.status }}</span>
          </div>
        </div>

        <div v-else-if="detailTab === 'decisions'" class="timeline" style="margin-top:16px">
          <EmptyState v-if="!detail.decisions.length" icon="cpu" title="没有模型调用记录" />
          <div v-for="item in detail.decisions" :key="item.sequence" class="timeline__item">
            <Icon name="brain" :size="15" />
            <div>
              <b class="small">第 {{ item.sequence }} 轮 · {{ item.model }}</b>
              <div class="xs faint">{{ item.finish_reason }} · {{ item.tool_call_count }} 次工具调用</div>
            </div>
            <span class="timeline__time">{{ item.usage?.total_tokens || 0 }} tok</span>
          </div>
        </div>

        <div v-else class="stack" style="margin-top:16px">
          <EmptyState v-if="!detail.artifacts.length" icon="file" title="没有产出文件"
                      text="在结果页点击「生成报告 / PPT / Excel」后会出现在这里。" />
          <article v-for="item in detail.artifacts" v-else :key="item.id" class="file-card">
            <span class="file-card__icon"><Icon name="fileText" :size="18" /></span>
            <div class="grow">
              <div class="file-card__title">{{ item.title }}</div>
              <div class="file-card__meta">{{ item.kind }} · {{ item.filename }}</div>
            </div>
            <button class="btn btn--sm" @click="actions.download(item.download_url, item.filename)">下载</button>
          </article>
        </div>
      </div>
    </Drawer>`,
};

/* ------------------------------------------------------------------ 评测 */

const REASON_BARS = ['数据不正确', '理解错误', '结论不合理', '图表问题', '不够深入', '速度太慢', '其他'];

export const EvaluationsView = {
  name: 'EvaluationsView',
  components: { EmptyState, Icon, Status },
  setup() {
    return { formatDate, state, toast };
  },
  data() {
    return { data: null, loading: true, loadError: '', tab: 'overview' };
  },
  computed: {
    reasonMax() {
      return Math.max(1, ...(this.data?.reasons || []).map(item => item.count));
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
        this.data = await actions.get('/api/admin/evaluations');
      } catch (error) {
        this.loadError = error.message;
      } finally {
        this.loading = false;
      }
    },
    reasonBar(reason) {
      return `${Math.round((reason.count / this.reasonMax) * 100)}%`;
    },
  },
  template: `
    <div class="view__inner">
      <header class="page-head">
        <div class="grow">
          <h1 class="page-head__title">评测</h1>
          <p class="page-head__desc">用户反馈、错误统计与质量趋势。反馈不只是一个赞踩按钮，它要能说明哪里不对。</p>
        </div>
        <div class="page-head__actions">
          <button class="btn btn--sm" :disabled="loading" @click="load"><Icon name="refresh" :size="14" />{{ loading ? '刷新中…' : '刷新' }}</button>
        </div>
      </header>

      <div v-if="loading" class="stack">
        <div v-for="index in 3" :key="index" class="skeleton" style="height:110px"></div>
      </div>

      <div v-else-if="loadError" class="insight insight--risk" role="alert"><div class="grow"><b>评测数据加载失败</b><p class="small">{{ loadError }}</p></div><button class="btn btn--sm" @click="load">重试</button></div>

      <template v-else-if="data">
        <div class="metric-strip">
          <div class="metric-strip__item">
            <div class="metric-strip__label">总运行</div>
            <div class="metric-strip__value">{{ data.totals.runs }}</div>
          </div>
          <div class="metric-strip__item">
            <div class="metric-strip__label">已完成</div>
            <div class="metric-strip__value">{{ data.totals.finished }}</div>
          </div>
          <div class="metric-strip__item">
            <div class="metric-strip__label">通过核验</div>
            <div class="metric-strip__value">{{ data.totals.published }}</div>
          </div>
          <div class="metric-strip__item">
            <div class="metric-strip__label">失败</div>
            <div class="metric-strip__value">{{ data.totals.failed }}</div>
          </div>
          <div class="metric-strip__item">
            <div class="metric-strip__label">满意度</div>
            <div class="metric-strip__value">
              {{ data.totals.satisfaction === null ? '—' : Math.round(data.totals.satisfaction * 100) + '%' }}
            </div>
          </div>
        </div>

        <div class="grid grid--2" style="margin-top:16px">
          <div class="card">
            <h2 class="card__title" style="margin-bottom:4px">负反馈原因</h2>
            <p class="card__hint" style="margin-bottom:14px">用户认为哪里不对</p>
            <div class="stack" style="display:flex;flex-direction:column;gap:10px">
              <div v-for="reason in data.reasons" :key="reason.key">
                <div class="row row--between small" style="margin-bottom:4px">
                  <span>{{ reason.label }}</span><span class="faint">{{ reason.count }}</span>
                </div>
                <div class="bar-track">
                  <div class="bar-fill bar-fill--danger" :style="{ width: reasonBar(reason) }"></div>
                </div>
              </div>
            </div>
          </div>

          <div class="card">
            <h2 class="card__title" style="margin-bottom:4px">失败原因</h2>
            <p class="card__hint" style="margin-bottom:14px">最常见的运行中止原因</p>
            <EmptyState v-if="!data.failures.length" icon="success" title="没有失败记录" />
            <div v-else class="stack" style="display:flex;flex-direction:column;gap:10px">
              <div v-for="item in data.failures" :key="item.reason">
                <div class="row row--between small" style="margin-bottom:4px">
                  <span class="mono xs">{{ item.reason }}</span><span class="faint">{{ item.count }}</span>
                </div>
                <div class="bar-track">
                  <div class="bar-fill bar-fill--warning"
                       :style="{ width: Math.round((item.count / Math.max(1, data.failures[0].count)) * 100) + '%' }"></div>
                </div>
              </div>
            </div>
          </div>

          <div class="card">
            <h2 class="card__title" style="margin-bottom:4px">模型用量</h2>
            <p class="card__hint" style="margin-bottom:14px">按累计 token 排序</p>
            <EmptyState v-if="!data.usage.length" icon="cpu" title="还没有用量记录" />
            <div v-else class="table-wrap"><table class="table">
              <thead><tr><th>模型</th><th class="is-numeric">请求</th><th class="is-numeric">Token</th></tr></thead>
              <tbody>
                <tr v-for="item in data.usage" :key="item.model">
                  <td>{{ item.model }}</td>
                  <td class="is-numeric">{{ item.requests }}</td>
                  <td class="is-numeric">{{ item.total_tokens.toLocaleString('zh-CN') }}</td>
                </tr>
              </tbody>
            </table></div>
          </div>

          <div class="card">
            <h2 class="card__title" style="margin-bottom:4px">最近反馈</h2>
            <p class="card__hint" style="margin-bottom:14px">每条反馈都要能定位到具体运行</p>
            <EmptyState v-if="!data.feedback.length" icon="chat" title="还没有反馈"
                        text="用户在分析结果页点赞或点踩后会出现在这里。" />
            <div v-else class="stack" style="display:flex;flex-direction:column;gap:8px">
              <article v-for="item in data.feedback" :key="item.id" class="card" style="padding:10px 12px">
                <div class="row row--between">
                  <span class="badge" :class="item.rating === 'correct' ? 'badge--success' : 'badge--danger'">
                    {{ item.rating === 'correct' ? '有帮助' : '需改进' }}
                  </span>
                  <span class="xs faint">{{ formatDate(item.created_at) }}</span>
                </div>
                <p v-if="item.comment" class="small muted" style="margin-top:6px">{{ item.comment }}</p>
              </article>
            </div>
          </div>
        </div>
      </template>
    </div>`,
};
