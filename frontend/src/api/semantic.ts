import { request } from '@/api/client'
import type { DatasetDetail, DatasetSummary, LintReport } from '@/api/types'

export function listDatasets(): Promise<DatasetSummary[]> {
  return request<DatasetSummary[]>('/api/semantic/datasets')
}

export function getDataset(name: string): Promise<DatasetDetail> {
  return request<DatasetDetail>(`/api/semantic/datasets/${encodeURIComponent(name)}`)
}

export function getLint(name: string): Promise<LintReport> {
  return request<LintReport>(`/api/semantic/datasets/${encodeURIComponent(name)}/lint`)
}

export function publishDataset(name: string): Promise<{ published: boolean }> {
  return request<{ published: boolean }>(
    `/api/semantic/datasets/${encodeURIComponent(name)}/publish`,
    { method: 'POST' },
  )
}