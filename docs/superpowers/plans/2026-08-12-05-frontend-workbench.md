# 前端工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出 M-39 的三分栏工作台与配置侧页面，让用户看得见口径、改得动条件、查得到过程。

**Architecture:** 单个 Vue3 应用。服务端状态与视图状态分离：`api/` 只做 HTTP 与类型，`stores/` 持有会话与当前轮次状态，组件只渲染与派发。三分栏的每一栏是独立组件，条件面板直接绑定后端返回的 `slot_state`——这是 M-19 结构化上下文在界面上的同一份数据，不是前端另存一份。历史代码 3308 行的 `QueryPage.vue` 是反面样本，本轮任何单文件组件超过 300 行即视为需要拆分。

**Tech Stack:** Vue 3（`<script setup>` + TypeScript）、Vite、Pinia、Vue Router、Element Plus、Vitest + @vue/test-utils

## Global Constraints

以下约束来自 `docs/superpowers/specs/2026-08-12-trusted-query-loop-design.md`，每个任务的要求都隐含包含本节：

- 引证块默认展开，不折叠；「由数据权限自动附加」必须显式出现在界面上。
- 查询条件面板必须可见且可编辑，改完能直接重跑。
- 澄清以卡片形式出现在对话流，带可点选项，不让用户重新打字。
- 点踩必须弹归因分类，未选分类不能提交。
- 拒答与失败在界面上是正常的一轮，不是错误弹窗；仍可查看 Trace。
- 界面文案用中文；代码标识符、注释、组件名用英文。
- 配置侧页面朴素优先，使用者是管理员。
- 单个组件只负责一件事，超过 300 行即拆分。

## 前置

依赖计划 04 完成（`/api/chat/*` 与 `/api/trace/*` 可用），以及计划 01 的 `/api/semantic/*`。

---

### Task 1: 前端工程骨架与 API 层

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/chat.ts`
- Create: `frontend/src/api/trace.ts`
- Create: `frontend/src/api/semantic.ts`
- Create: `frontend/tests/api/client.spec.ts`

**Interfaces:**
- Produces:
  - `src/api/types.ts` — 与后端 Pydantic 模型一一对应的 TypeScript 类型
  - `src/api/client.ts` — `request<T>(path, init?)`，统一注入身份头与错误归一化
  - `src/api/chat.ts` — `ask`、`listConversations`、`listTurns`、`sendFeedback`
  - `src/api/trace.ts` — `getTrace`、`replayTurn`
  - `src/api/semantic.ts` — `listDatasets`、`getDataset`、`getLint`、`publishDataset`

类型手写而非生成：本轮接口面不大，手写能顺带作为契约检查——后端字段改了名，`tsc` 会立刻报错。

- [ ] **Step 1: 初始化工程**

```bash
cd /Users/fuyuxiang/Desktop/data-agent
npm create vite@latest frontend -- --template vue-ts
cd frontend
npm install
npm install element-plus pinia vue-router
npm install -D vitest @vue/test-utils jsdom @types/node
```

`frontend/package.json` 的 `scripts` 补上：

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "test": "vitest run",
    "test:watch": "vitest"
  }
}
```

`frontend/vite.config.ts`：

```ts
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
```

`frontend/tsconfig.json` 的 `compilerOptions` 需含：

```json
{
  "compilerOptions": {
    "strict": true,
    "types": ["vitest/globals"],
    "paths": { "@/*": ["./src/*"] }
  }
}
```

- [ ] **Step 2: 写失败的客户端测试**

`frontend/tests/api/client.spec.ts`：

```ts
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, request, setUsername } from '@/api/client'

function mockFetch(status: number, body: unknown) {
  const spy = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: async () => body,
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('request', () => {
  it('attaches the identity header', async () => {
    const spy = mockFetch(200, { ok: true })
    setUsername('admin')

    await request('/api/chat/conversations')

    const [, init] = spy.mock.calls[0]
    expect(init.headers['X-Username']).toBe('admin')
  })

  it('serialises the body as json', async () => {
    const spy = mockFetch(200, {})
    setUsername('admin')

    await request('/api/chat/ask', { method: 'POST', body: { question: '本月销售额' } })

    const [, init] = spy.mock.calls[0]
    expect(JSON.parse(init.body).question).toBe('本月销售额')
    expect(init.headers['Content-Type']).toBe('application/json')
  })

  it('raises ApiError carrying the status and detail', async () => {
    mockFetch(404, { detail: '数据集不存在' })

    await expect(request('/api/semantic/datasets/ghost')).rejects.toMatchObject({
      status: 404,
      message: '数据集不存在',
    })
  })

  it('keeps a readable message when the body is not json', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new Error('not json')
        },
      }),
    )

    await expect(request('/api/chat/ask')).rejects.toBeInstanceOf(ApiError)
  })

  it('surfaces a network failure as ApiError with status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed to fetch')))

    await expect(request('/api/chat/ask')).rejects.toMatchObject({ status: 0 })
  })

  it('validation errors from pydantic become a readable message', async () => {
    mockFetch(422, { detail: [{ loc: ['body', 'category'], msg: 'Value error, 负反馈必须归因' }] })

    await expect(request('/api/chat/turns/1/feedback')).rejects.toMatchObject({
      message: expect.stringContaining('负反馈必须归因'),
    })
  })
})
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，无法解析 `@/api/client`

- [ ] **Step 4: 写类型定义**

`frontend/src/api/types.ts`：

```ts
// Mirrors the backend Pydantic models. Renaming a field there must break tsc here.

export type TurnStatus = 'answered' | 'clarifying' | 'refused' | 'failed'

export interface CitationLine {
  label: string
  value: string
  /** 'permission' lines are appended by row-level policy, never typed by the user. */
  source: 'user' | 'permission'
}

export interface Citation {
  metric: string
  time: string
  filters: CitationLine[]
  data_updated_at: string
}

export interface DrillDown {
  label: string
  kind: string
  target: string
}

export interface Answer {
  headline: string
  conclusion: string
  assumptions: string[]
  warnings: string[]
  citation: Citation | null
  drill_downs: DrillDown[]
  columns: string[]
  rows: unknown[][]
}

export interface ClarifyOption {
  value: string
  label: string
  hint: string
}

export interface Clarification {
  kind: string
  target: string
  question: string
  options: ClarifyOption[]
}

export interface FilterSlot {
  field: string
  operator: string
  values: string[]
  spoken_values: string[]
}

export interface TimeSlot {
  start: string
  end: string
  grain: string
  expression: string
}

export interface SlotState {
  kind: string
  dataset: string
  metrics: string[]
  dimensions: string[]
  filters: FilterSlot[]
  time: TimeSlot | null
  comparison: string
  sort: { by: string; descending: boolean; limit: number | null } | null
  assumptions: string[]
}

export interface AskResponse {
  status: TurnStatus
  conversation_id: number
  turn_id: number
  answer: Answer | null
  clarifications: Clarification[]
  refusal_reason: string
  slot_state: SlotState | null
}

export interface Conversation {
  id: number
  title: string
  dataset_name: string
  updated_at: string
}

export interface Turn {
  id: number
  question: string
  status: TurnStatus
  answer: { headline: string; conclusion: string } | null
  created_at: string
}

export type FeedbackCategory = 'metric' | 'time' | 'sql' | 'calculation' | 'conclusion'

export interface TraceStage {
  stage: string
  sequence: number
  input_payload: Record<string, unknown> | null
  output_payload: Record<string, unknown> | null
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  elapsed_ms: number
  error: string
}

export interface Trace {
  turn_id: number
  question: string
  status: TurnStatus
  intent_snapshot: Record<string, unknown> | null
  stages: TraceStage[]
}

export interface Replay {
  sql: string
  display_sql: string
  matches_original: boolean
  applied_row_filters: string[]
  masked_field_names: string[]
}

export interface EnumValue {
  physical_value: string
  business_value: string
  aliases: string[]
  description: string
}

export interface Field {
  name: string
  business_name: string
  synonyms: string[]
  semantic_type: string
  unit: string
  display_format: string
  default_aggregation: string
  allowed_aggregations: string[]
  is_filterable: boolean
  is_groupable: boolean
  sensitivity: string
  enum_values: EnumValue[]
}

export interface Metric {
  name: string
  business_name: string
  version: number
  kind: string
  aggregation_behavior: string
  description: string
  unit: string
  display_format: string
}

export interface DatasetSummary {
  name: string
  business_name: string
  physical_table: string
  grain: string
  is_published: boolean
  updated_at: string | null
}

export interface DatasetDetail extends DatasetSummary {
  aliases: string[]
  description: string
  applicable_scenario: string
  forbidden_scenario: string
  fields: Field[]
  metrics: Metric[]
}

export interface LintIssue {
  severity: 'error' | 'warning'
  target: string
  message: string
}

export interface LintReport {
  dataset: string
  publishable: boolean
  issues: LintIssue[]
}
```

- [ ] **Step 5: 写客户端与接口函数**

`frontend/src/api/client.ts`：

```ts
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** Placeholder identity, matching the backend's X-Username header (spec 3.1). */
let username = ''

export function setUsername(value: string): void {
  username = value
}

export function getUsername(): string {
  return username
}

interface RequestInitLite {
  method?: string
  body?: unknown
}

function readDetail(payload: unknown): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item as { msg?: string }).msg ?? '')
        .filter(Boolean)
        .join('；')
    }
    if (detail && typeof detail === 'object' && 'message' in detail) {
      return String((detail as { message: unknown }).message)
    }
  }
  return ''
}

export async function request<T>(path: string, init: RequestInitLite = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      method: init.method ?? 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Username': username },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    })
  } catch {
    throw new ApiError('无法连接服务，请检查后端是否启动', 0)
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw new ApiError(readDetail(payload) || `请求失败（${response.status}）`, response.status)
  }
  return payload as T
}
```

`frontend/src/api/chat.ts`：

```ts
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
  return request<void>(`/api/chat/turns/${turnId}/feedback`, { method: 'POST', body: payload })
}
```

`frontend/src/api/trace.ts`：

```ts
import { request } from '@/api/client'
import type { Replay, Trace } from '@/api/types'

export function getTrace(turnId: number): Promise<Trace> {
  return request<Trace>(`/api/trace/turns/${turnId}`)
}

export function replayTurn(turnId: number): Promise<Replay> {
  return request<Replay>(`/api/trace/turns/${turnId}/replay`, { method: 'POST' })
}
```

`frontend/src/api/semantic.ts`：

```ts
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
```

- [ ] **Step 6: 写入口与路由**

`frontend/src/main.ts`：

```ts
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'

import App from '@/App.vue'
import { setUsername } from '@/api/client'
import router from '@/router'

// Placeholder identity until real login exists; overridable for local testing.
setUsername(localStorage.getItem('username') ?? 'admin')

createApp(App).use(createPinia()).use(router).use(ElementPlus).mount('#app')
```

`frontend/src/router/index.ts`：

```ts
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/ask' },
    { path: '/ask', name: 'ask', component: () => import('@/views/WorkbenchView.vue') },
    { path: '/trace/:turnId', name: 'trace', component: () => import('@/views/TraceView.vue') },
    { path: '/admin/datasets', name: 'datasets', component: () => import('@/views/DatasetsView.vue') },
    {
      path: '/admin/datasets/:name',
      name: 'dataset-detail',
      component: () => import('@/views/DatasetDetailView.vue'),
    },
  ],
})

export default router
```

`frontend/src/App.vue`：

```vue
<script setup lang="ts"></script>

<template>
  <router-view />
</template>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
}
</style>
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（6 项）

路由指向的四个视图此时尚不存在，`npm run build` 会失败——这是预期的，Task 2~6 逐个补齐。测试不加载路由，因此 `npm test` 可以先绿。

