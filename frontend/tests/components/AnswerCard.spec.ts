import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AnswerCard from '@/components/chat/AnswerCard.vue'
import type { ChatMessage } from '@/stores/session'

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    role: 'agent',
    kind: 'answer',
    turnId: 11,
    answer: {
      headline: '华东本月销售额 4,235 万',
      conclusion: '环比 +12.4%',
      assumptions: [],
      warnings: [],
      citation: {
        metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
        time: '2026-08-01 ~ 2026-08-12（按完成日期）',
        filters: [],
        data_updated_at: '2026-08-12 09:00',
      },
      drill_downs: [],
      columns: ['sales_revenue'],
      rows: [[42350000]],
    },
    ...overrides,
  }
}

describe('AnswerCard', () => {
  it('shows the headline and the conclusion', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.text()).toContain('4,235 万')
    expect(wrapper.text()).toContain('环比 +12.4%')
  })

  it('always renders the citation block for an answer', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.find('[data-test="citation"]').exists()).toBe(true)
  })

  it('shows assumptions prominently, not as a footnote', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: {
            ...message().answer!,
            assumptions: ['指标口径未确认，已默认按「销售额」处理'],
          },
        }),
      },
    })
    expect(wrapper.find('[data-test="assumptions"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('已默认按')
  })

  it('shows validation warnings', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: { ...message().answer!, warnings: ['结果超过上限已截断'] },
        }),
      },
    })
    expect(wrapper.text()).toContain('已截断')
  })

  it('renders the feedback bar for an answer', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.find('[data-test="feedback"]').exists()).toBe(true)
  })

  it('a refusal shows the reason and no citation', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          kind: 'refusal',
          answer: undefined,
          reason: '你没有该数据的访问权限',
        }),
      },
    })
    expect(wrapper.text()).toContain('你没有该数据的访问权限')
    expect(wrapper.find('[data-test="citation"]').exists()).toBe(false)
  })

  it('a refusal still offers the trace link so it stays diagnosable', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          kind: 'refusal',
          answer: undefined,
          reason: '超出范围',
        }),
      },
    })
    expect(wrapper.find('[data-test="trace-link"]').exists()).toBe(true)
  })

  it('a refusal offers no feedback bar', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          kind: 'refusal',
          answer: undefined,
          reason: '超出范围',
        }),
      },
    })
    expect(wrapper.find('[data-test="feedback"]').exists()).toBe(false)
  })

  it('renders drill-down suggestions when present', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: {
            ...message().answer!,
            drill_downs: [{ label: '按省份看', kind: 'dimension', target: 'province' }],
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('按省份看')
  })

  it('emits drill-down with the target', async () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: {
            ...message().answer!,
            drill_downs: [{ label: '按省份看', kind: 'dimension', target: 'province' }],
          },
        }),
      },
    })

    await wrapper.find('[data-test="drill-down"]').trigger('click')

    expect(wrapper.emitted('drill')?.[0]).toEqual([
      { label: '按省份看', kind: 'dimension', target: 'province' },
    ])
  })
})