import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as chatApi from '@/api/chat'
import * as semanticApi from '@/api/semantic'
import WorkbenchView from '@/views/WorkbenchView.vue'
import type { AskResponse, DatasetDetail } from '@/api/types'

function answered(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    status: 'answered',
    conversation_id: 7,
    turn_id: 11,
    answer: {
      headline: '本月销售额 4,235 万',
      conclusion: '',
      assumptions: [],
      warnings: [],
      citation: {
        metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
        time: '2026-08-01 ~ 2026-08-31（按完成日期）',
        filters: [{ label: '大区', value: '属于 华东', source: 'permission' }],
        data_updated_at: '2026-08-12 09:00',
      },
      drill_downs: [],
      columns: ['sales_revenue'],
      rows: [[42350000]],
    },
    clarifications: [],
    refusal_reason: '',
    slot_state: {
      kind: 'aggregate',
      dataset: 'orders',
      metrics: ['sales_revenue'],
      dimensions: [],
      filters: [],
      time: { start: '2026-08-01', end: '2026-08-31', grain: 'month', expression: '本月' },
      comparison: 'none',
      sort: null,
      assumptions: [],
    },
    ...overrides,
  }
}

function dataset(): DatasetDetail {
  return {
    name: 'orders',
    business_name: '订单',
    is_published: true,
    physical_table: 'sample.orders',
    description: '',
    grain: '',
    applicable_scenario: '',
    forbidden_scenario: '',
    aliases: [],
    updated_at: null,
    fields: [],
    metrics: [],
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  vi.spyOn(chatApi, 'listConversations').mockResolvedValue([
    { id: 7, title: '华东销售', dataset_name: 'orders', updated_at: '2026-08-12T09:00:00Z' },
  ])
  vi.spyOn(semanticApi, 'getDataset').mockResolvedValue(dataset())
})

async function mounted() {
  const wrapper = mount(WorkbenchView)
  await flushPromises()
  return wrapper
}

describe('WorkbenchView', () => {
  it('renders the three panes', async () => {
    const wrapper = await mounted()

    expect(wrapper.find('[data-test="pane-conversations"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-chat"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-evidence"]').exists()).toBe(true)
  })

  it('loads the conversation list on mount', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('华东销售')
  })

  it('asking renders the answer with its citation', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('4,235 万')
    expect(wrapper.text()).toContain('由数据权限自动附加')
  })

  it('fills the condition panel from the returned slots', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="rerun"]').exists()).toBe(true)
  })

  it('shows the result table in the evidence pane', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-evidence"]').text()).toContain('sales_revenue')
  })

  it('offers a trace link for the latest turn', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="open-trace"]').exists()).toBe(true)
  })

  it('clicking a clarify option asks again', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({
        status: 'clarifying',
        answer: null,
        clarifications: [
          {
            kind: 'metric',
            target: 'sales_revenue',
            question: '你指的是哪个销售额？',
            options: [{ value: 'sales_revenue', label: '含税订单金额', hint: '' }],
          },
        ],
      })
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('业绩怎么样')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="clarify-option"]').trigger('click')
    await flushPromises()

    expect(spy.mock.calls[1][0].question).toContain('含税订单金额')
  })

  it('rerunning from the panel asks again', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="rerun"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('a refusal is shown in the stream, not as an error banner', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({
        status: 'refused',
        answer: null,
        refusal_reason: '你没有该数据的访问权限',
      })
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('总成本')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-chat"]').text()).toContain('你没有该数据的访问权限')
    expect(wrapper.find('[data-test="transport-error"]').exists()).toBe(false)
  })

  it('a transport failure shows a banner', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(chatApi, 'ask').mockRejectedValue(new ApiError('无法连接服务', 0))
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="transport-error"]').text()).toContain('无法连接服务')
  })

  it('disables the input while asking', async () => {
    let release: (value: AskResponse) => void = () => {}
    vi.spyOn(chatApi, 'ask').mockReturnValue(
      new Promise<AskResponse>((resolve) => {
        release = resolve
      })
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-test="ask-submit"]').attributes('disabled')).toBeDefined()
    release(answered())
    await flushPromises()
  })

  it('selecting a conversation loads its turns', async () => {
    const spy = vi.spyOn(chatApi, 'listTurns').mockResolvedValue([])
    const wrapper = await mounted()

    await wrapper.find('[data-test="conversation-item"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith(7)
  })

  it('starting a new conversation clears the stream', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="new-conversation"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-chat"]').text()).not.toContain('4,235 万')
  })

  it('has no mode selector this round', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="mode-selector"]').exists()).toBe(false)
  })
})