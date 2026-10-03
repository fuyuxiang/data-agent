/**
 * 执行状态。
 *
 * 重点打磨区 A。这里显示的是**任务步骤**，不是模型私有思维链——
 * 用户需要知道"到哪一步了"，不需要知道模型在内部想什么。
 */

import { Icon } from './icons.js';
import { Status } from './ui.js';

/** 把后端事件与运行状态翻译成用户看得懂的任务步骤。 */
const STEPS = [
  { key: 'understood', label: '已理解问题', done: () => true },
  { key: 'queried', label: '已查询数据', done: (run) => run.hasData },
  { key: 'analyzed', label: '已完成分析', done: (run) => run.hasChart || run.hasAnalysis },
  { key: 'validated', label: '已核验结果', done: (run) => run.validated },
  { key: 'delivered', label: '已生成结论', done: (run) => ['finished', 'partial'].includes(run.execution_status) },
];

const STOP_LABELS = {
  contract_confirmation_required: '等待确认分析口径',
  waiting_input: '需要补充信息',
  model_not_configured: '尚未配置模型，无法自主分析',
  model_unavailable: '模型服务暂时不可用',
  model_refusal: '模型拒绝回答该问题',
  model_budget_exceeded: '本次运行超出模型预算',
  daily_model_budget_exceeded: '工作空间今日模型额度已用完',
  run_time_budget_exceeded: '分析超过最长执行时间',
  iteration_budget_exceeded: '分析轮次已达上限',
  repeated_tool_failures: '连续多次取数失败',
  no_progress_repeated_action: '重复动作过多，已中止',
  publication_gate_blocked: '结果未通过核验门禁',
  tool_approval_required: '等待工具授权',
  external_job_running: '外部作业运行中',
  user_cancelled: '已取消',
  empty_model_output: '模型没有返回内容',
  invalid_model_protocol: '模型返回格式异常',
  model_length: '模型输出被长度限制截断',
};

export const ExecutionStatus = {
  name: 'ExecutionStatus',
  components: { Icon, Status },
  props: {
    run: { type: Object, default: () => ({}) },
    title: { type: String, default: '正在分析' },
  },
  computed: {
    status() { return this.run.execution_status || 'queued'; },
    finished() { return ['finished', 'failed', 'cancelled', 'partial'].includes(this.status); },
    steps() {
      const run = this.run;
      const steps = STEPS.map(step => ({
        key: step.key,
        label: step.label,
        state: step.done(run) ? 'done' : (this.finished ? 'skipped' : 'pending'),
      }));
      if (run.execution_status === 'waiting_input') {
        // 等待确认时，卡在"已查询数据"之前会让人以为已经在取数了。
        steps.splice(1, 0, { key: 'confirm', label: '确认分析口径', state: 'pending' });
      }
      return steps;
    },
    activeIndex() {
      const index = this.steps.findIndex(step => step.state !== 'done');
      return index === -1 ? this.steps.length : index;
    },
    headline() {
      if (this.status === 'failed') return '分析未完成';
      if (this.status === 'cancelled') return '分析已取消';
      if (this.status === 'finished' || this.status === 'partial') {
        return this.run.quality_status === 'passed' ? '分析已完成并通过核验' : '分析已完成';
      }
      if (this.status === 'waiting_input') return '等待你的确认';
      if (this.status === 'waiting_approval') return '等待工具授权';
      return this.title;
    },
    problem() {
      const reason = STOP_LABELS[this.run.stop_reason] || '';
      if (reason) return reason;
      if (this.status === 'failed') return '分析没有完成，可以换一种问法或检查数据范围。';
      return '';
    },
    elapsed() {
      const seconds = Number(this.run.duration_seconds);
      if (Number.isFinite(seconds) && seconds > 0) {
        return seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.round(seconds / 60)} 分钟`;
      }
      return '';
    },
  },
  methods: {
    stepState(index) {
      const step = this.steps[index];
      if (step.state === 'done') return 'done';
      if (step.state === 'skipped') return 'pending';
      return index === this.activeIndex ? 'active' : 'pending';
    },
  },
  template: `
    <div class="execution" :data-state="status">
      <div class="execution__head">
        <span class="execution__pulse"></span>
        <b class="grow">{{ headline }}</b>
        <span v-if="elapsed" class="execution__time">{{ elapsed }}</span>
        <Status :status="status" />
      </div>
      <div v-if="!finished || status === 'partial'" class="execution__steps">
        <span v-for="(step, index) in steps" :key="step.key" class="step" :data-state="stepState(index)">
          <span class="step__mark">
            <Icon v-if="stepState(index) === 'done'" name="check" :size="10" />
          </span>
          {{ step.label }}
        </span>
      </div>
      <p v-if="problem" class="execution__error">{{ problem }}</p>
    </div>`,
};

export { STOP_LABELS };
