import { request } from '@/api/client'
import type { Replay, Trace } from '@/api/types'

export function getTrace(turnId: number): Promise<Trace> {
  return request<Trace>(`/api/trace/turns/${turnId}`)
}

export function replayTurn(turnId: number): Promise<Replay> {
  return request<Replay>(`/api/trace/turns/${turnId}/replay`, { method: 'POST' })
}