import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as semanticApi from '@/api/semantic'
import DatasetDetailView from '@/views/DatasetDetailView.vue'
import type { DatasetDetail, LintReport } from '@/api/types'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { name: 'orders' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

function detail(overrides: Partial<DatasetDetail> = {}): DatasetDetail {
  return {
    name: 'orders',
    business_name: '订单',
    physical_table: 'sample.orders',
    grain: '一行一个订单',
    is_published: false,
    updated_at: '2026-08-12T09:00:00Z',
    aliases: ['销售单'],
    description: '订单明细宽表',
    applicable_scenario: '可用于销售额、订单数分析',
    forbidden_scenario: '不可用于财务对账',
    fields: [
      {
        name: 'customer_name',
        business_name: '客户名称',
        synonyms: ['客户'],
        semantic_type: 'attribute',
        unit: '',
        display_format: '',
        default_aggregation: 'none',
        allowed_aggregations: [],
        is_filterable: true,
        is_groupable: true,
        sensitivity: 'sensitive',
        enum_values: [],
      },
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
          { physical_value: 'EC', business_value: '华东', aliases: ['东区'], description: '' },
        ],
      },
    ],
    metrics: [
      {
        name: 'gross_margin_rate',
        business_name: '毛利率',
        version: 2,
        kind: 'ratio',
        aggregation_behavior: 'recalculate',
        description: '',
        unit: '',
        display_format: '0.00%',
      },
    ],
    ...overrides,
  }
}

function report(overrides: Partial<LintReport> = {}): LintReport {
  return { dataset: 'orders', publishable: true, issues: [], ...overrides }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(semanticApi, 'getDataset').mockResolvedValue(detail())
  vi.spyOn(semanticApi, 'getLint').mockResolvedValue(report())
})

async function mounted() {
  const wrapper = mount(DatasetDetailView)
  await flushPromises()
  return wrapper
}

describe('DatasetDetailView', () => {
  it('shows the grain and the applicable scenario', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('一行一个订单')
    expect(wrapper.text()).toContain('可用于销售额')
  })

  it('shows the forbidden scenario as a warning', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="forbidden"]').text()).toContain('不可用于财务对账')
  })

  it('lists fields with their sensitivity', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="fields"]').text()).toContain('客户名称')
    expect(wrapper.find('[data-test="fields"]').text()).toContain('sensitive')
  })

  it('shows enum values with aliases', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('华东')
    expect(wrapper.text()).toContain('东区')
  })

  it('shows metric version and aggregation behavior', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="metrics"]').text()).toContain('v2')
    expect(wrapper.find('[data-test="metrics"]').text()).toContain('recalculate')
  })

  it('publish is enabled when the lint report is clean', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="publish"]').attributes('disabled')).toBe('false')
  })

  it('publish is blocked when lint found errors', async () => {
    vi.spyOn(semanticApi, 'getLint').mockResolvedValue(
      report({
        publishable: false,
        issues: [{ severity: 'error', target: 'sales_revenue', message: '缺少时间字段' }],
      })
    )
    const wrapper = await mounted()

    expect(wrapper.find('[data-test="publish"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('缺少时间字段')
  })

  it('warnings do not block publishing', async () => {
    vi.spyOn(semanticApi, 'getLint').mockResolvedValue(
      report({
        publishable: true,
        issues: [{ severity: 'warning', target: 'province', message: '缺少同义词' }],
      })
    )
    const wrapper = await mounted()

    expect(wrapper.find('[data-test="publish"]').attributes('disabled')).toBe('false')
    expect(wrapper.text()).toContain('缺少同义词')
  })

  it('publishing refreshes the dataset state', async () => {
    const publish = vi
      .spyOn(semanticApi, 'publishDataset')
      .mockResolvedValue({ published: true })
    vi.spyOn(semanticApi, 'getDataset')
      .mockResolvedValueOnce(detail())
      .mockResolvedValueOnce(detail({ is_published: true }))
    const wrapper = await mounted()

    await wrapper.find('[data-test="publish"]').trigger('click')
    await flushPromises()

    expect(publish).toHaveBeenCalledWith('orders')
    expect(wrapper.text()).toContain('已发布')
  })

  it('a rejected publish shows the reason', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(semanticApi, 'publishDataset').mockRejectedValue(
      new ApiError('语义体检未通过，无法发布', 409)
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="publish"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('语义体检未通过')
  })

  it('offers no field editing form this round', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="field-editor"]').exists()).toBe(false)
  })
})