- [ ] **Step 8: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/tsconfig.json frontend/index.html frontend/src frontend/tests
git commit -F - <<'EOF'
搭建前端工程骨架与接口层

后端接口已可用但没有任何前端工程，且历史代码把类型、请求、页面混在超大单文件里，字段改名只能靠运行时报错发现。

- 建立 Vite + Vue3 + TypeScript 工程，配置 /api 代理与路径别名
- 接口类型手写并与后端模型逐字段对应，后端改名即触发类型检查失败
- 统一请求函数注入身份头，并把网络失败、非 JSON 响应、校验错误归一为同一种错误类型
- 按 chat/trace/semantic 三组拆分接口函数，不做跨组复用
- 验证：vitest 客户端 6 项通过
EOF
```

---

### Task 2: 会话状态 Store

**Files:**
- Create: `frontend/src/stores/session.ts`
- Create: `frontend/tests/stores/session.spec.ts`

**Interfaces:**
- Consumes: `@/api/chat`
- Produces:
  - `useSessionStore()` — state `conversations`/`activeConversationId`/`messages`/`slotState`/`asking`/`error`；actions `loadConversations`、`openConversation`、`startNew`、`submit`、`answerClarification`、`rerunWithSlots`
  - `type ChatMessage` — `{ id, role: 'user' | 'agent', kind: 'answer' | 'clarify' | 'refusal' | 'failure', turnId, question?, answer?, clarifications?, reason? }`

Store 只持有服务端状态与派生视图数据。**它不判断权限、不拼 SQL、不改口径**——这些都在后端，前端只显示。

- [ ] **Step 1: 写失败的 store 测试**

`frontend/tests/stores/session.spec.ts`：

```ts
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
      answered({ status: 'refused', answer: null, refusal_reason: '你没有该数据的访问权限', slot_state: null }),
    )
    await store.submit('总成本')

    expect(store.slotState?.metrics).toEqual(['sales_revenue'])
  })

  it('renders a refusal as a normal message, not an error', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({ status: 'refused', answer: null, refusal_reason: '你没有该数据的访问权限' }),
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
      }),
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
      { kind: 'metric', target: 'sales_revenue', question: '你指的是哪个销售额？', options: [] },
      { value: 'sales_revenue', label: '含税订单金额', hint: '' },
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
    let release: (value: AskResponse) => void = () => {}
    vi.spyOn(chatApi, 'ask').mockReturnValue(
      new Promise<AskResponse>((resolve) => {
        release = resolve
      }),
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，无法解析 `@/stores/session`

- [ ] **Step 3: 写 store**

`frontend/src/stores/session.ts`：

```ts
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

/** Slot edits are expressed as a sentence so the whole pipeline runs again,
 * including recognition — this keeps one code path instead of a second,
 * slot-only entry point that would skip validation. */
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
    messages.value.push({ id: nextId(), role: 'user', kind: 'question', turnId: 0, question: text })
    asking.value = true
    try {
      apply(
        await ask({
          question: text,
          dataset_name: DEFAULT_DATASET,
          conversation_id: activeConversationId.value,
        }),
      )
    } catch (raised) {
      error.value = raised instanceof ApiError ? raised.message : '请求失败'
    } finally {
      asking.value = false
    }
  }

  async function answerClarification(
    request: Clarification,
    option: ClarifyOption,
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
    messages.value = turns.flatMap((turn) => [
      { id: nextId(), role: 'user', kind: 'question', turnId: turn.id, question: turn.question },
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
      } as ChatMessage,
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
```

历史轮次回放只带 `headline` 与 `conclusion`（后端 `TurnRow.answer` 只存这两项），所以引证块在打开旧会话时不显示。要看完整引证走 Trace 页。这是有意的取舍：把完整答案存进 `turns.answer` 会让引证有两份真相，Trace 才是权威来源。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（客户端 6 项 + store 15 项）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/stores/session.ts frontend/tests/stores/session.spec.ts
git commit -F - <<'EOF'
建立会话状态 Store

对话区、条件面板、会话列表要读同一份状态，若各组件自行请求并各存一份，条件面板会与后端槽位不一致，用户改完条件重跑得到的口径就不可预期。

- 会话、消息、槽位、请求中标志与错误集中在单个 store
- 拒答与澄清作为正常消息进入对话流，仅传输失败才落到 error
- 槽位以后端返回为准，某轮未返回时保留上一轮，不在前端自行推导
- 改条件重跑走同一条提问路径，避免出现一条跳过意图校验的旁路
- 验证：vitest store 15 项通过
EOF
```

---

### Task 3: 引证块、澄清卡片与反馈

**Files:**
- Create: `frontend/src/components/chat/CitationBlock.vue`
- Create: `frontend/src/components/chat/ClarifyCard.vue`
- Create: `frontend/src/components/chat/FeedbackBar.vue`
- Create: `frontend/src/components/chat/AnswerCard.vue`
- Create: `frontend/tests/components/CitationBlock.spec.ts`
- Create: `frontend/tests/components/ClarifyCard.spec.ts`
- Create: `frontend/tests/components/FeedbackBar.spec.ts`
- Create: `frontend/tests/components/AnswerCard.spec.ts`

**Interfaces:**
- Consumes: `@/api/types`、`@/api/chat`
- Produces:
  - `CitationBlock` — props `citation: Citation`；无折叠开关
  - `ClarifyCard` — props `clarifications: Clarification[]`；emit `choose(request, option)`
  - `FeedbackBar` — props `turnId: number`；内部管理归因弹窗
  - `AnswerCard` — props `message: ChatMessage`；组合上面三者与结论、假设、警告

这四个组件是本轮信任度的界面载体，测试重点不是样式而是**必须出现的信息不会被隐藏**。

- [ ] **Step 1: 写失败的引证块测试**

`frontend/tests/components/CitationBlock.spec.ts`：

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import CitationBlock from '@/components/chat/CitationBlock.vue'
import type { Citation } from '@/api/types'

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
    time: '2026-08-01 ~ 2026-08-12（按完成日期）',
    filters: [
      { label: '大区', value: '属于 华东', source: 'user' },
      { label: '大区', value: '属于 华东', source: 'permission' },
    ],
    data_updated_at: '2026-08-12 09:00',
    ...overrides,
  }
}

describe('CitationBlock', () => {
  it('shows the metric caliber including its version', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('sales_revenue v3')
  })

  it('shows the time range and which date field it uses', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('按完成日期')
  })

  it('labels permission-appended filters explicitly', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('由数据权限自动附加')
  })

  it('does not label user-typed filters as permission-appended', () => {
    const wrapper = mount(CitationBlock, {
      props: { citation: citation({ filters: [{ label: '大区', value: '属于 华东', source: 'user' }] }) },
    })
    expect(wrapper.text()).not.toContain('由数据权限自动附加')
  })

  it('shows the data freshness', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.text()).toContain('2026-08-12 09:00')
  })

  it('has no collapse toggle: the block is always expanded', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation() } })
    expect(wrapper.find('.el-collapse').exists()).toBe(false)
    expect(wrapper.findAll('details')).toHaveLength(0)
  })

  it('omits the filter row when there is none', () => {
    const wrapper = mount(CitationBlock, { props: { citation: citation({ filters: [] }) } })
    expect(wrapper.find('[data-test="citation-filters"]').exists()).toBe(false)
  })

  it('omits the freshness row when the backend has none', () => {
    const wrapper = mount(CitationBlock, {
      props: { citation: citation({ data_updated_at: '' }) },
    })
    expect(wrapper.find('[data-test="citation-updated"]').exists()).toBe(false)
  })
})
```

- [ ] **Step 2: 写失败的澄清卡片测试**

`frontend/tests/components/ClarifyCard.spec.ts`：

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import type { Clarification } from '@/api/types'

const request: Clarification = {
  kind: 'metric',
  target: 'sales_revenue',
  question: '你指的是哪个销售额？',
  options: [
    { value: 'sales_revenue', label: '含税订单金额', hint: '已完成订单' },
    { value: 'net_revenue', label: '财务确认收入', hint: '' },
  ],
}

describe('ClarifyCard', () => {
  it('shows the question', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.text()).toContain('你指的是哪个销售额？')
  })

  it('renders every option as a clickable button', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.findAll('[data-test="clarify-option"]')).toHaveLength(2)
  })

  it('shows the option hint so the user can tell them apart', () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })
    expect(wrapper.text()).toContain('已完成订单')
  })

  it('emits choose with the request and the picked option', async () => {
    const wrapper = mount(ClarifyCard, { props: { clarifications: [request] } })

    await wrapper.findAll('[data-test="clarify-option"]')[1].trigger('click')

    expect(wrapper.emitted('choose')?.[0]).toEqual([request, request.options[1]])
  })

  it('renders several clarifications at once', () => {
    const wrapper = mount(ClarifyCard, {
      props: {
        clarifications: [request, { ...request, target: 'time', question: '哪个时间范围？' }],
      },
    })
    expect(wrapper.text()).toContain('哪个时间范围？')
  })

  it('falls back to a text hint when there are no options', () => {
    const wrapper = mount(ClarifyCard, {
      props: { clarifications: [{ ...request, options: [] }] },
    })
    expect(wrapper.text()).toContain('请补充说明')
  })
})
```

- [ ] **Step 3: 写失败的反馈栏测试**

`frontend/tests/components/FeedbackBar.spec.ts`：

```ts
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

    expect(wrapper.vm.categories.map((item: { value: string }) => item.value)).toEqual([
      'metric',
      'time',
      'sql',
      'calculation',
      'conclusion',
    ])
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

    expect(wrapper.find('[data-test="thumb-up"]').attributes('disabled')).toBeDefined()
  })

  it('keeps the dialog open and shows the reason when the server rejects', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(chatApi, 'sendFeedback').mockRejectedValue(new ApiError('负反馈必须归因', 422))
    const wrapper = mount(FeedbackBar, { props: { turnId: 11 } })
    await wrapper.find('[data-test="thumb-down"]').trigger('click')

    wrapper.vm.category = 'metric'
    await wrapper.vm.confirm()

    expect(wrapper.vm.dialogVisible).toBe(true)
    expect(wrapper.vm.hint).toContain('负反馈必须归因')
  })
})
```

- [ ] **Step 4: 写失败的答案卡测试**

`frontend/tests/components/AnswerCard.spec.ts`：

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AnswerCard from '@/components/chat/AnswerCard.vue'
import type { ChatMessage } from '@/stores/session'

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    role: 'agent',
    kind: 'answer',
    turnId: 11,
    answer: {
      headline: '华东本月销售额 4,235 万',
      conclusion: '环比 +12.4%',
      assumptions: [],
      warnings: [],
      citation: {
        metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
        time: '2026-08-01 ~ 2026-08-12（按完成日期）',
        filters: [],
        data_updated_at: '2026-08-12 09:00',
      },
      drill_downs: [],
      columns: ['sales_revenue'],
      rows: [[42350000]],
    },
    ...overrides,
  }
}

describe('AnswerCard', () => {
  it('shows the headline and the conclusion', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.text()).toContain('4,235 万')
    expect(wrapper.text()).toContain('环比 +12.4%')
  })

  it('always renders the citation block for an answer', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.find('[data-test="citation"]').exists()).toBe(true)
  })

  it('shows assumptions prominently, not as a footnote', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: { ...message().answer!, assumptions: ['指标口径未确认，已默认按「销售额」处理'] },
        }),
      },
    })
    expect(wrapper.find('[data-test="assumptions"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('已默认按')
  })

  it('shows validation warnings', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({ answer: { ...message().answer!, warnings: ['结果超过上限已截断'] } }),
      },
    })
    expect(wrapper.text()).toContain('已截断')
  })

  it('renders the feedback bar for an answer', () => {
    const wrapper = mount(AnswerCard, { props: { message: message() } })
    expect(wrapper.find('[data-test="feedback"]').exists()).toBe(true)
  })

  it('a refusal shows the reason and no citation', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          kind: 'refusal',
          answer: undefined,
          reason: '你没有该数据的访问权限',
        }),
      },
    })
    expect(wrapper.text()).toContain('你没有该数据的访问权限')
    expect(wrapper.find('[data-test="citation"]').exists()).toBe(false)
  })

  it('a refusal still offers the trace link so it stays diagnosable', () => {
    const wrapper = mount(AnswerCard, {
      props: { message: message({ kind: 'refusal', answer: undefined, reason: '超出范围' }) },
    })
    expect(wrapper.find('[data-test="trace-link"]').exists()).toBe(true)
  })

  it('a refusal offers no feedback bar', () => {
    const wrapper = mount(AnswerCard, {
      props: { message: message({ kind: 'refusal', answer: undefined, reason: '超出范围' }) },
    })
    expect(wrapper.find('[data-test="feedback"]').exists()).toBe(false)
  })

  it('renders drill-down suggestions when present', () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: {
            ...message().answer!,
            drill_downs: [{ label: '按省份看', kind: 'dimension', target: 'province' }],
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('按省份看')
  })

  it('emits drill-down with the target', async () => {
    const wrapper = mount(AnswerCard, {
      props: {
        message: message({
          answer: {
            ...message().answer!,
            drill_downs: [{ label: '按省份看', kind: 'dimension', target: 'province' }],
          },
        }),
      },
    })

    await wrapper.find('[data-test="drill-down"]').trigger('click')

    expect(wrapper.emitted('drill')?.[0]).toEqual([
      { label: '按省份看', kind: 'dimension', target: 'province' },
    ])
  })
})
```

