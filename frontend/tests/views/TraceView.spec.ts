import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as traceApi from '@/api/trace'
import TraceView from '@/views/TraceView.vue'
import type { Trace, TraceStage } from '@/api/types'

function stage(overrides: Partial<TraceStage> = {}): TraceStage {
  return {
    stage: 'intent',
    sequence: 2,
    input_payload: { question: '本月销售额' },
    output_payload: { metrics: ['sales_revenue'] },
    model: 'stub',
    prompt_tokens: 90,
    completion_tokens: 15,
    elapsed_ms: 420,
    error: null,
    ...overrides,
  }
}

function trace(overrides: Partial<Trace> = {}): Trace {
  return {
    turn_id: 11,
    question: '本月销售额',
    status: 'answered',
    intent_snapshot: { metrics: ['sales_revenue'] },
    stages: [
      stage({ stage: 'verified_recall', sequence: 1, model: null, prompt_tokens: 0, elapsed_ms: 6 }),
      stage(),
      stage({ stage: 'semantic_resolve', sequence: 3, model: null, prompt_tokens: 0 }),
      stage({ stage: 'compile', sequence: 4, model: null, prompt_tokens: 0 }),
      stage({
        stage: 'security',
        sequence: 5,
        model: null,
        prompt_tokens: 0,
        output_payload: {
          sql: "SELECT SUM(amount) FROM sample.orders WHERE region_code IN ('EC')",
        },
      }),
      stage({ stage: 'execute', sequence: 6, model: null, prompt_tokens: 0, elapsed_ms: 38 }),
      stage({ stage: 'answer', sequence: 7, model: null, prompt_tokens: 0 }),
    ],
    ...overrides,
  }
}

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { turnId: '11' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

beforeEach(() => {
  vi.restoreAllMocks()
})

async function mounted(payload = trace()) {
  vi.spyOn(traceApi, 'getTrace').mockResolvedValue(payload)
  const wrapper = mount(TraceView)
  await flushPromises()
  return wrapper
}

describe('TraceView', () => {
  it('shows the question and the turn status', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('本月销售额')
    expect(wrapper.text()).toContain('已作答')
  })

  it('lists all stages in order with chinese labels', async () => {
    const wrapper = await mounted()
    const labels = wrapper.findAll('[data-test="stage-item"]').map((item) => item.text())

    expect(labels[0]).toContain('固定查询召回')
    expect(labels[1]).toContain('意图识别')
    expect(labels[4]).toContain('安全改写')
    expect(labels).toHaveLength(7)
  })

  it('shows elapsed time per stage', async () => {
    const wrapper = await mounted()
    expect(wrapper.findAll('[data-test="stage-item"]')[0].text()).toContain('6')
  })

  it('shows tokens only for the stage that used a model', async () => {
    const wrapper = await mounted()
    const items = wrapper.findAll('[data-test="stage-item"]')

    expect(items[1].text()).toContain('90')
    expect(items[0].text()).not.toContain('Token')
  })

  it('selects the first stage by default', async () => {
    const wrapper = await mounted()
    expect(wrapper.vm.activeSequence).toBe(1)
  })

  it('clicking a stage shows its payloads', async () => {
    const wrapper = await mounted()

    await wrapper.findAll('[data-test="stage-item"]')[4].trigger('click')

    expect(wrapper.find('[data-test="stage-detail"]').text()).toContain('sample.orders')
  })

  it('highlights a failed stage', async () => {
    const payload = trace({
      status: 'failed',
      stages: [stage({ sequence: 1, stage: 'intent', error: 'ValidationError: 缺少 confidence' })],
    })
    const wrapper = await mounted(payload)

    expect(wrapper.find('[data-test="stage-item"]').classes()).toContain('stage--error')
  })

  it('shows the error text of a failed stage', async () => {
    const payload = trace({
      stages: [stage({ sequence: 1, error: 'ValidationError: 缺少 confidence' })],
    })
    const wrapper = await mounted(payload)

    expect(wrapper.text()).toContain('缺少 confidence')
  })

  it('shows the executed sql for the security stage', async () => {
    const wrapper = await mounted()

    await wrapper.findAll('[data-test="stage-item"]')[4].trigger('click')

    expect(wrapper.find('[data-test="stage-sql"]').text()).toContain('SELECT')
  })

  it('replay shows the sql and whether it matches the original', async () => {
    const spy = vi.spyOn(traceApi, 'replayTurn').mockResolvedValue({
      sql: 'SELECT SUM(amount) FROM sample.orders',
      display_sql: 'SELECT SUM(amount) FROM sample.orders',
      matches_original: true,
      applied_row_filters: ['大区'],
      masked_field_names: [],
    })
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith(11)
    expect(wrapper.find('[data-test="replay-result"]').text()).toContain('与原始一致')
  })

  it('replay reports a mismatch loudly', async () => {
    vi.spyOn(traceApi, 'replayTurn').mockResolvedValue({
      sql: 'SELECT SUM(amount) FROM sample.orders',
      display_sql: 'SELECT SUM(amount) FROM sample.orders',
      matches_original: false,
      applied_row_filters: [],
      masked_field_names: [],
    })
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="replay-result"]').text()).toContain('与原始不一致')
  })

  it('a turn without a snapshot reports why replay is unavailable', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(traceApi, 'replayTurn').mockRejectedValue(
      new ApiError('该轮没有可重放的意图快照', 409)
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('没有可重放的意图快照')
  })

  it('a missing turn shows a not-found message', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(traceApi, 'getTrace').mockRejectedValue(new ApiError('记录不存在', 404))
    const wrapper = mount(TraceView)
    await flushPromises()

    expect(wrapper.text()).toContain('记录不存在')
  })
})