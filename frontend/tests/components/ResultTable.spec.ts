import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ResultTable from '@/components/workbench/ResultTable.vue'

const props = {
  columns: ['province', 'sales_revenue'],
  rows: [
    ['江苏', 18200000],
    ['浙江', 15600000],
  ],
}

describe('ResultTable', () => {
  it('renders one column per name', () => {
    const wrapper = mount(ResultTable, { props })
    expect(wrapper.vm.tableColumns).toHaveLength(2)
  })

  it('maps rows to keyed records', () => {
    const wrapper = mount(ResultTable, { props })
    expect(wrapper.vm.tableRows[0]).toEqual({
      province: '江苏',
      sales_revenue: 18200000,
    })
  })

  it('renders NULL as a visible placeholder rather than blank', () => {
    const wrapper = mount(ResultTable, {
      props: { columns: ['province'], rows: [[null]] },
    })
    expect(wrapper.vm.tableRows[0].province).toBe('—')
  })

  it('builds csv with a header row', () => {
    const wrapper = mount(ResultTable, { props })
    expect(wrapper.vm.toCsv().split('\n')[0]).toBe('province,sales_revenue')
  })

  it('quotes values containing separators', () => {
    const wrapper = mount(ResultTable, {
      props: { columns: ['name'], rows: [['甲, 乙']] },
    })
    expect(wrapper.vm.toCsv()).toContain('"甲, 乙"')
  })

  it('shows an empty state instead of an empty table', () => {
    const wrapper = mount(ResultTable, { props: { columns: [], rows: [] } })
    expect(wrapper.text()).toContain('暂无数据')
  })
})