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
  { key: 'understood', label: '接收分析问题', done: () => true },
  { key: 'queried', label: '查询相关数据', done: (run) => run.hasData },
  { key: 'analyzed', label: '整理分析结果', done: (run) => run.hasChart || run.hasAnalysis },
  { key: 'validated', label: '校验结果依据', done: (run) => run.validated },
  { key: 'delivered', label: '汇总分析输出', done: (run) => run.hasConclusion || run.execution_status === 'finished' },
];

const STOP_LABELS = {
  contract_confirmation_required: '请确认分析口径后开始分析',
  waiting_input: '请补充分析对象、时间范围或统计口径',
  model_not_configured: '自主分析暂不可用，请联系工作空间所有者配置可用模型。',
  model_unavailable: '分析所用模型暂不可用，请稍后重试或联系管理员。',
  model_refusal: '模型拒绝回答该问题，请核对问题内容后重新提问。',
  model_budget_exceeded: '本次分析已达到模型用量上限，请缩小问题范围后重试。',
  daily_model_budget_exceeded: '工作空间今日模型额度已用完，请联系管理员或次日重试。',
  run_time_budget_exceeded: '分析已达到执行时长上限，请缩小数据范围后重试。',
  iteration_budget_exceeded: '分析轮次已达上限，请聚焦单个问题后重试。',
  repeated_tool_failures: '查询或分析工具连续失败，请检查数据范围或联系管理员。',
  no_progress_repeated_action: '分析未取得进一步进展，已停止；请补充问题说明后重试。',
  publication_gate_blocked: '结果未通过发布校验，暂不能生成正式成果。请核对数据和口径后重新分析。',
  tool_approval_required: '等待操作授权，请联系管理员确认。',
  external_job_running: '外部计算任务正在运行',
  user_cancelled: '已取消',
  cancel_confirmation_pending: '已请求终止，正在确认外部任务是否停止',
  cancel_retry_pending: '暂时无法确认任务已停止，系统正在重试',
  empty_model_output: '模型未返回分析内容，请稍后重试。',
  invalid_model_protocol: '模型返回内容格式异常，请重新提问或联系管理员。',
  model_length: '模型回复达到长度上限，请缩小问题范围后重试。',
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
      if (this.status === 'cancelling') return '正在终止分析';
      if (this.status === 'partial') return '分析部分完成';
      if (this.status === 'finished') {
        return this.run.quality_status === 'passed' ? '分析已结束，结果通过发布校验' : '本次分析已结束';
      }
      if (this.status === 'waiting_input') return '等待补充或确认信息';
      if (this.status === 'waiting_approval') return '等待操作授权';
      return this.title;
    },
    problem() {
      const reason = STOP_LABELS[this.run.stop_reason] || '';
      if (reason) return reason;
      if (this.status === 'failed') return '分析未完成，请检查数据范围、补充问题说明后重试。';
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
