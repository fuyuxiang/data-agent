import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as chatApi from '@/api/chat'
import FeedbackBar from '@/components/chat/FeedbackBar.vue'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('FeedbackBar', () => {
  it('thumbs-up submits immediately without a dialog', async () => {
    const spy = vi.spyOn(chatApi, 'sendFeedback').mockResolvedValue()
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })

    await wrapper.find('[data-test="thumb-up"]').trigger('click')

    expect(spy).toHaveBeenCalledWith(11, { is_positive: true })
  })

  it('thumbs-down opens the attribution dialog instead of submitting', async () => {
    const spy = vi.spyOn(chatApi, 'sendFeedback').mockResolvedValue()
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })

    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    expect(spy).not.toHaveBeenCalled()
    expect(wrapper.vm.dialogVisible).toBe(true)
  })

  it('offers the five attribution categories', async () => {
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })
    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    expect(
      wrapper.vm.categories.map((item: { value: string }) => item.value)
    ).toEqual(['metric', 'time', 'sql', 'calculation', 'conclusion'])
  })

  it('cannot submit a thumbs-down without picking a category', async () => {
    const spy = vi.spyOn(chatApi, 'sendFeedback').mockResolvedValue()
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })
    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    await wrapper.vm.confirm()

    expect(spy).not.toHaveBeenCalled()
    expect(wrapper.vm.hint).toContain('请选择')
  })

  it('submits the category and the comment', async () => {
    const spy = vi.spyOn(chatApi, 'sendFeedback').mockResolvedValue()
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })
    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    wrapper.vm.category = 'metric'
    wrapper.vm.comment = '口径不对'
    await wrapper.vm.confirm()

    expect(spy).toHaveBeenCalledWith(11, {
      is_positive: false,
      category: 'metric',
      comment: '口径不对',
    })
  })

  it('marks itself submitted so the same turn is not rated twice', async () => {
    vi.spyOn(chatApi, 'sendFeedback').mockResolvedValue()
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })

    await wrapper.find('[data-test="thumb-up"]').trigger('click')
    await wrapper.vm.$nextTick()

    expect(
      wrapper.find('[data-test="thumb-up"]').attributes('disabled')
    ).toBeDefined()
  })

  it('keeps the dialog open and shows the reason when the server rejects', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(chatApi, 'sendFeedback').mockRejectedValue(
      new ApiError('负反馈必须归因', 422)
    )
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })
    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    wrapper.vm.category = 'metric'
    await wrapper.vm.confirm()

    expect(wrapper.vm.dialogVisible).toBe(true)
    expect(wrapper.vm.hint).toContain('负反馈必须归因')
  })
})