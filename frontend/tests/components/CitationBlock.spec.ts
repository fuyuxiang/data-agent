import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CitationBlock from '@/components/chat/CitationBlock.vue'
import type { Citation } from '@/api/types'

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
    time: '2026-08-01 ~ 2026-08-12（按完成日期）',
    filters: [
      { label: '大区', value: '属于 华东', source: 'user' },
      { label: '大区', value: '属于 华东', source: 'permission' },
    ],
    data_updated_at: '2026-08-12 09:00',
    ...overrides,
  }
}

describe('CitationBlock', () => {
  it('shows the metric caliber including its version', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('sales_revenue v3')
  })

  it('shows the time range and which date field it uses', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('按完成日期')
  })

  it('labels permission-appended filters explicitly', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('由数据权限自动附加')
  })

  it('does not label user-typed filters as permission-appended', () => {
    const wrapper = mount(CitationBlock, {
      props: {
        citation: citation({
          filters: [{ label: '大区', value: '属于 华东', source: 'user' }],
        }),
      },
    })
    expect(wrapper.text()).not.toContain('由数据权限自动附加')
  })

  it('shows the data freshness', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('2026-08-12 09:00')
  })

  it('has no collapse toggle: the block is always expanded', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.find('.el-collapse').exists()).toBe(false)
    expect(wrapper.findAll('details')).toHaveLength(0)
  })

  it('omits the filter row when there is none', () => {
    const wrapper = mount(CitationBlock, {
      props: { citation: citation({ filters: [] }) },
    })
    expect(wrapper.find('[data-test="citation-filters"]').exists()).toBe(false)
  })

  it('omits the freshness row when the backend has none', () => {
    const wrapper = mount(CitationBlock, {
      props: { citation: citation({ data_updated_at: '' }) },
    })
    expect(wrapper.find('[data-test="citation-updated"]').exists()).toBe(false)
  })
})