- [ ] **Step 5: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，四个组件均不存在

- [ ] **Step 6: 写引证块**

`frontend/src/components/chat/CitationBlock.vue`：

```vue
<script setup lang="ts">
import type { Citation } from '@/api/types'

// Always expanded by design (spec 6): a number whose caliber is hidden is a
// number nobody trusts. There is deliberately no collapse prop.
defineProps<{ citation: Citation }>()
</script>

<template>
  <div class="citation" data-test="citation">
    <div class="citation__title">引证</div>
    <dl>
      <div class="citation__row">
        <dt>口径</dt>
        <dd>{{ citation.metric }}</dd>
      </div>
      <div class="citation__row">
        <dt>时间</dt>
        <dd>{{ citation.time }}</dd>
      </div>
      <div v-if="citation.filters.length" class="citation__row" data-test="citation-filters">
        <dt>过滤</dt>
        <dd>
          <div v-for="(line, index) in citation.filters" :key="index" class="citation__filter">
            <span>{{ line.label }} {{ line.value }}</span>
            <span v-if="line.source === 'permission'" class="citation__permission">
              ← 由数据权限自动附加
            </span>
          </div>
        </dd>
      </div>
      <div v-if="citation.data_updated_at" class="citation__row" data-test="citation-updated">
        <dt>更新</dt>
        <dd>{{ citation.data_updated_at }}</dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.citation {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color);
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.citation__title {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
}

.citation__row {
  display: flex;
  gap: 8px;
  margin: 2px 0;
}

.citation__row dt {
  flex: none;
  width: 32px;
  color: var(--el-text-color-secondary);
}

.citation__row dd {
  margin: 0;
}

.citation__permission {
  margin-left: 8px;
  color: var(--el-color-warning);
}
</style>
```

- [ ] **Step 7: 写澄清卡片**

`frontend/src/components/chat/ClarifyCard.vue`：

```vue
<script setup lang="ts">
import type { Clarification, ClarifyOption } from '@/api/types'

defineProps<{ clarifications: Clarification[] }>()
const emit = defineEmits<{ choose: [Clarification, ClarifyOption] }>()
</script>

<template>
  <div class="clarify">
    <div v-for="request in clarifications" :key="request.target" class="clarify__item">
      <div class="clarify__question">{{ request.question }}</div>
      <div v-if="request.options.length" class="clarify__options">
        <el-button
          v-for="option in request.options"
          :key="option.value"
          data-test="clarify-option"
          size="small"
          @click="emit('choose', request, option)"
        >
          {{ option.label }}
          <span v-if="option.hint" class="clarify__hint">（{{ option.hint }}）</span>
        </el-button>
      </div>
      <div v-else class="clarify__hint">请补充说明后重新提问</div>
    </div>
  </div>
</template>

<style scoped>
.clarify__item + .clarify__item {
  margin-top: 12px;
}

.clarify__question {
  margin-bottom: 8px;
}

.clarify__options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.clarify__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
```

- [ ] **Step 8: 写反馈栏**

`frontend/src/components/chat/FeedbackBar.vue`：

```vue
<script setup lang="ts">
import { ref } from 'vue'

import { sendFeedback } from '@/api/chat'
import { ApiError } from '@/api/client'
import type { FeedbackCategory } from '@/api/types'

const props = defineProps<{ turnId: number }>()

// M-38: a thumbs-down without attribution is unusable as eval data, so the
// dialog is mandatory rather than an optional comment box.
const categories: { value: FeedbackCategory; label: string }[] = [
  { value: 'metric', label: '指标错' },
  { value: 'time', label: '时间错' },
  { value: 'sql', label: 'SQL 错' },
  { value: 'calculation', label: '计算错' },
  { value: 'conclusion', label: '结论错' },
]

const dialogVisible = ref(false)
const submitted = ref(false)
const category = ref<FeedbackCategory | ''>('')
const comment = ref('')
const hint = ref('')

async function thumbUp(): Promise<void> {
  try {
    await sendFeedback(props.turnId, { is_positive: true })
    submitted.value = true
  } catch (raised) {
    hint.value = raised instanceof ApiError ? raised.message : '提交失败'
  }
}

function thumbDown(): void {
  hint.value = ''
  dialogVisible.value = true
}

async function confirm(): Promise<void> {
  if (!category.value) {
    hint.value = '请选择一个归因分类'
    return
  }
  try {
    await sendFeedback(props.turnId, {
      is_positive: false,
      category: category.value,
      comment: comment.value,
    })
    submitted.value = true
    dialogVisible.value = false
  } catch (raised) {
    hint.value = raised instanceof ApiError ? raised.message : '提交失败'
  }
}

defineExpose({ dialogVisible, categories, category, comment, hint, confirm })
</script>

<template>
  <div class="feedback" data-test="feedback">
    <el-button
      data-test="thumb-up"
      text
      size="small"
      :disabled="submitted"
      @click="thumbUp"
    >
      👍
    </el-button>
    <el-button
      data-test="thumb-down"
      text
      size="small"
      :disabled="submitted"
      @click="thumbDown"
    >
      👎
    </el-button>
    <span v-if="submitted" class="feedback__done">已记录，谢谢</span>

    <el-dialog v-model="dialogVisible" title="哪里不对？" width="420px">
      <el-radio-group v-model="category">
        <el-radio v-for="item in categories" :key="item.value" :value="item.value">
          {{ item.label }}
        </el-radio>
      </el-radio-group>
      <el-input
        v-model="comment"
        class="feedback__comment"
        type="textarea"
        :rows="3"
        placeholder="补充说明（选填）"
      />
      <div v-if="hint" class="feedback__hint">{{ hint }}</div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirm">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.feedback {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
}

.feedback__done,
.feedback__hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.feedback__hint {
  margin-top: 8px;
  color: var(--el-color-danger);
}

.feedback__comment {
  margin-top: 12px;
}
</style>
```

`defineExpose` 把弹窗状态暴露给测试，避免为了断言去 mock Element Plus 的弹层渲染。

- [ ] **Step 9: 写答案卡**

`frontend/src/components/chat/AnswerCard.vue`：

```vue
<script setup lang="ts">
import { computed } from 'vue'

import CitationBlock from '@/components/chat/CitationBlock.vue'
import FeedbackBar from '@/components/chat/FeedbackBar.vue'
import type { DrillDown } from '@/api/types'
import type { ChatMessage } from '@/stores/session'

const props = defineProps<{ message: ChatMessage }>()
const emit = defineEmits<{ drill: [DrillDown] }>()

const answer = computed(() => props.message.answer)
const isAnswer = computed(() => props.message.kind === 'answer' && Boolean(answer.value))
</script>

<template>
  <div class="answer" :class="`answer--${message.kind}`">
    <template v-if="isAnswer">
      <div class="answer__headline">{{ answer!.headline }}</div>
      <div v-if="answer!.conclusion" class="answer__conclusion">{{ answer!.conclusion }}</div>

      <el-alert
        v-if="answer!.assumptions.length"
        class="answer__alert"
        data-test="assumptions"
        type="warning"
        :closable="false"
        title="本次作答使用了默认假设"
      >
        <ul>
          <li v-for="(item, index) in answer!.assumptions" :key="index">{{ item }}</li>
        </ul>
      </el-alert>

      <el-alert
        v-if="answer!.warnings.length"
        class="answer__alert"
        data-test="warnings"
        type="info"
        :closable="false"
        title="结果提示"
      >
        <ul>
          <li v-for="(item, index) in answer!.warnings" :key="index">{{ item }}</li>
        </ul>
      </el-alert>

      <CitationBlock v-if="answer!.citation" :citation="answer!.citation" />

      <div v-if="answer!.drill_downs.length" class="answer__drills">
        <el-button
          v-for="item in answer!.drill_downs"
          :key="item.target"
          data-test="drill-down"
          size="small"
          text
          @click="emit('drill', item)"
        >
          {{ item.label }}
        </el-button>
      </div>

      <FeedbackBar :turn-id="message.turnId" />
    </template>

    <template v-else>
      <div class="answer__reason">{{ message.reason }}</div>
      <router-link
        v-if="message.turnId"
        class="answer__trace"
        data-test="trace-link"
        :to="{ name: 'trace', params: { turnId: message.turnId } }"
      >
        查看 Trace
      </router-link>
    </template>
  </div>
</template>

<style scoped>
.answer__headline {
  font-size: 18px;
  font-weight: 600;
}

.answer__conclusion {
  margin-top: 4px;
  color: var(--el-text-color-regular);
}

.answer__alert {
  margin-top: 12px;
}

.answer__alert ul {
  margin: 0;
  padding-left: 18px;
}

.answer__drills {
  margin-top: 12px;
}

.answer--refusal .answer__reason,
.answer--failure .answer__reason {
  color: var(--el-text-color-primary);
}

.answer__trace {
  display: inline-block;
  margin-top: 8px;
  font-size: 12px;
}
</style>
```

`AnswerCard` 里的 `router-link` 需要路由上下文，测试中用 stub 处理：在 `frontend/tests/setup.ts` 里注册全局 stub，并在 `vite.config.ts` 的 `test` 段加 `setupFiles: ['./tests/setup.ts']`。

`frontend/tests/setup.ts`：

```ts
import { config } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { h } from 'vue'

config.global.plugins = [ElementPlus, createPinia()]
config.global.stubs = {
  RouterLink: { props: ['to'], render() { return h('a', this.$slots.default?.()) } },
}
```

- [ ] **Step 10: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（引证 8 项 + 澄清 6 项 + 反馈 7 项 + 答案卡 10 项）

- [ ] **Step 11: 提交**

```bash
git add frontend/src/components/chat frontend/tests/components frontend/tests/setup.ts frontend/vite.config.ts
git commit -F - <<'EOF'
实现引证块、澄清卡片与反馈归因组件

数字本身不构成可信，用户需要同时看到口径、时间字段、过滤来源与数据新鲜度；若引证可折叠或权限附加的过滤不标注来源，用户会拿权限内的局部数据下全局结论。

- 引证块无折叠开关，权限自动附加的过滤条件单独标注来源
- 澄清渲染为可点选项卡片，无选项时退化为文字提示
- 点踩强制弹归因弹窗，五类分类未选不允许提交
- 默认假设与结果告警以显著样式呈现，不做脚注
- 拒答与失败同样是一条正常消息并保留 Trace 入口
- 验证：vitest 组件 31 项通过
EOF
```

