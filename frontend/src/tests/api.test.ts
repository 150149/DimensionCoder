import {afterEach, describe, expect, it, vi} from 'vitest'
import {api, ApiError} from '../api/client'

function okResponse(body: unknown) {
  return {ok: true, status: 200, json: async () => body} as Response
}

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('test_list_tasks_sends_request', async () => {
    const body = {
      epics: [],
      tasks: [{id: 't1', type: 'dev-full-flow', title: '任务', status: 'active', created_at: '2026-01-01T00:00:00', updated_at: '2026-01-01T00:00:00', steps: []}],
      task_count: 1,
      status_distribution: {active: 1},
      available_task_types: [],
    }
    const mockFetch = vi.fn().mockResolvedValue(okResponse(body))
    vi.stubGlobal('fetch', mockFetch)

    const data = await api.listTasks()

    expect(mockFetch).toHaveBeenCalledTimes(1)
    expect(mockFetch.mock.calls[0][0]).toBe('/api/tasks')
    expect(data.task_count).toBe(1)
    expect(data.tasks[0].id).toBe('t1')
  })

  it('test_fsWrite_body', async () => {
    const mockFetch = vi.fn().mockResolvedValue(okResponse({status: 'ok', size: 7}))
    vi.stubGlobal('fetch', mockFetch)

    await api.fsWrite('/a/b.txt', 'content')

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/fs/file')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({path: '/a/b.txt', content: 'content'})
  })

  it('test_fsWrite_baseMtime_optional', async () => {

    const mockFetch = vi.fn().mockResolvedValue(okResponse({status: 'ok', size: 7}))
    vi.stubGlobal('fetch', mockFetch)

    await api.fsWrite('/a/b.txt', 'content', 1700000000)
    let [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/fs/file')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({path: '/a/b.txt', content: 'content', baseMtime: 1700000000})

    await api.fsWrite('/a/b.txt', 'content')
    ;[url, init] = mockFetch.mock.calls[1] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({path: '/a/b.txt', content: 'content'})
  })

  it('test_fsTree_recursive_optional', async () => {

    const mockFetch = vi.fn().mockResolvedValue(okResponse({path: '.', entries: [], truncated: false}))
    vi.stubGlobal('fetch', mockFetch)

    await api.fsTree('src', {recursive: true})
    let [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/fs/tree?path=src&recursive=true')
    expect(init.method ?? 'GET').toBe('GET')

    await api.fsTree()
    ;[url, init] = mockFetch.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/api/fs/tree')

    await api.fsTree('src')
    ;[url, init] = mockFetch.mock.calls[2] as [string, RequestInit]
    expect(url).toBe('/api/fs/tree?path=src')
  })

  it('test_network_error_swallowed', async () => {

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(api.listTasks()).rejects.toBeInstanceOf(ApiError)
    await expect(api.listTasks()).rejects.toMatchObject({status: 0})
  })
})
