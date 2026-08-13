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
    mockFetch(422, {
      detail: [{ loc: ['body', 'category'], msg: 'Value error, 负反馈必须归因' }],
    })

    await expect(request('/api/chat/turns/1/feedback')).rejects.toMatchObject({
      message: expect.stringContaining('负反馈必须归因'),
    })
  })
})