---

### Task 4: 查询条件面板

**Files:**
- Create: `frontend/src/components/workbench/ConditionPanel.vue`
- Create: `frontend/src/components/workbench/ResultTable.vue`
- Create: `frontend/tests/components/ConditionPanel.spec.ts`
- Create: `frontend/tests/components/ResultTable.spec.ts`

**Interfaces:**
- Consumes: `@/api/types`、`@/api/semantic`
- Produces:
  - `ConditionPanel` — props `slotState: SlotState | null`、`dataset: DatasetDetail | null`、`running: boolean`；emit `rerun(slots)`
  - `ResultTable` — props `columns: string[]`、`rows: unknown[][]`；内含导出 CSV

这是 M-19 在界面上的落地：面板绑定的就是后端返回的槽位，用户改完点重跑，等价于把修改后的槽位再问一遍。**面板里的可选项来自数据集语义，不是硬编码**——不可分组的字段不出现在维度下拉里，不可过滤的字段不出现在过滤里。

- [ ] **Step 1: 写失败的条件面板测试**

`frontend/tests/components/ConditionPanel.spec.ts`：

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConditionPanel from '@/components/workbench/ConditionPanel.vue'
import type { DatasetDetail, SlotState } from '@/api/types'

function dataset(): DatasetDetail {
  return {
    name: 'orders',
    business_name: '订单',
    is_published: true,
    physical_table: 'sample.orders',
    description: '',
    grain: '一行一个订单',
    applicable_scenario: '',
    forbidden_scenario: '',
    fields: [
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
          { physical_value: 'EC', business_value: '华东', aliases: [], description: '' },
          { physical_value: 'SC', business_value: '华南', aliases: [], description: '' },
        ],
      },
      {
        name: 'province',
        business_name: '省份',
        synonyms: [],
        semantic_type: 'dimension',
        unit: '',
        display_format: '',
        default_aggregation: 'none',
        allowed_aggregations: [],
        is_filterable: true,
        is_groupable: true,
        sensitivity: 'public',
        enum_values: [],
      },
      {
        name: 'amount',
        business_name: '订单金额',
        synonyms: [],
        semantic_type: 'measure',
        unit: '元',
        display_format: '',
        default_aggregation: 'sum',
        allowed_aggregations: ['sum', 'avg'],
        is_filterable: true,
        is_groupable: false,
        sensitivity: 'internal',
        enum_values: [],
      },
    ],
    metrics: [
      {
        name: 'sales_revenue',
        business_name: '销售额',
        version: 3,
        kind: 'simple',
        aggregation_behavior: 'additive',
        description: '已完成订单含税金额',
        unit: '元',
        display_format: '',
      },
      {
        name: 'order_count',
        business_name: '订单数',
        version: 1,
        kind: 'simple',
        aggregation_behavior: 'additive',
        description: '',
        unit: '单',
        display_format: '',
      },
    ],
  }
}

function slots(overrides: Partial<SlotState> = {}): SlotState {
  return {
    kind: 'aggregate',
    dataset: 'orders',
    metrics: ['sales_revenue'],
    dimensions: [],
    filters: [
      { field: 'region_code', operator: 'in', values: ['EC'], spoken_values: ['华东'] },
    ],
    time: { start: '2026-08-01', end: '2026-08-31', grain: 'month', expression: '本月' },
    comparison: 'mom',
    sort: null,
    assumptions: [],
    ...overrides,
  }
}

describe('ConditionPanel', () => {
  it('shows the current metric, time, dimension and filter slots', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    const text = wrapper.text()
    expect(text).toContain('指标')
    expect(text).toContain('时间')
    expect(text).toContain('维度')
    expect(text).toContain('过滤')
  })

  it('offers only groupable fields as dimensions', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.dimensionOptions.map((item: { value: string }) => item.value)).toEqual([
      'region_code',
      'province',
    ])
  })

  it('offers only filterable fields as filters', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.filterFieldOptions).toHaveLength(3)
  })

  it('offers enum values by their business value, never the physical code', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    const options = wrapper.vm.enumOptions('region_code')
    expect(options.map((item: { label: string }) => item.label)).toEqual(['华东', '华南'])
  })

  it('shows spoken values for the current filter, not physical codes', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.text()).toContain('华东')
    expect(wrapper.text()).not.toContain("'EC'")
  })

  it('emits rerun with the edited slots', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })

    wrapper.vm.draft.dimensions = ['province']
    await wrapper.vm.$nextTick()
    await wrapper.find('[data-test="rerun"]').trigger('click')

    expect(wrapper.emitted('rerun')?.[0][0]).toMatchObject({ dimensions: ['province'] })
  })

  it('resyncs the draft when a new turn returns new slots', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })

    await wrapper.setProps({ slotState: slots({ dimensions: ['province'] }) })

    expect(wrapper.vm.draft.dimensions).toEqual(['province'])
  })

  it('marks itself dirty once the user edits something', async () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.dirty).toBe(false)

    wrapper.vm.draft.comparison = 'yoy'
    await wrapper.vm.$nextTick()

    expect(wrapper.vm.dirty).toBe(true)
  })

  it('disables rerun while a request is in flight', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: true },
    })
    expect(wrapper.find('[data-test="rerun"]').attributes('disabled')).toBeDefined()
  })

  it('shows an empty hint before the first turn', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: null, dataset: dataset(), running: false },
    })
    expect(wrapper.text()).toContain('提问后')
  })

  it('does not offer a metric the dataset never published', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.metricOptions.map((item: { value: string }) => item.value)).toEqual([
      'sales_revenue',
      'order_count',
    ])
  })

  it('labels metrics with their version so the caliber is unambiguous', () => {
    const wrapper = mount(ConditionPanel, {
      props: { slotState: slots(), dataset: dataset(), running: false },
    })
    expect(wrapper.vm.metricOptions[0].label).toContain('v3')
  })
})
```

- [ ] **Step 2: 写失败的结果表测试**

`frontend/tests/components/ResultTable.spec.ts`：

```ts
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
    expect(wrapper.vm.tableRows[0]).toEqual({ province: '江苏', sales_revenue: 18200000 })
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，两个组件不存在

- [ ] **Step 4: 写条件面板**

`frontend/src/components/workbench/ConditionPanel.vue`：

```vue
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import type { DatasetDetail, SlotState } from '@/api/types'

const props = defineProps<{
  slotState: SlotState | null
  dataset: DatasetDetail | null
  running: boolean
}>()
const emit = defineEmits<{ rerun: [SlotState] }>()

const COMPARISONS = [
  { value: 'none', label: '不对比' },
  { value: 'mom', label: '环比' },
  { value: 'yoy', label: '同比' },
  { value: 'ytd', label: '年累计' },
  { value: 'mtd', label: '月累计' },
]

function blank(): SlotState {
  return {
    kind: 'aggregate',
    dataset: '',
    metrics: [],
    dimensions: [],
    filters: [],
    time: null,
    comparison: 'none',
    sort: null,
    assumptions: [],
  }
}

const draft = reactive<SlotState>(structuredClone(props.slotState ?? blank()))
const dirty = ref(false)
let syncing = false

// A new turn is authoritative: the panel mirrors the backend slots rather than
// holding a divergent local copy (M-19).
watch(
  () => props.slotState,
  (value) => {
    syncing = true
    Object.assign(draft, structuredClone(value ?? blank()))
    dirty.value = false
    syncing = false
  },
  { deep: true },
)

watch(
  draft,
  () => {
    if (!syncing) dirty.value = true
  },
  { deep: true },
)

const metricOptions = computed(() =>
  (props.dataset?.metrics ?? []).map((metric) => ({
    value: metric.name,
    label: `${metric.business_name} v${metric.version}`,
    hint: metric.description,
  })),
)

const dimensionOptions = computed(() =>
  (props.dataset?.fields ?? [])
    .filter((field) => field.is_groupable)
    .map((field) => ({ value: field.name, label: field.business_name })),
)

const filterFieldOptions = computed(() =>
  (props.dataset?.fields ?? [])
    .filter((field) => field.is_filterable)
    .map((field) => ({ value: field.name, label: field.business_name })),
)

function enumOptions(fieldName: string) {
  const field = props.dataset?.fields.find((item) => item.name === fieldName)
  // Only business values are ever offered; physical codes stay server-side.
  return (field?.enum_values ?? []).map((item) => ({
    value: item.business_value,
    label: item.business_value,
  }))
}

function addFilter(): void {
  draft.filters.push({ field: '', operator: 'in', values: [], spoken_values: [] })
}

function removeFilter(index: number): void {
  draft.filters.splice(index, 1)
}

function rerun(): void {
  emit('rerun', structuredClone(draft))
}

defineExpose({
  draft,
  dirty,
  metricOptions,
  dimensionOptions,
  filterFieldOptions,
  enumOptions,
})
</script>

<template>
  <div class="panel">
    <div class="panel__title">当前查询条件</div>

    <div v-if="!slotState" class="panel__empty">提问后这里会显示可编辑的查询条件</div>

    <template v-else>
      <div class="panel__row">
        <label>指标</label>
        <el-select v-model="draft.metrics" multiple size="small" placeholder="选择指标">
          <el-option
            v-for="item in metricOptions"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row">
        <label>时间</label>
        <el-date-picker
          v-if="draft.time"
          v-model="timeRange"
          type="daterange"
          size="small"
          value-format="YYYY-MM-DD"
          start-placeholder="开始"
          end-placeholder="结束"
        />
      </div>

      <div class="panel__row">
        <label>对比</label>
        <el-select v-model="draft.comparison" size="small">
          <el-option
            v-for="item in COMPARISONS"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row">
        <label>维度</label>
        <el-select v-model="draft.dimensions" multiple size="small" placeholder="不分组">
          <el-option
            v-for="item in dimensionOptions"
            :key="item.value"
            :value="item.value"
            :label="item.label"
          />
        </el-select>
      </div>

      <div class="panel__row panel__row--stack">
        <label>过滤</label>
        <div
          v-for="(filter, index) in draft.filters"
          :key="index"
          class="panel__filter"
        >
          <el-select v-model="filter.field" size="small" placeholder="字段">
            <el-option
              v-for="item in filterFieldOptions"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            />
          </el-select>
          <el-select
            v-if="enumOptions(filter.field).length"
            v-model="filter.spoken_values"
            multiple
            size="small"
            placeholder="取值"
          >
            <el-option
              v-for="item in enumOptions(filter.field)"
              :key="item.value"
              :value="item.value"
              :label="item.label"
            />
          </el-select>
          <el-input
            v-else
            v-model="filter.spoken_values[0]"
            size="small"
            placeholder="取值"
          />
          <el-button text size="small" @click="removeFilter(index)">移除</el-button>
        </div>
        <el-button text size="small" @click="addFilter">+ 增加过滤</el-button>
      </div>

      <el-button
        class="panel__rerun"
        data-test="rerun"
        type="primary"
        size="small"
        :disabled="running"
        @click="rerun"
      >
        {{ dirty ? '按新条件重跑' : '重跑' }}
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.panel {
  padding: 12px;
  font-size: 13px;
}

.panel__title {
  margin-bottom: 12px;
  font-weight: 600;
}

.panel__empty {
  color: var(--el-text-color-secondary);
}

.panel__row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.panel__row label {
  flex: none;
  width: 32px;
  color: var(--el-text-color-secondary);
}

.panel__row--stack {
  flex-direction: column;
  align-items: stretch;
}

.panel__filter {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}

.panel__rerun {
  width: 100%;
  margin-top: 8px;
}
</style>
```

