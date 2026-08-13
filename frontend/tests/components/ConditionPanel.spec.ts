import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConditionPanel from '@/components/workbench/ConditionPanel.vue'
import type { DatasetDetail, SlotState } from '@/api/types'

function dataset(): DatasetDetail {
  return {
    name: 'orders',
    business_name: '订单',
    is_published: true,
    physical_table: 'sample.orders',
    description: '',
    grain: '一行一个订单',
    applicable_scenario: '',
    forbidden_scenario: '',
    aliases: [],
    updated_at: null,
    fields: [
      {
        name: 'region_code',
        business_name: '大区',
        synonyms: [],
        semantic_type: 'dimension',
        unit: '',
        display_format: '',
        default_aggregation: 'none',
        allowed_aggregations: [],
        is_filterable: true,
        is_groupable: true,
        sensitivity: 'public',
        enum_values: [
          { physical_value: 'EC', business_value: '华东', aliases: [], description: '' },
          { physical_value: 'SC', business_value: '华南', aliases: [], description: '' },
        ],
      },
      {
        name: 'province',
        business_name: '省份',
        synonyms: [],
        semantic_type: 'dimension',
        unit: '',
        display_format: '',
        default_aggregation: 'none',
        allowed_aggregations: [],
        is_filterable: true,
        is_groupable: true,
        sensitivity: 'public',
        enum_values: [],
      },
      {
        name: 'amount',
        business_name: '订单金额',
        synonyms: [],
        semantic_type: 'measure',
        unit: '元',
        display_format: '',
        default_aggregation: 'sum',
        allowed_aggregations: ['sum', 'avg'],
        is_filterable: true,
        is_groupable: false,
        sensitivity: 'internal',
        enum_values: [],
      },
    ],
    metrics: [
      {
        name: 'sales_revenue',
        business_name: '销售额',
        version: 3,
        kind: 'simple',
        aggregation_behavior: 'additive',
        description: '已完成订单含税金额',
        unit: '元',
        display_format: '',
      },
      {
        name: 'order_count',
        business_name: '订单数',
        version: 1,
        kind: 'simple',
        aggregation_behavior: 'additive',
        description: '',
        unit: '单',
        display_format: '',
      },
    ],
  }
}

function slots(overrides: Partial<SlotState> = {}): SlotState {
  return {
    kind: 'aggregate',
    dataset: 'orders',
    metrics: ['sales_revenue'],
    dimensions: [],
    filters: [
      { field: 'region_code', operator: 'in', values: ['EC'], spoken_values: ['华东'] },
    ],
    time: { start: '2026-08-01', end: '2026-08-31', grain: 'month', expression: '本月' },
    comparison: 'mom',
    sort: null,
    assumptions: [],
    ...overrides,
  }
}

describe('ConditionPanel', () => {
  it('shows the current metric, time, dimension and filter slots', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    const text = wrapper.text()
    expect(text).toContain('指标')
    expect(text).toContain('时间')
    expect(text).toContain('维度')
    expect(text).toContain('过滤')
  })

  it('offers only groupable fields as dimensions', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(
      wrapper.vm.dimensionOptions.map((item: { value: string }) => item.value)
    ).toEqual(['region_code', 'province'])
  })

  it('offers only filterable fields as filters', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.filterFieldOptions).toHaveLength(3)
  })

  it('offers enum values by their business value, never the physical code', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    const options = wrapper.vm.enumOptions('region_code')
    expect(options.map((item: { label: string }) => item.label)).toEqual(['华东', '华南'])
  })

  it('shows spoken values for the current filter, not physical codes', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.text()).toContain('华东')
    expect(wrapper.text()).not.toContain("'EC'")
  })

  it('emits rerun with the edited slots', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })

    wrapper.vm.draft.dimensions = ['province']
    await wrapper.vm.$nextTick()
    await wrapper.find('[data-test="rerun"]').trigger('click')

    expect(wrapper.emitted('rerun')?.[0][0]).toMatchObject({ dimensions: ['province'] })
  })

  it('resyncs the draft when a new turn returns new slots', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })

    await wrapper.setProps({ slotState: slots({ dimensions: ['province'] }) })

    expect(wrapper.vm.draft.dimensions).toEqual(['province'])
  })

  it('marks itself dirty once the user edits something', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.dirty).toBe(false)

    wrapper.vm.draft.comparison = 'yoy'
    await wrapper.vm.$nextTick()

    expect(wrapper.vm.dirty).toBe(true)
  })

  it('disables rerun while a request is in flight', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: true },
    })
    expect(wrapper.find('[data-test="rerun"]').attributes('disabled')).toBeDefined()
  })

  it('shows an empty hint before the first turn', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: null, dataset: dataset(), running: false },
    })
    expect(wrapper.text()).toContain('提问后')
  })

  it('does not offer a metric the dataset never published', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(
      wrapper.vm.metricOptions.map((item: { value: string }) => item.value)
    ).toEqual(['sales_revenue', 'order_count'])
  })

  it('labels metrics with their version so the caliber is unambiguous', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.metricOptions[0].label).toContain('v3')
  })
})