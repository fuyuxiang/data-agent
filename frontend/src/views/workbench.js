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
    return { demoOpen: false, seeding: false, submitting: false };
  },
  mounted() {
    const agent = state.agents.find(item => item.id === state.routeParams.agent);
    if (agent) this.$refs.composer.agentId = agent.id;
    if (state.routeParams.ask) this.$refs.composer.text = state.routeParams.ask;
    if (state.routeParams.file) {
      this.$refs.composer.scopeExplicit = true;
      this.$refs.composer.sourceIds = [];
    }
    if (agent || state.routeParams.ask) this.$nextTick(() => this.$refs.composer.focus());
  },
  computed: {
    greeting() {
      const hour = new Date().getHours();
      if (hour < 6) return '夜深了';
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
      return !state.sources.length;
    },
    canAnalyze() {
      return ['owner', 'editor', 'analyst'].includes(state.workspaceRole);
    },
    /** 有正在进行或刚建好的对话就直接续用，不必每次新建。 */
    sessionReady() {
      return Boolean(state.activeSessionId);
    },
    questions() {
      const fromState = state.recommendedQuestions.length ? state.recommendedQuestions : SUGGESTED;
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
      this.$refs.composer.text = text;
      this.$nextTick(() => this.$refs.composer.focus());
    },
    async submit(payload) {
      if (this.submitting) return;
      this.submitting = true;
      try {
        if (!this.sessionReady) {
          const created = await actions.post('/api/sessions', {
            name: payload.text.slice(0, 40) || '新对话',
            source_ids: payload.sourceIds,
          });
          state.sessions.unshift(created.item);
          state.activeSessionId = created.item.id;
        }
        const created = await actions.post('/api/analyses', {
          session_id: state.activeSessionId,
          objective: payload.text,
          source_ids: payload.sourceIds,
          agent_id: payload.agentId || undefined,
          skill_id: payload.skillHint || undefined,
        });
        if (payload.files.length) {
          try {
            await actions.uploadAttachments(created.item.id, payload.files);
          } catch (error) {
            await actions.post(`/api/analyses/${created.item.id}/control`, { action: 'cancel' }).catch(() => {});
            throw error;
          }
        }
        if (state.routeParams.file) {
          try {
            await actions.post(`/api/analyses/${created.item.id}/attachments/library`, {
              record_id: state.routeParams.file,
            });
          } catch (error) {
            await actions.post(`/api/analyses/${created.item.id}/control`, { action: 'cancel' }).catch(() => {});
            throw error;
          }
        }
        this.$refs.composer.reset();
        navigate('conversation', { id: state.activeSessionId });
      } catch (error) {
        toast(error.message, '无法开始分析', 'error');
      } finally {
        this.submitting = false;
      }
    },
    async seed() {
      this.seeding = true;
      try {
        await loadDemo();
        toast('演示数据已载入，可以直接提问', '完成');
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
          <h1 class="workbench__title">{{ greeting }}，今天想分析什么？</h1>
          <p class="muted" style="margin-top:6px">直接用业务语言提问，系统会自动选择合适的能力并给出可核验的结论。</p>
        </div>

        <Composer ref="composer" :disabled="seeding || submitting || !canAnalyze" @submit="submit" />
        <p v-if="!canAnalyze" class="small muted">当前为只读权限，可查看已有内容；发起分析需要分析权限。</p>

        <div class="workbench__capabilities">
          <div v-for="item in capabilities" :key="item.label" class="capability">
            <Icon :name="item.icon" :size="19" />
            <span>{{ item.label }}</span>
          </div>
        </div>

        <div v-if="needsData" class="card" style="margin-top:22px;display:flex;align-items:center;gap:14px">
          <Icon name="database" :size="22" class="muted" />
          <div class="grow">
            <b>还没有可分析的数据</b>
            <p class="small muted">接入自己的数据源，或者先载入一套演示数据体验完整链路。</p>
          </div>
          <button class="btn btn--sm" @click="navigate('admin/data')">接入数据</button>
          <button class="btn btn--primary btn--sm" @click="demoOpen = true">使用演示数据</button>
        </div>

        <div v-else-if="questions.length" class="workbench__suggest">
          <div class="suggest-title"><Icon name="sparkle" :size="15" />试试这些问题</div>
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
        并同步建立正式指标、业务知识和默认智能体。数据是确定性的，
        所以下面这些问题每次都会得到同样的答案。
      </p>
      <ul class="stack" style="display:flex;flex-direction:column;gap:6px">
        <li v-for="text in sampleQuestions" :key="text" class="small muted">· {{ text }}</li>
      </ul>
      <template #footer>
        <button class="btn" @click="demoOpen = false">取消</button>
        <button class="btn btn--primary" :disabled="seeding" @click="seed">
          {{ seeding ? '正在载入…' : '载入并开始' }}
        </button>
      </template>
    </Modal>`,
};
