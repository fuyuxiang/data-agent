import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as semanticApi from '@/api/semantic'
import DatasetsView from '@/views/DatasetsView.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: {} }),
  useRouter: () => ({ push: vi.fn() }),
}))

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('DatasetsView', () => {
  it('lists datasets with their publish state', async () => {
    vi.spyOn(semanticApi, 'listDatasets').mockResolvedValue([
      {
        name: 'orders',
        business_name: '订单',
        physical_table: 'sample.orders',
        grain: '一行一个订单',
        is_published: true,
        updated_at: '2026-08-12T09:00:00Z',
      },
      {
        name: 'refunds',
        business_name: '退款',
        physical_table: 'sample.refunds',
        grain: '一行一笔退款',
        is_published: false,
        updated_at: null,
      },
    ])
    const wrapper = mount(DatasetsView)
    await flushPromises()

    expect(wrapper.text()).toContain('订单')
    expect(wrapper.text()).toContain('已发布')
    expect(wrapper.text()).toContain('未发布')
  })

  it('shows an empty state when there is none', async () => {
    vi.spyOn(semanticApi, 'listDatasets').mockResolvedValue([])
    const wrapper = mount(DatasetsView)
    await flushPromises()

    expect(wrapper.text()).toContain('还没有数据集')
  })

  it('reports a load failure instead of rendering an empty list', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(semanticApi, 'listDatasets').mockRejectedValue(
      new ApiError('无法连接服务', 0)
    )
    const wrapper = mount(DatasetsView)
    await flushPromises()

    expect(wrapper.text()).toContain('无法连接服务')
  })

  it('links each dataset to its detail page', async () => {
    vi.spyOn(semanticApi, 'listDatasets').mockResolvedValue([
      {
        name: 'orders',
        business_name: '订单',
        physical_table: 'sample.orders',
        grain: '一行一个订单',
        is_published: true,
        updated_at: '2026-08-12T09:00:00Z',
      },
    ])
    const wrapper = mount(DatasetsView)
    await flushPromises()

    expect(wrapper.find('[data-test="dataset-link"]').exists()).toBe(true)
  })
})