import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import type { Clarification } from '@/api/types'

const request: Clarification = {
  kind: 'metric',
  target: 'sales_revenue',
  question: '你指的是哪个销售额？',
  options: [
    { value: 'sales_revenue', label: '含税订单金额', hint: '已完成订单' },
    { value: 'net_revenue', label: '财务确认收入', hint: '' },
  ],
}

describe('ClarifyCard', () => {
  it('shows the question', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.text()).toContain('你指的是哪个销售额？')
  })

  it('renders every option as a clickable button', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.findAll('[data-test="clarify-option"]')).toHaveLength(2)
  })

  it('shows the option hint so the user can tell them apart', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.text()).toContain('已完成订单')
  })

  it('emits choose with the request and the picked option', async () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })

    await wrapper.findAll('[data-test="clarify-option"]')[1].trigger('click')

    expect(wrapper.emitted('choose')?.[0]).toEqual([request, request.options[1]])
  })

  it('renders several clarifications at once', () => {
    const wrapper = mount(ClarifyCard, {
      props: {
        clarifications: [request, { ...request, target: 'time', question: '哪个时间范围？' }],
      },
    })
    expect(wrapper.text()).toContain('哪个时间范围？')
  })

  it('falls back to a text hint when there are no options', () => {
    const wrapper = mount(ClarifyCard, {
      props: { clarifications: [{ ...request, options: [] }] },
    })
    expect(wrapper.text()).toContain('请补充说明')
  })
})