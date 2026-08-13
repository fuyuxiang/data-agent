import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as chatApi from '@/api/chat'
import { ApiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import type { AskResponse } from '@/api/types'

function answered(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    status: 'answered',
    conversation_id: 7,
    turn_id: 11,
    answer: {
      headline: '本月销售额 4,235 万',
      conclusion: '环比 +12.4%',
      assumptions: [],
      warnings: [],
      citation: {
        metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
        time: '2026-08-01 ~ 2026-08-31（按完成日期）',
        filters: [],
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

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
})

describe('useSessionStore', () => {
  it('appends the question then the answer', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()

    await store.submit('本月销售额')

    expect(store.messages.map((item) => item.role)).toEqual(['user', 'agent'])
    expect(store.messages[1].kind).toBe('answer')
  })

  it('adopts the conversation id returned by the first turn', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()

    await store.submit('本月销售额')

    expect(store.activeConversationId).toBe(7)
  })

  it('sends the conversation id on follow-up turns', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()

    await store.submit('本月销售额')
    await store.submit('那按省份看')

    expect(spy.mock.calls[1][0].conversation_id).toBe(7)
  })

  it('mirrors slot_state so the condition panel stays in sync', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()

    await store.submit('本月销售额')

    expect(store.slotState?.metrics).toEqual(['sales_revenue'])
  })

  it('keeps the previous slots when a turn returns none', async () => {
    const store = useSessionStore()
    vi.spyOn(chatApi, 'ask').mockResolvedValueOnce(answered())
    await store.submit('本月销售额')

    vi.spyOn(chatApi, 'ask').mockResolvedValueOnce(
      answered({
        status: 'refused',
        answer: null,
        refusal_reason: '你没有该数据的访问权限',
        slot_state: null,
      })
    )
    await store.submit('总成本')

    expect(store.slotState?.metrics).toEqual(['sales_revenue'])
  })

  it('renders a refusal as a normal message, not an error', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({
        status: 'refused',
        answer: null,
        refusal_reason: '你没有该数据的访问权限',
      })
    )
    const store = useSessionStore()

    await store.submit('总成本')

    expect(store.messages[1].kind).toBe('refusal')
    expect(store.error).toBe('')
  })

  it('renders clarifications as a clarify message', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({
        status: 'clarifying',
        answer: null,
        clarifications: [
          {
            kind: 'metric',
            target: 'sales_revenue',
            question: '你指的是哪个销售额？',
            options: [
              { value: 'sales_revenue', label: '含税订单金额', hint: '' },
              { value: 'net_revenue', label: '财务确认收入', hint: '' },
            ],
          },
        ],
      })
    )
    const store = useSessionStore()

    await store.submit('业绩怎么样')

    expect(store.messages[1].kind).toBe('clarify')
    expect(store.messages[1].clarifications).toHaveLength(1)
  })

  it('answering a clarification asks again with the chosen label', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()

    await store.answerClarification(
      {
        kind: 'metric',
        target: 'sales_revenue',
        question: '你指的是哪个销售额？',
        options: [],
      },
      { value: 'sales_revenue', label: '含税订单金额', hint: '' }
    )

    expect(spy.mock.calls[0][0].question).toContain('含税订单金额')
  })

  it('rerunning with edited slots submits a sentence built from the slots', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()
    await store.submit('本月销售额')

    await store.rerunWithSlots({
      ...answered().slot_state!,
      dimensions: ['province'],
    })

    expect(spy.mock.calls[1][0].question).toContain('province')
  })

  it('sets asking while the request is in flight', async () => {
    let release!: (value: AskResponse) => void
    vi.spyOn(chatApi, 'ask').mockReturnValue(
      new Promise<AskResponse>((resolve) => {
        release = resolve
      })
    )
    const store = useSessionStore()

    const pending = store.submit('本月销售额')
    expect(store.asking).toBe(true)
    release(answered())
    await pending
    expect(store.asking).toBe(false)
  })

  it('a transport error becomes store.error, not a message', async () => {
    vi.spyOn(chatApi, 'ask').mockRejectedValue(new ApiError('无法连接服务', 0))
    const store = useSessionStore()

    await store.submit('本月销售额')

    expect(store.error).toContain('无法连接服务')
    expect(store.messages.some((item) => item.role === 'agent')).toBe(false)
  })

  it('ignores an empty question', async () => {
    const spy = vi.spyOn(chatApi, 'ask')
    const store = useSessionStore()

    await store.submit('   ')

    expect(spy).not.toHaveBeenCalled()
  })

  it('opening a conversation replaces the message list', async () => {
    vi.spyOn(chatApi, 'listTurns').mockResolvedValue([
      {
        id: 1,
        question: '本月销售额',
        status: 'answered',
        answer: { headline: '4,235 万', conclusion: '' },
        created_at: '2026-08-12T09:00:00Z',
      },
    ])
    const store = useSessionStore()

    await store.openConversation(7)

    expect(store.activeConversationId).toBe(7)
    expect(store.messages).toHaveLength(2)
  })

  it('startNew clears the conversation and the slots', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const store = useSessionStore()
    await store.submit('本月销售额')

    store.startNew()

    expect(store.activeConversationId).toBeNull()
    expect(store.messages).toHaveLength(0)
    expect(store.slotState).toBeNull()
  })

  it('loadConversations fills the sidebar', async () => {
    vi.spyOn(chatApi, 'listConversations').mockResolvedValue([
      { id: 7, title: '华东销售', dataset_name: 'orders', updated_at: '2026-08-12T09:00:00Z' },
    ])
    const store = useSessionStore()

    await store.loadConversations()

    expect(store.conversations[0].title).toBe('华东销售')
  })
})