`timeRange` 是把 `draft.time.start`/`end` 桥接到日期选择器的计算属性，写在 `<script setup>` 中：

```ts
const timeRange = computed<[string, string] | null>({
  get: () => (draft.time ? [draft.time.start, draft.time.end] : null),
  set: (value) => {
    if (!draft.time || !value) return
    draft.time.start = value[0]
    draft.time.end = value[1]
    // The spoken expression no longer describes the range once edited by hand.
    draft.time.expression = `${value[0]} 至 ${value[1]}`
  },
})
```

- [ ] **Step 5: 写结果表**

`frontend/src/components/workbench/ResultTable.vue`：

```vue
<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ columns: string[]; rows: unknown[][] }>()

const tableColumns = computed(() => props.columns.map((name) => ({ prop: name, label: name })))

const tableRows = computed(() =>
  props.rows.map((row) => {
    const record: Record<string, unknown> = {}
    props.columns.forEach((name, index) => {
      // NULL must be visible: a blank cell reads as zero.
      record[name] = row[index] === null || row[index] === undefined ? '—' : row[index]
    })
    return record
  }),
)

function cell(value: unknown): string {
  const text = value === null || value === undefined ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function toCsv(): string {
  const header = props.columns.map(cell).join(',')
  const body = props.rows.map((row) => row.map(cell).join(',')).join('\n')
  return body ? `${header}\n${body}` : header
}

function exportCsv(): void {
  const blob = new Blob([`﻿${toCsv()}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'result.csv'
  anchor.click()
  URL.revokeObjectURL(url)
}

defineExpose({ tableColumns, tableRows, toCsv })
</script>

<template>
  <div class="result">
    <div v-if="!columns.length" class="result__empty">暂无数据</div>
    <template v-else>
      <el-table :data="tableRows" size="small" max-height="320">
        <el-table-column
          v-for="column in tableColumns"
          :key="column.prop"
          :prop="column.prop"
          :label="column.label"
        />
      </el-table>
      <el-button class="result__export" text size="small" @click="exportCsv">
        导出 CSV
      </el-button>
    </template>
  </div>
</template>

