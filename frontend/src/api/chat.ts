import { request } from '@/api/client'
import type { AskResponse, Conversation, FeedbackCategory, Turn } from '@/api/types'

export function ask(payload: {
  question: string
  dataset_name: string
  conversation_id?: number | null
}): Promise<AskResponse> {
  return request<AskResponse>('/api/chat/ask', { method: 'POST', body: payload })
}

export function listConversations(): Promise<Conversation[]> {
  return request<Conversation[]>('/api/chat/conversations')
}

export function listTurns(conversationId: number): Promise<Turn[]> {
  return request<Turn[]>(`/api/chat/conversations/${conversationId}/turns`)
}

export function sendFeedback(
  turnId: number,
  payload: { is_positive: boolean; category?: FeedbackCategory; comment?: string },
): Promise<void> {
  return request<void>(`/api/chat/turns/${turnId}/feedback`, {
    method: 'POST',
    body: payload,
  })
}