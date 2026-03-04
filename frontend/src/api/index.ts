import axios, { type AxiosInstance, type AxiosResponse } from 'axios'
import type {
  User,
  LoginParams,
  LoginResponse,
  Workspace,
  DataSource,
  DataSourceCreate,
  SchemaColumn,
  Dataset,
  DatasetCreate,
  QueryRequest,
  QueryHistory,
} from './types'

const baseURL = '/api/v1'

// 创建 axios 实例
const api: AxiosInstance = axios.create({
  baseURL,
  timeout: 60000,
})

// 请求拦截器 - 添加 token
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// 响应拦截器 - 处理错误
api.interceptors.response.use(
  (response) => response,
  (error) => {
    console.error('API Error:', error)
    if (error.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(error)
  }
)

// ── 认证 API ───────────────────────────────────────────────────────────────

export const authApi = {
  login: (data: LoginParams) =>
    api.post<any, AxiosResponse<LoginResponse>>('/auth/login', data),

  getMe: () => api.get<any, AxiosResponse<User>>('/auth/me'),

  getWorkspaces: () => api.get<any, AxiosResponse<Workspace[]>>('/auth/workspaces'),

  createWorkspace: (data: { name: string; description?: string }) =>
    api.post<any, AxiosResponse<Workspace>>('/auth/workspaces', data),
}

// ── 数据源 API ───────────────────────────────────────────────────────────

export const dataSourceApi = {
  create: (data: DataSourceCreate) =>
    api.post<any, AxiosResponse<DataSource>>('/data-sources', data),

  uploadCSV: (file: File, workspaceId: number = 1, dataSourceId?: number) => {
    const formData = new FormData()
    formData.append('file', file)
    return api.post<any, AxiosResponse<DataSource>>('/data-sources/upload-csv', formData, {
      params: { workspace_id: workspaceId, data_source_id: dataSourceId },
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  list: (workspaceId: number) =>
    api.get<any, AxiosResponse<DataSource[]>>('/data-sources', {
      params: { workspace_id: workspaceId },
    }),

  getSchema: (id: number, tableName?: string) =>
    api.get<any, AxiosResponse<SchemaColumn[]>>(`/data-sources/${id}/schema`, {
      params: { table_name: tableName },
    }),
}

// ── 数据集 API ───────────────────────────────────────────────────────────

export const datasetApi = {
  create: (data: DatasetCreate) =>
    api.post<any, AxiosResponse<Dataset>>('/datasets', data),

  list: (workspaceId: number, dataSourceId?: number) =>
    api.get<any, AxiosResponse<Dataset[]>>('/datasets', {
      params: { workspace_id: workspaceId, data_source_id: dataSourceId },
    }),

  update: (id: number, data: Partial<DatasetCreate>) =>
    api.patch<any, AxiosResponse<Dataset>>(`/datasets/${id}`, data),

  delete: (id: number) =>
    api.delete<any, AxiosResponse<void>>(`/datasets/${id}`),
}

// ── 查询 API ─────────────────────────────────────────────────────────────

export const queryApi = {
  // 流式执行
  streamExecute: (data: QueryRequest) => {
    const token = localStorage.getItem('token')
    console.log('[API] streamExecute called, token:', token ? token.substring(0, 20) + '...' : 'null')
    console.log('[API] Request data:', data)
    console.log('[API] Fetch URL:', '/api/v1/queries/stream')

    return fetch('/api/v1/queries/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify(data),
    }).then(response => {
      console.log('[API] Response status:', response.status)
      console.log('[API] Response ok:', response.ok)
      console.log('[API] Response type:', response.type)
      return response
    }).catch(err => {
      console.error('[API] Fetch error:', err)
      throw err
    })
  }
}

// ── 历史记录 API ─────────────────────────────────────────────────────────

export const historyApi = {
  list: (workspaceId: number, datasetId?: number, limit = 20, offset = 0) =>
    api.get<any, AxiosResponse<QueryHistory[]>>('/history', {
      params: { workspace_id: workspaceId, dataset_id: datasetId, limit, offset },
    }),
}

export default api