<style scoped>
.result__empty {
  padding: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.result__export {
  margin-top: 6px;
}
</style>
```

CSV 前缀写 BOM，否则 Excel 打开中文列名会乱码。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（条件面板 12 项 + 结果表 6 项）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/components/workbench frontend/tests/components/ConditionPanel.spec.ts frontend/tests/components/ResultTable.spec.ts
git commit -F - <<'EOF'
实现可编辑的查询条件面板与结果表

多轮上下文若只体现在聊天记录里，用户无法确认这一轮到底按什么口径、什么时间、什么过滤在算，改条件只能再打一句话且容易漏掉槽位。

- 条件面板绑定后端返回的槽位，新一轮返回即以后端为准重新同步
- 指标带版本号展示，维度与过滤的可选项来自数据集语义而非硬编码
- 枚举取值只提供业务值，物理编码不出现在界面
- 结果表把 NULL 显示为占位符，避免空单元格被读成零
- 导出 CSV 对含分隔符的值加引号并写 BOM，规避 Excel 乱码
- 验证：vitest 条件面板 12 项、结果表 6 项通过
EOF
```

---

### Task 5: 三分栏工作台

**Files:**
- Create: `frontend/src/components/workbench/ConversationList.vue`
- Create: `frontend/src/components/workbench/MessageStream.vue`
- Create: `frontend/src/components/workbench/AskInput.vue`
- Create: `frontend/src/views/WorkbenchView.vue`
- Create: `frontend/tests/views/WorkbenchView.spec.ts`

**Interfaces:**
- Consumes: `@/stores/session`、Task 3~4 的组件、`@/api/semantic`
- Produces:
  - `ConversationList` — props `conversations`、`activeId`；emit `select(id)`、`create`
  - `MessageStream` — props `messages`、`asking`；emit `choose`、`drill`
  - `AskInput` — props `disabled`；emit `submit(text)`
  - `WorkbenchView` — 组装三栏

- [ ] **Step 1: 写失败的工作台测试**

`frontend/tests/views/WorkbenchView.spec.ts`：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as chatApi from '@/api/chat'
import * as semanticApi from '@/api/semantic'
import WorkbenchView from '@/views/WorkbenchView.vue'
import type { AskResponse, DatasetDetail } from '@/api/types'

function answered(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    status: 'answered',
    conversation_id: 7,
    turn_id: 11,
    answer: {
      headline: '本月销售额 4,235 万',
      conclusion: '',
      assumptions: [],
      warnings: [],
      citation: {
        metric: 'sales_revenue v3（销售额）：已完成订单含税金额',
        time: '2026-08-01 ~ 2026-08-31（按完成日期）',
        filters: [{ label: '大区', value: '属于 华东', source: 'permission' }],
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

function dataset(): DatasetDetail {
  return {
    name: 'orders',
    business_name: '订单',
    is_published: true,
    physical_table: 'sample.orders',
    description: '',
    grain: '',
    applicable_scenario: '',
    forbidden_scenario: '',
    fields: [],
    metrics: [],
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  vi.spyOn(chatApi, 'listConversations').mockResolvedValue([
    { id: 7, title: '华东销售', dataset_name: 'orders', updated_at: '2026-08-12T09:00:00Z' },
  ])
  vi.spyOn(semanticApi, 'getDataset').mockResolvedValue(dataset())
})

async function mounted() {
  const wrapper = mount(WorkbenchView)
  await flushPromises()
  return wrapper
}

describe('WorkbenchView', () => {
  it('renders the three panes', async () => {
    const wrapper = await mounted()

    expect(wrapper.find('[data-test="pane-conversations"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-chat"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="pane-evidence"]').exists()).toBe(true)
  })

  it('loads the conversation list on mount', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('华东销售')
  })

  it('asking renders the answer with its citation', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('4,235 万')
    expect(wrapper.text()).toContain('由数据权限自动附加')
  })

  it('fills the condition panel from the returned slots', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="rerun"]').exists()).toBe(true)
  })

  it('shows the result table in the evidence pane', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-evidence"]').text()).toContain('sales_revenue')
  })

  it('offers a trace link for the latest turn', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="open-trace"]').exists()).toBe(true)
  })

  it('clicking a clarify option asks again', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({
        status: 'clarifying',
        answer: null,
        clarifications: [
          {
            kind: 'metric',
            target: 'sales_revenue',
            question: '你指的是哪个销售额？',
            options: [{ value: 'sales_revenue', label: '含税订单金额', hint: '' }],
          },
        ],
      }),
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('业绩怎么样')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="clarify-option"]').trigger('click')
    await flushPromises()

    expect(spy.mock.calls[1][0].question).toContain('含税订单金额')
  })

  it('rerunning from the panel asks again', async () => {
    const spy = vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="rerun"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('a refusal is shown in the stream, not as an error banner', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(
      answered({ status: 'refused', answer: null, refusal_reason: '你没有该数据的访问权限' }),
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('总成本')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-chat"]').text()).toContain('你没有该数据的访问权限')
    expect(wrapper.find('[data-test="transport-error"]').exists()).toBe(false)
  })

  it('a transport failure shows a banner', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(chatApi, 'ask').mockRejectedValue(new ApiError('无法连接服务', 0))
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="transport-error"]').text()).toContain('无法连接服务')
  })

  it('disables the input while asking', async () => {
    let release: (value: AskResponse) => void = () => {}
    vi.spyOn(chatApi, 'ask').mockReturnValue(
      new Promise<AskResponse>((resolve) => {
        release = resolve
      }),
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-test="ask-submit"]').attributes('disabled')).toBeDefined()
    release(answered())
    await flushPromises()
  })

  it('selecting a conversation loads its turns', async () => {
    const spy = vi.spyOn(chatApi, 'listTurns').mockResolvedValue([])
    const wrapper = await mounted()

    await wrapper.find('[data-test="conversation-item"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith(7)
  })

  it('starting a new conversation clears the stream', async () => {
    vi.spyOn(chatApi, 'ask').mockResolvedValue(answered())
    const wrapper = await mounted()

    await wrapper.find('[data-test="ask-input"] textarea').setValue('本月销售额')
    await wrapper.find('[data-test="ask-submit"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="new-conversation"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="pane-chat"]').text()).not.toContain('4,235 万')
  })

  it('has no mode selector this round', async () => {
    const wrapper = await mounted()
    expect(wrapper.find('[data-test="mode-selector"]').exists()).toBe(false)
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，`WorkbenchView` 不存在

- [ ] **Step 3: 写会话列表**

`frontend/src/components/workbench/ConversationList.vue`：

```vue
<script setup lang="ts">
import type { Conversation } from '@/api/types'

defineProps<{ conversations: Conversation[]; activeId: number | null }>()
const emit = defineEmits<{ select: [number]; create: [] }>()
</script>

<template>
  <div class="list">
    <el-button
      class="list__new"
      data-test="new-conversation"
      size="small"
      @click="emit('create')"
    >
      + 新会话
    </el-button>
    <div
      v-for="item in conversations"
      :key="item.id"
      class="list__item"
      :class="{ 'list__item--active': item.id === activeId }"
      data-test="conversation-item"
      @click="emit('select', item.id)"
    >
      <div class="list__title">{{ item.title }}</div>
      <div class="list__meta">{{ item.dataset_name }}</div>
    </div>
    <div v-if="!conversations.length" class="list__empty">还没有会话</div>
  </div>
</template>

<style scoped>
.list {
  padding: 8px;
  font-size: 13px;
}

.list__new {
  width: 100%;
  margin-bottom: 8px;
}

.list__item {
  padding: 8px;
  border-radius: 4px;
  cursor: pointer;
}

.list__item:hover,
.list__item--active {
  background: var(--el-fill-color-light);
}

.list__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.list__meta,
.list__empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
```

- [ ] **Step 4: 写消息流与输入框**

`frontend/src/components/workbench/MessageStream.vue`：

```vue
<script setup lang="ts">
import AnswerCard from '@/components/chat/AnswerCard.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import type { Clarification, ClarifyOption, DrillDown } from '@/api/types'
import type { ChatMessage } from '@/stores/session'

defineProps<{ messages: ChatMessage[]; asking: boolean }>()
const emit = defineEmits<{
  choose: [Clarification, ClarifyOption]
  drill: [DrillDown]
}>()
</script>

<template>
  <div class="stream">
    <div v-for="message in messages" :key="message.id" class="stream__row">
      <div v-if="message.role === 'user'" class="stream__question">{{ message.question }}</div>
      <div v-else class="stream__bubble">
        <ClarifyCard
          v-if="message.kind === 'clarify' && message.clarifications"
          :clarifications="message.clarifications"
          @choose="(request, option) => emit('choose', request, option)"
        />
        <AnswerCard v-else :message="message" @drill="(item) => emit('drill', item)" />
      </div>
    </div>
    <div v-if="asking" class="stream__pending">正在查询…</div>
  </div>
</template>

<style scoped>
.stream {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  overflow-y: auto;
}

.stream__question {
  align-self: flex-end;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
}

.stream__bubble {
  padding: 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
}

.stream__pending {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
```

`frontend/src/components/workbench/AskInput.vue`：

```vue
<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{ disabled: boolean }>()
const emit = defineEmits<{ submit: [string] }>()

const text = ref('')

function submit(): void {
  if (props.disabled || !text.value.trim()) return
  emit('submit', text.value)
  text.value = ''
}
</script>

<template>
  <div class="ask" data-test="ask-input">
    <el-input
      v-model="text"
      type="textarea"
      :rows="2"
      resize="none"
      placeholder="问一个数据问题，例如：华东本月销售额环比"
      @keydown.enter.exact.prevent="submit"
    />
    <el-button
      data-test="ask-submit"
      type="primary"
      :disabled="disabled"
      @click="submit"
    >
      提问
    </el-button>
  </div>
</template>

<style scoped>
.ask {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--el-border-color-light);
}
</style>
```

- [ ] **Step 5: 写工作台视图**

`frontend/src/views/WorkbenchView.vue`：

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { getDataset } from '@/api/semantic'
import AskInput from '@/components/workbench/AskInput.vue'
import ConditionPanel from '@/components/workbench/ConditionPanel.vue'
import ConversationList from '@/components/workbench/ConversationList.vue'
import MessageStream from '@/components/workbench/MessageStream.vue'
import ResultTable from '@/components/workbench/ResultTable.vue'
import { useSessionStore } from '@/stores/session'
import type { Clarification, ClarifyOption, DatasetDetail, DrillDown, SlotState } from '@/api/types'

const DATASET = 'orders'

const store = useSessionStore()
const dataset = ref<DatasetDetail | null>(null)

onMounted(async () => {
  await store.loadConversations()
  try {
    dataset.value = await getDataset(DATASET)
  } catch {
    dataset.value = null
  }
})

const latestAnswer = computed(() => {
  for (let index = store.messages.length - 1; index >= 0; index -= 1) {
    const message = store.messages[index]
    if (message.kind === 'answer' && message.answer) return message
  }
  return null
})

function onChoose(request: Clarification, option: ClarifyOption): void {
  void store.answerClarification(request, option)
}

function onDrill(item: DrillDown): void {
  if (!store.slotState) return
  const slots: SlotState = structuredClone(store.slotState)
  if (item.kind === 'dimension' && !slots.dimensions.includes(item.target)) {
    slots.dimensions.push(item.target)
  }
  void store.rerunWithSlots(slots)
}
</script>

<template>
  <div class="workbench">
    <aside class="workbench__pane workbench__pane--left" data-test="pane-conversations">
      <ConversationList
        :conversations="store.conversations"
        :active-id="store.activeConversationId"
        @select="store.openConversation"
        @create="store.startNew"
      />
    </aside>

    <section class="workbench__pane workbench__pane--center" data-test="pane-chat">
      <el-alert
        v-if="store.error"
        class="workbench__error"
        data-test="transport-error"
        type="error"
        :closable="false"
        :title="store.error"
      />
      <MessageStream
        class="workbench__stream"
        :messages="store.messages"
        :asking="store.asking"
        @choose="onChoose"
        @drill="onDrill"
      />
      <AskInput :disabled="store.asking" @submit="store.submit" />
    </section>

    <aside class="workbench__pane workbench__pane--right" data-test="pane-evidence">
      <ConditionPanel
        :slot-state="store.slotState"
        :dataset="dataset"
        :running="store.asking"
        @rerun="store.rerunWithSlots"
      />

      <div v-if="latestAnswer" class="workbench__evidence">
        <div class="workbench__subtitle">本轮成果</div>
        <ResultTable
          :columns="latestAnswer.answer!.columns"
          :rows="latestAnswer.answer!.rows"
        />
        <router-link
          class="workbench__trace"
          data-test="open-trace"
          :to="{ name: 'trace', params: { turnId: latestAnswer.turnId } }"
        >
          ▸ 查看 Trace
        </router-link>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: 200px 1fr 320px;
  height: 100vh;
}

.workbench__pane {
  overflow-y: auto;
}

.workbench__pane--left {
  border-right: 1px solid var(--el-border-color-light);
}

.workbench__pane--center {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench__pane--right {
  border-left: 1px solid var(--el-border-color-light);
}

.workbench__stream {
  flex: 1;
  min-height: 0;
}

.workbench__error {
  margin: 12px;
  width: auto;
}

.workbench__evidence {
  padding: 12px;
  border-top: 1px solid var(--el-border-color-light);
}

.workbench__subtitle {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.workbench__trace {
  display: inline-block;
  margin-top: 8px;
  font-size: 13px;
}
</style>
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（工作台 14 项）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/components/workbench frontend/src/views/WorkbenchView.vue frontend/tests/views/WorkbenchView.spec.ts
git commit -F - <<'EOF'
组装三分栏问数工作台

只有一个问答框的界面无法承载可信问数：用户看不到本轮口径、改不动条件、也进不去过程。历史实现把这些全塞进单个三千行页面组件，无法测试与维护。

- 左中右三栏各自独立组件，视图只做组装与事件转发
- 澄清点选与条件重跑都走 store 的同一条提问路径
- 右栏同时承载条件面板、结果表与 Trace 入口
- 拒答留在对话流内，仅传输失败才显示横幅
- 本轮不含模式选择器，并以测试固定该边界
- 验证：vitest 工作台 14 项通过
EOF
```

---

### Task 6: Trace 页面

**Files:**
- Create: `frontend/src/views/TraceView.vue`
- Create: `frontend/src/components/trace/StageTimeline.vue`
- Create: `frontend/src/components/trace/StageDetail.vue`
- Create: `frontend/tests/views/TraceView.spec.ts`

**Interfaces:**
- Consumes: `@/api/trace`
- Produces:
  - `StageTimeline` — props `stages: TraceStage[]`、`activeSequence: number`；emit `select(sequence)`
  - `StageDetail` — props `stage: TraceStage | null`
  - `TraceView` — 读路由参数 `turnId`，加载 Trace 并提供重放

Trace 页是本轮的调试主力，要求是**七个阶段一眼看全，出错的那个阶段一眼看出**。

- [ ] **Step 1: 写失败的 Trace 页测试**

`frontend/tests/views/TraceView.spec.ts`：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as traceApi from '@/api/trace'
import TraceView from '@/views/TraceView.vue'
import type { Trace, TraceStage } from '@/api/types'

function stage(overrides: Partial<TraceStage> = {}): TraceStage {
  return {
    stage: 'intent',
    sequence: 2,
    input_payload: { question: '本月销售额' },
    output_payload: { metrics: ['sales_revenue'] },
    model: 'stub',
    prompt_tokens: 90,
    completion_tokens: 15,
    elapsed_ms: 420,
    error: '',
    ...overrides,
  }
}

function trace(overrides: Partial<Trace> = {}): Trace {
  return {
    turn_id: 11,
    question: '本月销售额',
    status: 'answered',
    intent_snapshot: { metrics: ['sales_revenue'] },
    stages: [
      stage({ stage: 'verified_recall', sequence: 1, model: null, prompt_tokens: 0, elapsed_ms: 6 }),
      stage(),
      stage({ stage: 'semantic_resolve', sequence: 3, model: null, prompt_tokens: 0 }),
      stage({ stage: 'compile', sequence: 4, model: null, prompt_tokens: 0 }),
      stage({
        stage: 'security',
        sequence: 5,
        model: null,
        prompt_tokens: 0,
        output_payload: { sql: 'SELECT SUM(amount) FROM sample.orders WHERE region_code IN (\'EC\')' },
      }),
      stage({ stage: 'execute', sequence: 6, model: null, prompt_tokens: 0, elapsed_ms: 38 }),
      stage({ stage: 'answer', sequence: 7, model: null, prompt_tokens: 0 }),
    ],
    ...overrides,
  }
}

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { turnId: '11' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

beforeEach(() => {
  vi.restoreAllMocks()
})

async function mounted(payload = trace()) {
  vi.spyOn(traceApi, 'getTrace').mockResolvedValue(payload)
  const wrapper = mount(TraceView)
  await flushPromises()
  return wrapper
}

describe('TraceView', () => {
  it('shows the question and the turn status', async () => {
    const wrapper = await mounted()
    expect(wrapper.text()).toContain('本月销售额')
    expect(wrapper.text()).toContain('已作答')
  })

  it('lists all stages in order with chinese labels', async () => {
    const wrapper = await mounted()
    const labels = wrapper.findAll('[data-test="stage-item"]').map((item) => item.text())

    expect(labels[0]).toContain('固定查询召回')
    expect(labels[1]).toContain('意图识别')
    expect(labels[4]).toContain('安全改写')
    expect(labels).toHaveLength(7)
  })

  it('shows elapsed time per stage', async () => {
    const wrapper = await mounted()
    expect(wrapper.findAll('[data-test="stage-item"]')[0].text()).toContain('6')
  })

  it('shows tokens only for the stage that used a model', async () => {
    const wrapper = await mounted()
    const items = wrapper.findAll('[data-test="stage-item"]')

    expect(items[1].text()).toContain('90')
    expect(items[0].text()).not.toContain('Token')
  })

  it('selects the first stage by default', async () => {
    const wrapper = await mounted()
    expect(wrapper.vm.activeSequence).toBe(1)
  })

  it('clicking a stage shows its payloads', async () => {
    const wrapper = await mounted()

    await wrapper.findAll('[data-test="stage-item"]')[4].trigger('click')

    expect(wrapper.find('[data-test="stage-detail"]').text()).toContain('sample.orders')
  })

  it('highlights a failed stage', async () => {
    const payload = trace({
      status: 'failed',
      stages: [stage({ sequence: 1, stage: 'intent', error: 'ValidationError: 缺少 confidence' })],
    })
    const wrapper = await mounted(payload)

    expect(wrapper.find('[data-test="stage-item"]').classes()).toContain('stage--error')
  })

  it('shows the error text of a failed stage', async () => {
    const payload = trace({
      stages: [stage({ sequence: 1, error: 'ValidationError: 缺少 confidence' })],
    })
    const wrapper = await mounted(payload)

    expect(wrapper.text()).toContain('缺少 confidence')
  })

  it('shows the executed sql for the security stage', async () => {
    const wrapper = await mounted()

    await wrapper.findAll('[data-test="stage-item"]')[4].trigger('click')

    expect(wrapper.find('[data-test="stage-sql"]').text()).toContain('SELECT')
  })

  it('replay shows the sql and whether it matches the original', async () => {
    const spy = vi.spyOn(traceApi, 'replayTurn').mockResolvedValue({
      sql: 'SELECT SUM(amount) FROM sample.orders',
      display_sql: 'SELECT SUM(amount) FROM sample.orders',
      matches_original: true,
      applied_row_filters: ['大区'],
      masked_field_names: [],
    })
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(spy).toHaveBeenCalledWith(11)
    expect(wrapper.find('[data-test="replay-result"]').text()).toContain('与原始一致')
  })

  it('replay reports a mismatch loudly', async () => {
    vi.spyOn(traceApi, 'replayTurn').mockResolvedValue({
      sql: 'SELECT SUM(amount) FROM sample.orders',
      display_sql: 'SELECT SUM(amount) FROM sample.orders',
      matches_original: false,
      applied_row_filters: [],
      masked_field_names: [],
    })
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="replay-result"]').text()).toContain('与原始不一致')
  })

  it('a turn without a snapshot reports why replay is unavailable', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(traceApi, 'replayTurn').mockRejectedValue(
      new ApiError('该轮没有可重放的意图快照', 409),
    )
    const wrapper = await mounted()

    await wrapper.find('[data-test="replay"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('没有可重放的意图快照')
  })

  it('a missing turn shows a not-found message', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(traceApi, 'getTrace').mockRejectedValue(new ApiError('记录不存在', 404))
    const wrapper = mount(TraceView)
    await flushPromises()

    expect(wrapper.text()).toContain('记录不存在')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，`TraceView` 不存在

- [ ] **Step 3: 写阶段时间线**

`frontend/src/components/trace/StageTimeline.vue`：

```vue
<script setup lang="ts">
import type { TraceStage } from '@/api/types'

const STAGE_LABELS: Record<string, string> = {
  verified_recall: '固定查询召回',
  intent: '意图识别',
  semantic_resolve: '语义解析',
  compile: 'SQL 编译',
  security: '安全改写',
  execute: '执行与校验',
  answer: '作答',
}

defineProps<{ stages: TraceStage[]; activeSequence: number }>()
const emit = defineEmits<{ select: [number] }>()

function label(name: string): string {
  return STAGE_LABELS[name] ?? name
}
</script>

<template>
  <ol class="timeline">
    <li
      v-for="item in stages"
      :key="item.sequence"
      class="stage"
      :class="{
        'stage--error': Boolean(item.error),
        'stage--active': item.sequence === activeSequence,
      }"
      data-test="stage-item"
      @click="emit('select', item.sequence)"
    >
      <div class="stage__name">{{ item.sequence }}. {{ label(item.stage) }}</div>
      <div class="stage__meta">
        <span>{{ item.elapsed_ms }} ms</span>
        <span v-if="item.model">
          {{ item.model }} · Token {{ item.prompt_tokens }}/{{ item.completion_tokens }}
        </span>
      </div>
      <div v-if="item.error" class="stage__error">{{ item.error }}</div>
    </li>
  </ol>
</template>

<style scoped>
.timeline {
  margin: 0;
  padding: 0;
  list-style: none;
}

.stage {
  padding: 8px 10px;
  border-left: 3px solid var(--el-border-color);
  cursor: pointer;
  font-size: 13px;
}

.stage--active {
  background: var(--el-fill-color-light);
  border-left-color: var(--el-color-primary);
}

.stage--error {
  border-left-color: var(--el-color-danger);
}

.stage__meta {
  display: flex;
  gap: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stage__error {
  margin-top: 4px;
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
```

- [ ] **Step 4: 写阶段详情**

`frontend/src/components/trace/StageDetail.vue`：

```vue
<script setup lang="ts">
import { computed } from 'vue'

import type { TraceStage } from '@/api/types'

const props = defineProps<{ stage: TraceStage | null }>()

// The security stage's sql is the one that actually ran; it gets its own block
// rather than being buried in the json dump.
const sql = computed(() => {
  const value = props.stage?.output_payload?.sql
  return typeof value === 'string' ? value : ''
})

function pretty(payload: Record<string, unknown> | null): string {
  return payload ? JSON.stringify(payload, null, 2) : '—'
}
</script>

<template>
  <div class="detail" data-test="stage-detail">
    <div v-if="!stage" class="detail__empty">选择左侧的阶段查看详情</div>
    <template v-else>
      <div v-if="sql" class="detail__block">
        <div class="detail__title">实际执行的 SQL</div>
        <pre data-test="stage-sql">{{ sql }}</pre>
      </div>
      <div class="detail__block">
        <div class="detail__title">输入</div>
        <pre>{{ pretty(stage.input_payload) }}</pre>
      </div>
      <div class="detail__block">
        <div class="detail__title">输出</div>
        <pre>{{ pretty(stage.output_payload) }}</pre>
      </div>
    </template>
  </div>
</template>

<style scoped>
.detail {
  font-size: 13px;
}

.detail__empty {
  color: var(--el-text-color-secondary);
}

.detail__block + .detail__block {
  margin-top: 12px;
}

.detail__title {
  margin-bottom: 4px;
  color: var(--el-text-color-secondary);
}

pre {
  margin: 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  overflow-x: auto;
  font-size: 12px;
}
</style>
```

- [ ] **Step 5: 写 Trace 视图**

`frontend/src/views/TraceView.vue`：

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '@/api/client'
import { getTrace, replayTurn } from '@/api/trace'
import StageDetail from '@/components/trace/StageDetail.vue'
import StageTimeline from '@/components/trace/StageTimeline.vue'
import type { Replay, Trace } from '@/api/types'

const STATUS_LABELS: Record<string, string> = {
  answered: '已作答',
  clarifying: '待澄清',
  refused: '已拒答',
  failed: '执行失败',
}

const route = useRoute()
const turnId = Number(route.params.turnId)

const trace = ref<Trace | null>(null)
const activeSequence = ref(1)
const replay = ref<Replay | null>(null)
const error = ref('')

onMounted(async () => {
  try {
    trace.value = await getTrace(turnId)
    activeSequence.value = trace.value.stages[0]?.sequence ?? 1
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  }
})

const activeStage = computed(
  () => trace.value?.stages.find((item) => item.sequence === activeSequence.value) ?? null,
)

async function runReplay(): Promise<void> {
  error.value = ''
  replay.value = null
  try {
    replay.value = await replayTurn(turnId)
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '重放失败'
  }
}

defineExpose({ activeSequence })
</script>

<template>
  <div class="trace">
    <header class="trace__header">
      <router-link to="/ask">← 返回工作台</router-link>
      <template v-if="trace">
        <h2>{{ trace.question }}</h2>
        <el-tag size="small">{{ STATUS_LABELS[trace.status] ?? trace.status }}</el-tag>
      </template>
    </header>

    <el-alert v-if="error" type="error" :closable="false" :title="error" />

    <div v-if="trace" class="trace__body">
      <div class="trace__timeline">
        <StageTimeline
          :stages="trace.stages"
          :active-sequence="activeSequence"
          @select="(value) => (activeSequence = value)"
        />
        <el-button class="trace__replay" data-test="replay" size="small" @click="runReplay">
          从意图快照重放
        </el-button>
        <div v-if="replay" class="trace__replay-result" data-test="replay-result">
          <el-tag :type="replay.matches_original ? 'success' : 'warning'" size="small">
            {{ replay.matches_original ? '与原始一致' : '与原始不一致' }}
          </el-tag>
          <pre>{{ replay.display_sql }}</pre>
          <div v-if="replay.applied_row_filters.length" class="trace__note">
            行级权限附加：{{ replay.applied_row_filters.join('、') }}
          </div>
        </div>
      </div>
      <div class="trace__detail">
        <StageDetail :stage="activeStage" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.trace {
  padding: 16px;
}

.trace__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.trace__header h2 {
  margin: 0;
  font-size: 16px;
}

.trace__body {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}

.trace__replay {
  margin-top: 12px;
  width: 100%;
}

.trace__replay-result {
  margin-top: 8px;
}

.trace__replay-result pre {
  margin: 6px 0 0;
  padding: 8px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  white-space: pre-wrap;
}

.trace__note {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（Trace 页 13 项）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/views/TraceView.vue frontend/src/components/trace frontend/tests/views/TraceView.spec.ts
git commit -F - <<'EOF'
实现 Trace 页面与意图重放

后端已按阶段记录完整过程，但没有界面，排查一次错答只能查库。若把输入输出平铺成一份 JSON，实际执行的 SQL 与出错阶段会被埋在里面，失去调试价值。

- 七个阶段以时间线呈现，出错阶段标红并直接展示错误文本
- 仅使用模型的阶段显示模型名与 Token，其余阶段只显示耗时
- 安全改写阶段的 SQL 单独成块，即实际执行的那条语句
- 重放按钮从意图快照重编译并显式提示是否与原始一致
- 无快照的轮次给出原因而非静默失败
- 验证：vitest Trace 页 13 项通过
EOF
```

---

### Task 7: 配置侧页面

**Files:**
- Create: `frontend/src/views/DatasetsView.vue`
- Create: `frontend/src/views/DatasetDetailView.vue`
- Create: `frontend/src/components/admin/LintReportCard.vue`
- Create: `frontend/tests/views/DatasetsView.spec.ts`
- Create: `frontend/tests/views/DatasetDetailView.spec.ts`

**Interfaces:**
- Consumes: `@/api/semantic`
- Produces:
  - `DatasetsView` — 数据集列表、发布状态、进入详情
  - `DatasetDetailView` — 字段与指标只读表、体检报告、发布按钮
  - `LintReportCard` — props `report: LintReport | null`；emit `publish`

本轮配置侧**只读 + 发布**，不做字段编辑表单。理由：语义配置由脚本与 SQL 落库（计划 01 的样本工厂即是），而发布前的体检与发布动作必须在界面上可见——这是防止未体检语义流入生产的关卡。字段编辑 UI 属于下一轮多数据集场景。

- [ ] **Step 1: 写失败的列表页测试**

`frontend/tests/views/DatasetsView.spec.ts`：

```ts
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as semanticApi from '@/api/semantic'
import DatasetsView from '@/views/DatasetsView.vue'

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
    vi.spyOn(semanticApi, 'listDatasets').mockRejectedValue(new ApiError('无法连接服务', 0))
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
```

- [ ] **Step 2: 写失败的详情页测试**

`frontend/tests/views/DatasetDetailView.spec.ts`：

```ts
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
    expect(wrapper.find('[data-test="publish"]').attributes('disabled')).toBeUndefined()
  })

  it('publish is blocked when lint found errors', async () => {
    vi.spyOn(semanticApi, 'getLint').mockResolvedValue(
      report({
        publishable: false,
        issues: [{ severity: 'error', target: 'sales_revenue', message: '缺少时间字段' }],
      }),
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
      }),
    )
    const wrapper = await mounted()

    expect(wrapper.find('[data-test="publish"]').attributes('disabled')).toBeUndefined()
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
      new ApiError('语义体检未通过，无法发布', 409),
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd frontend && npm test`
Expected: FAIL，两个视图不存在

- [ ] **Step 4: 写体检报告卡**

`frontend/src/components/admin/LintReportCard.vue`：

```vue
<script setup lang="ts">
import { computed } from 'vue'

import type { LintReport } from '@/api/types'

const props = defineProps<{ report: LintReport | null; publishing: boolean }>()
const emit = defineEmits<{ publish: [] }>()

const errors = computed(() => props.report?.issues.filter((item) => item.severity === 'error') ?? [])
const warnings = computed(
  () => props.report?.issues.filter((item) => item.severity === 'warning') ?? [],
)
</script>

<template>
  <div class="lint">
    <div class="lint__title">语义体检</div>
    <div v-if="!report" class="lint__empty">体检结果加载中</div>
    <template v-else>
      <el-alert
        v-if="!errors.length && !warnings.length"
        type="success"
        :closable="false"
        title="未发现问题"
      />
      <el-alert
        v-for="(issue, index) in errors"
        :key="`e${index}`"
        class="lint__issue"
        type="error"
        :closable="false"
        :title="`${issue.target}：${issue.message}`"
      />
      <el-alert
        v-for="(issue, index) in warnings"
        :key="`w${index}`"
        class="lint__issue"
        type="warning"
        :closable="false"
        :title="`${issue.target}：${issue.message}`"
      />
      <el-button
        class="lint__publish"
        data-test="publish"
        type="primary"
        size="small"
        :disabled="!report.publishable || publishing"
        @click="emit('publish')"
      >
        发布
      </el-button>
      <div v-if="!report.publishable" class="lint__hint">存在错误项，修复后才能发布</div>
    </template>
  </div>
</template>

<style scoped>
.lint {
  font-size: 13px;
}

.lint__title {
  margin-bottom: 8px;
  font-weight: 600;
}

.lint__issue {
  margin-bottom: 6px;
}

.lint__publish {
  margin-top: 8px;
}

.lint__empty,
.lint__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
```

- [ ] **Step 5: 写列表页**

`frontend/src/views/DatasetsView.vue`：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { listDatasets } from '@/api/semantic'
import type { DatasetSummary } from '@/api/types'

const datasets = ref<DatasetSummary[]>([])
const error = ref('')
const loaded = ref(false)

onMounted(async () => {
  try {
    datasets.value = await listDatasets()
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  } finally {
    loaded.value = true
  }
})
</script>

<template>
  <div class="datasets">
    <header class="datasets__header">
      <router-link to="/ask">← 返回工作台</router-link>
      <h2>数据集</h2>
    </header>

    <el-alert v-if="error" type="error" :closable="false" :title="error" />

    <el-table v-else-if="datasets.length" :data="datasets" size="small">
      <el-table-column label="名称">
        <template #default="{ row }">
          <router-link
            data-test="dataset-link"
            :to="{ name: 'dataset-detail', params: { name: row.name } }"
          >
            {{ row.business_name }}（{{ row.name }}）
          </router-link>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.is_published ? 'success' : 'info'" size="small">
            {{ row.is_published ? '已发布' : '未发布' }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>

    <div v-else-if="loaded" class="datasets__empty">还没有数据集</div>
  </div>
</template>

<style scoped>
.datasets {
  padding: 16px;
}

.datasets__header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.datasets__header h2 {
  margin: 0;
  font-size: 16px;
}

.datasets__empty {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
```

- [ ] **Step 6: 写详情页**

`frontend/src/views/DatasetDetailView.vue`：

```vue
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '@/api/client'
import { getDataset, getLint, publishDataset } from '@/api/semantic'
import LintReportCard from '@/components/admin/LintReportCard.vue'
import type { DatasetDetail, LintReport } from '@/api/types'

const route = useRoute()
const name = String(route.params.name)

const dataset = ref<DatasetDetail | null>(null)
const report = ref<LintReport | null>(null)
const error = ref('')
const publishing = ref(false)

async function load(): Promise<void> {
  try {
    dataset.value = await getDataset(name)
    report.value = await getLint(name)
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '加载失败'
  }
}

onMounted(load)

async function publish(): Promise<void> {
  error.value = ''
  publishing.value = true
  try {
    await publishDataset(name)
    await load()
  } catch (raised) {
    error.value = raised instanceof ApiError ? raised.message : '发布失败'
  } finally {
    publishing.value = false
  }
}

function enumSummary(values: { business_value: string; aliases: string[] }[]): string {
  return values
    .map((item) =>
      item.aliases.length ? `${item.business_value}（${item.aliases.join('、')}）` : item.business_value,
    )
    .join('，')
}
</script>

<template>
  <div class="dataset">
    <header class="dataset__header">
      <router-link to="/admin/datasets">← 数据集列表</router-link>
      <template v-if="dataset">
        <h2>{{ dataset.business_name }}（{{ dataset.name }}）</h2>
        <el-tag :type="dataset.is_published ? 'success' : 'info'" size="small">
          {{ dataset.is_published ? '已发布' : '未发布' }}
        </el-tag>
      </template>
    </header>

    <el-alert v-if="error" type="error" :closable="false" :title="error" />

    <div v-if="dataset" class="dataset__body">
      <section class="dataset__main">
        <dl class="dataset__meta">
          <div><dt>物理表</dt><dd>{{ dataset.physical_table }}</dd></div>
          <div><dt>粒度</dt><dd>{{ dataset.grain }}</dd></div>
          <div><dt>适用</dt><dd>{{ dataset.applicable_scenario }}</dd></div>
          <div data-test="forbidden">
            <dt>禁用</dt>
            <dd class="dataset__forbidden">{{ dataset.forbidden_scenario }}</dd>
          </div>
        </dl>

        <h3>指标</h3>
        <el-table :data="dataset.metrics" size="small" data-test="metrics">
          <el-table-column prop="business_name" label="名称" />
          <el-table-column prop="name" label="标识" />
          <el-table-column label="版本" width="80">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column prop="kind" label="类型" width="90" />
          <el-table-column prop="aggregation_behavior" label="聚合行为" width="120" />
          <el-table-column prop="description" label="口径说明" />
        </el-table>

        <h3>字段</h3>
        <el-table :data="dataset.fields" size="small" data-test="fields">
          <el-table-column prop="business_name" label="名称" />
          <el-table-column prop="name" label="标识" />
          <el-table-column prop="semantic_type" label="语义类型" width="110" />
          <el-table-column prop="default_aggregation" label="默认聚合" width="100" />
          <el-table-column label="允许聚合" width="140">
            <template #default="{ row }">{{ row.allowed_aggregations.join('、') || '—' }}</template>
          </el-table-column>
          <el-table-column prop="sensitivity" label="敏感级" width="100" />
          <el-table-column label="枚举值">
            <template #default="{ row }">{{ enumSummary(row.enum_values) || '—' }}</template>
          </el-table-column>
        </el-table>
      </section>

      <aside class="dataset__side">
        <LintReportCard :report="report" :publishing="publishing" @publish="publish" />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.dataset {
  padding: 16px;
}

.dataset__header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.dataset__header h2 {
  margin: 0;
  font-size: 16px;
}

.dataset__body {
  display: grid;
  grid-template-columns: 1fr 280px;
  gap: 16px;
  align-items: start;
  margin-top: 12px;
}

.dataset__meta {
  margin: 0 0 16px;
  font-size: 13px;
}

.dataset__meta > div {
  display: flex;
  gap: 8px;
  margin: 2px 0;
}

.dataset__meta dt {
  flex: none;
  width: 48px;
  color: var(--el-text-color-secondary);
}

.dataset__meta dd {
  margin: 0;
}

.dataset__forbidden {
  color: var(--el-color-warning);
}

h3 {
  margin: 16px 0 8px;
  font-size: 14px;
}
</style>
```

- [ ] **Step 7: 运行全量测试与构建**

Run: `cd frontend && npm test && npm run build`
Expected: PASS（前端合计 112 项；`npm run build` 此时应通过，四个路由视图均已存在）

- [ ] **Step 8: 提交**

```bash
git add frontend/src/views/DatasetsView.vue frontend/src/views/DatasetDetailView.vue frontend/src/components/admin frontend/tests/views/DatasetsView.spec.ts frontend/tests/views/DatasetDetailView.spec.ts
git commit -F - <<'EOF'
实现数据集配置与发布页面

语义配置目前只在库里，管理员看不到当前口径、敏感级与枚举字典，也无处执行发布前体检，未体检的语义可能直接被问数使用。

- 列表页展示各数据集发布状态并进入详情
- 详情页只读展示粒度、适用与禁用场景、指标口径与字段敏感级、枚举值及别名
- 体检报告区分错误与告警，仅错误阻断发布并给出原因
- 发布成功后重新拉取状态，发布被拒时显示服务端原因
- 本轮不做字段编辑表单，并以测试固定该边界
- 验证：vitest 列表页 4 项、详情页 11 项通过；前端全量 112 项通过，npm run build 通过
EOF
```

---

## 自查

**Spec 覆盖**（对应设计文档 6 前端工作台、6.1 配置侧页面、6.2 反面样本约束）：

| Spec 条目 | 承载任务 |
|---|---|
| 6 三分栏布局（会话列表 / 对话流 / 证据与条件） | Task 5 |
| 6「引证块默认展开，不折叠」 | Task 3（`CitationBlock`，并以测试固定无折叠） |
| 6「← 数据权限自动附加」必须显式出现 | Task 3（`source === 'permission'` 的行） |
| M-18 澄清带可点选项，不让用户重新打字 | Task 3（`ClarifyCard`）+ Task 2（点选后回填再问） |
| M-19 结构化多轮上下文在界面上的同一份数据 | Task 2（store 镜像 `slot_state`）+ Task 4（面板绑定同一份） |
| 6「拒答与失败是正常的一轮，不是错误弹窗」 | Task 2（进 `messages`）+ Task 5（仅传输失败进横幅） |
| M-34 Trace 每阶段输入/输出/模型/Token/耗时/错误 | Task 6 |
| 重放并展示是否与原始一致 | Task 6 |
| M-38 反馈归因（负反馈必须选分类） | Task 3（`FeedbackBar` 前置校验） |
| 6.1 数据集/字段语义、枚举字典、指标定义可见 | Task 7（详情页只读表） |
| 6.1 语义体检与发布 | Task 7（`LintReportCard`，错误阻断发布） |
| 6.2「任何单文件组件超过 300 行即拆分」 | 全部任务（最大组件 `ConditionPanel.vue`，见下） |
| 本轮不做模式选择器、不做字段编辑表单 | Task 5、Task 7 以测试固定边界 |

**测试规模**：Task 1 的 6 项 + Task 2 的 15 项 + Task 3 的 31 项 + Task 4 的 18 项 + Task 5 的 14 项 + Task 6 的 13 项 + Task 7 的 15 项 = 112 项。其中 Task 3 的 31 项拆为 `CitationBlock.spec.ts` 8 项、`ClarifyCard.spec.ts` 6 项、`FeedbackBar.spec.ts` 7 项、`AnswerCard.spec.ts` 10 项；Task 4 的 18 项拆为 `ConditionPanel.spec.ts` 12 项与 `ResultTable.spec.ts` 6 项；Task 7 的 15 项拆为 `DatasetsView.spec.ts` 4 项与 `DatasetDetailView.spec.ts` 11 项。

**界面上的安全断言**（前端不是安全边界，但不能反向削弱后端约束）：

- 引证块不可折叠、权限附加行必须渲染：`CitationBlock.spec.ts`
- 拒答不渲染引证与反馈栏，但保留 Trace 入口：`AnswerCard.spec.ts`
- 过滤枚举下拉只提供业务值，物理编码不出现在界面：`ConditionPanel.spec.ts`
- 不可分组字段不进维度、不可过滤字段不进过滤：`ConditionPanel.spec.ts`
- 结果表 NULL 渲染为 `—` 而非空白：`ResultTable.spec.ts`
- 体检存在错误项时发布按钮 disabled：`DatasetDetailView.spec.ts`

**类型一致性**：`src/api/types.ts` 手写，取后端 Pydantic 模型（`AskOut`/`ConversationOut`/`TurnOut`/`TraceOut`/`ReplayOut`/`DatasetSummaryOut`/`DatasetDetailOut`/`LintReportOut`）中界面用到的字段子集，**字段名与嵌套结构与后端逐字对齐**，不做重命名或扁平化。界面不用的字段（如 `FieldOut.physical_column`、`MetricOut.expression` 等编译期才需要的口径细节）不进前端类型——它们出现在界面上等于把物理实现暴露给使用者。后端改字段名不会自动同步，但引用处会在 `vue-tsc` 阶段报错，这是有意选择的「显式对齐」而非代码生成，代价是每次后端契约变更要手工改一次 `types.ts`。

**组件规模**：按本计划给出的实现，最大的是 `ConditionPanel.vue` 约 264 行（含样式），其后 `DatasetDetailView.vue` 约 165 行、`WorkbenchView.vue` 约 156 行、`TraceView.vue` 约 137 行，其余均在 130 行内，全部低于 300 行的拆分线。

**对后端计划的依赖**（实施顺序上必须在计划 01~04 之后）：

| 依赖项 | 来源 |
|---|---|
| `POST /api/chat/ask`、会话与轮次列表、反馈 | 计划 04 Task 7 |
| `GET /api/trace/turns/{id}`、`POST .../replay` | 计划 04 Task 7 |
| `GET /api/datasets`、`GET /api/datasets/{name}`、`/lint`、`/publish` | 计划 01 Task 8 |
| `X-Username` 请求头认证 | 计划 04 Task 7（`get_current_username`） |

**一处有意的简化**：历史会话回放只显示 `headline` 与 `conclusion`，不显示引证块——后端 `turns.answer` 只存这两项。若为了界面把完整答案也存进去，引证就有两份真相（`turns.answer` 与 Trace），口径不一致时无从判断哪份为准。代价是看旧轮次的完整引证要点进 Trace 页。

**一处已知缺口**：本轮没有前端登录页，`setUsername` 由启动时写入（开发期从 URL 查询参数或本地存储读取），对应 spec 3.1 中 `api-gateway` 认证职责的占位实现。接入真实身份时改动点收在 `src/api/client.ts` 一处。

## 交付物

完成本计划后：`npm run dev` 起前端、`uvicorn` 起后端，即可在浏览器里完成一次完整问数——提问、看到带引证的答案、被追问时点选项澄清、改条件重跑、给反馈、点进 Trace 看七个阶段并重放，以及在配置侧查看语义并发布数据集。这是「可信问数闭环」的可演示形态，计划 01~05 至此构成第一个可交付版本。




