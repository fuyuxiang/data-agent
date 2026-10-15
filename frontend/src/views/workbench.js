/**
 * 工作台：产品默认首页。
 *
 * 目标只有一个——用户打开系统后立刻知道"可以直接问企业数据"。
 * 这里不显示系统运行数量、配置完成率、指标审批数或系统健康状态。
 */

import { CAPABILITIES, Composer } from '../components/composer.js';
import { Icon } from '../components/icons.js';
import { Modal } from '../components/ui.js';
import { actions, loadDemo, state, toast } from '../store.js';
import { navigate } from '../router.js';

const SUGGESTED = [
  '本月销售额是多少？',
  '华东销售同比怎么样？',
  '哪个城市下降最多？',
  '为什么华东销售下降？',
  '哪些商品表现异常？',
  '预测下个月销售额',
];

export const WorkbenchView = {
  name: 'WorkbenchView',
  components: { Composer, Icon, Modal },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return { demoOpen: false, seeding: false, submitting: false, submissionStage: '', requestKey: '', requestFingerprint: '' };
  },
  mounted() {
    const agent = state.agents.find(item => item.id === state.routeParams.agent);
    if (agent) this.$refs.composer.agentId = agent.id;
    if (state.routeParams.file) {
      this.$refs.composer.scopeExplicit = true;
      this.$refs.composer.sourceIds = [];
    }
    if (agent || state.routeParams.ask) this.$refs.composer.setText(state.routeParams.ask || '', { focus: true });
  },
  computed: {
    greeting() {
      const hour = new Date().getHours();
      if (hour < 6) return '欢迎使用';
      if (hour < 12) return '早上好';
      if (hour < 14) return '中午好';
      if (hour < 18) return '下午好';
      return '晚上好';
    },
    capabilities() {
      return CAPABILITIES;
    },
    sampleQuestions() {
      return SUGGESTED;
    },
    needsData() {
      return !state.sources.some(item => item.status === 'ready');
    },
    canManageData() {
      return ['owner', 'editor'].includes(state.workspaceRole);
    },
    canAnalyze() {
      return ['owner', 'editor', 'analyst'].includes(state.workspaceRole);
    },
    /** 有正在进行或刚建好的对话就直接续用，不必每次新建。 */
    sessionReady() {
      return Boolean(state.activeSessionId);
    },
    questions() {
      const fromState = state.recommendedQuestions;
      const fromAgents = state.agents.flatMap(agent => agent.suggested_questions || []);
      const merged = [...fromAgents, ...fromState];
      return merged
        .filter((text, index) => merged.indexOf(text) === index)
        .slice(0, 6)
        .map(text => ({ text, icon: fromAgents.includes(text) ? 'robot' : 'sparkle' }));
    },
  },
  methods: {
    suggest(text) {
      this.$refs.composer.setText(text, { focus: true });
    },
    async submit(payload) {
      if (this.submitting || !this.canAnalyze) return;
      this.submitting = true;
      this.submissionStage = '正在保存问题…';
      try {
        if (!this.sessionReady) {
          const created = await actions.post('/api/sessions', {
            name: payload.text.slice(0, 40) || '新对话',
            source_ids: payload.sourceIds,
          });
          state.sessions.unshift(created.item);
          state.activeSessionId = created.item.id;
        }
        const fingerprint = JSON.stringify({
          session: state.activeSessionId, text: payload.text, sources: payload.sourceIds, agent: payload.agentId,
          files: payload.files.map(file => [file.name, file.size, file.lastModified]), library: state.routeParams.file,
        });
        if (!this.requestKey || this.requestFingerprint !== fingerprint) {
          this.requestKey = crypto.randomUUID();
          this.requestFingerprint = fingerprint;
        }
        const created = await actions.post('/api/analyses', {
          session_id: state.activeSessionId,
          objective: payload.text,
          source_ids: payload.sourceIds,
          agent_id: payload.agentId || undefined,
          skill_id: payload.skillHint || undefined,
          attachment_count: payload.files.length + (state.routeParams.file ? 1 : 0),
          idempotency_key: this.requestKey,
        });
        if (payload.files.length) {
          this.submissionStage = '正在上传并读取附件…';
          try {
            await actions.uploadAttachments(created.item.id, payload.files);
          } catch (error) {
            this.requestKey = '';
            await actions.post(`/api/analyses/${created.item.id}/control`, { action: 'cancel' }).catch(() => {});
            throw error;
          }
        }
        if (state.routeParams.file) {
          this.submissionStage = '正在关联资料…';
          try {
            await actions.post(`/api/analyses/${created.item.id}/attachments/library`, {
              record_id: state.routeParams.file,
            });
          } catch (error) {
            this.requestKey = '';
            await actions.post(`/api/analyses/${created.item.id}/control`, { action: 'cancel' }).catch(() => {});
            throw error;
          }
        }
        this.submissionStage = '正在开始分析…';
        try {
          await actions.post(`/api/analyses/${created.item.id}/start`, {
            expected_version: created.item.contract.version,
          });
        } catch (error) {
          // Preparation is saved. Opening the conversation leaves the original
          // confirmation/retry control available even if the network drops here.
          toast(error.message, '问题已保存，请在对话中确认或重试', 'error');
        }
        this.requestKey = '';
        this.$refs.composer.reset();
        navigate('conversation', { id: state.activeSessionId });
      } catch (error) {
        toast(error.message, '无法开始分析', 'error');
      } finally {
        this.submitting = false;
        this.submissionStage = '';
      }
    },
    async seed() {
      if (this.seeding || !this.canManageData) return;
      this.seeding = true;
      try {
        await loadDemo();
        toast('演示数据已载入，可选择示例问题；自主分析需有可用模型。', '完成');
        this.demoOpen = false;
      } catch (error) {
        toast(error.message, '载入失败', 'error');
      } finally {
        this.seeding = false;
      }
    },
  },
  template: `
    <div class="workbench">
      <div class="workbench__inner">
        <div class="workbench__hello">
          <div class="workbench__mark"><img src="/src/assets/logo-shuqing.png" alt="" /></div>
          <h1 class="workbench__title">{{ greeting }}，开始业务数据分析</h1>
          <p class="muted" style="margin-top:6px">用业务语言查询数据、开展分析，并查看结果依据。</p>
        </div>

        <Composer ref="composer" :disabled="seeding || submitting || !canAnalyze" @submit="submit" />
        <p v-if="submitting" class="small muted" role="status">{{ submissionStage }}</p>
        <p v-if="!canAnalyze" class="small muted">当前为只读权限，可查看已有内容；发起分析需要分析权限。</p>

        <div class="workbench__capabilities">
          <div v-for="item in capabilities" :key="item.label" class="capability">
            <Icon :name="item.icon" :size="19" />
            <span>{{ item.label }}</span>
          </div>
        </div>

        <div v-if="needsData" class="card workbench__empty">
          <Icon name="database" :size="22" class="muted" />
          <div class="grow">
            <b>还没有可分析的数据</b>
            <p class="small muted">{{ canManageData ? '接入业务数据，或载入演示数据了解查询与分析流程；也可以直接上传文件提问。' : canAnalyze ? '请联系管理员接入数据并授权；也可以直接上传文件提问。' : '请联系管理员接入数据并授权。你可以浏览已授权的指标和资料。' }}</p>
          </div>
          <div v-if="canManageData" class="row" style="flex-wrap:wrap">
            <button class="btn btn--sm" @click="navigate('admin/data')">接入数据</button>
            <button class="btn btn--primary btn--sm" @click="demoOpen = true">使用演示数据</button>
          </div>
        </div>

        <div v-else-if="questions.length" class="workbench__suggest">
          <div class="suggest-title"><Icon name="sparkle" :size="15" />推荐问题</div>
          <div class="suggest-list">
            <button v-for="item in questions" :key="item.text" class="suggest" @click="suggest(item.text)">
              <Icon :name="item.icon" :size="15" />
              <span class="grow truncate">{{ item.text }}</span>
              <Icon name="arrowRight" :size="15" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <Modal :open="demoOpen" title="载入演示数据" @close="demoOpen = false">
      <p class="muted small" style="margin-bottom:14px">
        会创建一套 24 个月的零售销售事实数据（区域 / 城市 / 品类 / 渠道），
        并同步建立正式指标、业务知识和默认智能体。这是合成样例，
        不代表真实企业数据；同一载入月份的数据可复现，新工作空间跨月首次载入时数据窗口会移动。
      </p>
      <ul class="stack" style="display:flex;flex-direction:column;gap:6px">
        <li v-for="text in sampleQuestions" :key="text" class="small muted">· {{ text }}</li>
      </ul>
      <template #footer>
        <button class="btn" @click="demoOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="seeding" @click="seed">
          {{ seeding ? '正在载入…' : '载入演示数据' }}
        </button>
      </template>
    </Modal>`,
};
