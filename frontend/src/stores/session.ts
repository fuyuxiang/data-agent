import { defineStore } from 'pinia'
import { ref } from 'vue'

import { ask, listConversations, listTurns } from '@/api/chat'
import { ApiError } from '@/api/client'
import type {
  Answer,
  AskResponse,
  Clarification,
  ClarifyOption,
  Conversation,
  SlotState,
} from '@/api/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  kind: 'question' | 'answer' | 'clarify' | 'refusal' | 'failure'
  turnId: number
  question?: string
  answer?: Answer
  clarifications?: Clarification[]
  reason?: string
}

const DEFAULT_DATASET = 'orders'

const KIND_BY_STATUS = {
  answered: 'answer',
  clarifying: 'clarify',
  refused: 'refusal',
  failed: 'failure',
} as const

let counter = 0
function nextId(): string {
  counter += 1
  return `m${counter}`
}

function slotsToQuestion(slots: SlotState): string {
  const parts: string[] = []
  if (slots.time?.expression) parts.push(slots.time.expression)
  for (const filter of slots.filters) {
    const values = filter.spoken_values.length ? filter.spoken_values : filter.values
    if (values.length) parts.push(values.join('、'))
  }
  if (slots.dimensions.length) parts.push(`按${slots.dimensions.join('、')}`)
  parts.push(slots.metrics.join('、'))
  if (slots.comparison !== 'none') parts.push(slots.comparison)
  return parts.filter(Boolean).join(' ')
}

export const useSessionStore = defineStore('session', () => {
  const conversations = ref<Conversation[]>([])
  const activeConversationId = ref<number | null>(null)
  const messages = ref<ChatMessage[]>([])
  const slotState = ref<SlotState | null>(null)
  const asking = ref(false)
  const error = ref('')

  function apply(response: AskResponse): void {
    activeConversationId.value = response.conversation_id
    if (response.slot_state) slotState.value = response.slot_state
    messages.value.push({
      id: nextId(),
      role: 'agent',
      kind: KIND_BY_STATUS[response.status],
      turnId: response.turn_id,
      answer: response.answer ?? undefined,
      clarifications: response.clarifications.length ? response.clarifications : undefined,
      reason: response.refusal_reason || undefined,
    })
  }

  async function submit(question: string): Promise<void> {
    const text = question.trim()
    if (!text || asking.value) return

    error.value = ''
    messages.value.push({
      id: nextId(),
      role: 'user',
      kind: 'question',
      turnId: 0,
      question: text,
    })
    asking.value = true
    try {
      apply(
        await ask({
          question: text,
          dataset_name: DEFAULT_DATASET,
          conversation_id: activeConversationId.value,
        })
      )
    } catch (raised) {
      error.value = raised instanceof ApiError ? raised.message : '请求失败'
    } finally {
      asking.value = false
    }
  }

  async function answerClarification(
    _request: Clarification,
    option: ClarifyOption
  ): Promise<void> {
    await submit(option.label)
  }

  async function rerunWithSlots(slots: SlotState): Promise<void> {
    slotState.value = slots
    await submit(slotsToQuestion(slots))
  }

  async function loadConversations(): Promise<void> {
    try {
      conversations.value = await listConversations()
    } catch (raised) {
      error.value = raised instanceof ApiError ? raised.message : '会话加载失败'
    }
  }

  async function openConversation(conversationId: number): Promise<void> {
    const turns = await listTurns(conversationId)
    activeConversationId.value = conversationId
    slotState.value = null
    messages.value = turns.flatMap<ChatMessage>((turn) => [
      {
        id: nextId(),
        role: 'user',
        kind: 'question',
        turnId: turn.id,
        question: turn.question,
      },
      {
        id: nextId(),
        role: 'agent',
        kind: KIND_BY_STATUS[turn.status],
        turnId: turn.id,
        answer: turn.answer
          ? {
              headline: turn.answer.headline,
              conclusion: turn.answer.conclusion,
              assumptions: [],
              warnings: [],
              citation: null,
              drill_downs: [],
              columns: [],
              rows: [],
            }
          : undefined,
      },
    ])
  }

  function startNew(): void {
    activeConversationId.value = null
    messages.value = []
    slotState.value = null
    error.value = ''
  }

  return {
    conversations,
    activeConversationId,
    messages,
    slotState,
    asking,
    error,
    submit,
    answerClarification,
    rerunWithSlots,
    loadConversations,
    openConversation,
    startNew,
  }
})