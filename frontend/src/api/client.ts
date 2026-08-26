import type {ConfigSave, ConfigView, CreateTaskResult, FsFile, FsTree, Message, MonitorConversations, StepData, TaskDetail, TaskOverview,} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const BASE_URL = ''

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE_URL + path, {
      ...init,
      headers: {
        ...(init.body ? {'Content-Type': 'application/json'} : {}),
        ...(init.headers ?? {}),
      },
    })
  } catch {

    throw new ApiError(0, '网络错误，请检查后端服务')
  }

  if (!res.ok) {
    let message = `HTTP ${res.status}`
    try {
      const body: unknown = await res.json()
      if (body && typeof body === 'object') {
        const obj = body as Record<string, unknown>
        const err = obj.error ?? obj.detail
        if (typeof err === 'string' && err) message = err
      }
    } catch {

    }
    throw new ApiError(res.status, message)
  }

  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function json(method: string, body?: unknown): RequestInit {
  return {method, body: body === undefined ? undefined : JSON.stringify(body)}
}

export const api = {

  listTasks(): Promise<TaskOverview> {
    return request<TaskOverview>('/api/tasks')
  },

  createTask(description: string): Promise<CreateTaskResult> {
    return request<CreateTaskResult>('/api/task', json('POST', {description}))
  },

  startTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}/start`, json('POST'))
  },

  setBestEffort(taskId: string, enabled: boolean): Promise<{ status: string; best_effort: boolean }> {
    return request<{ status: string; best_effort: boolean }>(
        `/api/task/${encodeURIComponent(taskId)}/best-effort`,
        json('POST', {enabled}),
    )
  },

  gracefulRestart(action: 'restart' | 'shutdown' | '' = 'restart'): Promise<{ status: string; action: string; message: string }> {
    return request<{ status: string; action: string; message: string }>(
        '/api/admin/graceful-restart',
        json('POST', {action}),
    )
  },

  pauseTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}/pause`, json('POST', {pause_level: 'user'}))
  },

  getTask(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(`/api/task/${encodeURIComponent(taskId)}`)
  },

  getMonitorConversations(taskId: string): Promise<MonitorConversations> {
    return request<MonitorConversations>(`/api/task/${encodeURIComponent(taskId)}/monitor-conversations`)
  },

  getStepMessages(taskId: string, stepId: string, afterSeq: number = -1): Promise<{ messages: Message[]; max_seq: number; after_seq: number }> {
    const q = new URLSearchParams({task_id: taskId, after_seq: String(afterSeq)})
    return request(`/api/step/${encodeURIComponent(stepId)}/messages?${q.toString()}`)
  },

  monitorControl(taskId: string, action: 'stop' | 'resume', message: string = '', stepId?: string): Promise<{ status: string; task_id: string; action: string }> {
    return request(`/api/monitor/control`, json('POST', {task_id: taskId, action, message, step_id: stepId}))
  },

  deleteTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}`, json('DELETE'))
  },

  getStep(taskId: string, stepId: string, opts?: { limit?: number; beforeSeq?: number }): Promise<StepData> {
    const q = new URLSearchParams({task_id: taskId})
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.beforeSeq != null) q.set('before_seq', String(opts.beforeSeq))
    return request<StepData>(`/api/step/${encodeURIComponent(stepId)}?${q.toString()}`)
  },

  stepIntervene(taskId: string, stepId: string, mode: 'send' | 'force_inject' | 'stop', content: string): Promise<unknown> {
    return request<unknown>('/api/intervene/step', json('POST', {task_id: taskId, step_id: stepId, intervention_type: mode, message: content}))
  },

  flowIntervene(taskId: string, mode: 'pending' | 'immediate' | 'rebuild', content: string, stepId?: string): Promise<unknown> {
    return request<unknown>('/api/intervene/flow', json('POST', {task_id: taskId, mode, reason: content, ...(stepId ? {step_id: stepId} : {})}))
  },

  approveGate(taskId: string, stepId: string, reason: string = ''): Promise<unknown> {
    return request<unknown>('/api/step/advance', json('POST', {task_id: taskId, step_id: stepId, decision: 'approved', reason}))
  },

  rejectGate(taskId: string, stepId: string, reason: string): Promise<unknown> {
    return request<unknown>('/api/step/advance', json('POST', {task_id: taskId, step_id: stepId, decision: 'rejected', reason}))
  },

  resumeStep(taskId: string, stepId: string): Promise<unknown> {
    return request<unknown>('/api/step/resume', json('POST', {task_id: taskId, step_id: stepId}))
  },

  compressStep(taskId: string, stepId: string): Promise<{
    status: string; reason?: string; count?: number;
    original_count?: number; compressed_count?: number;
  }> {
    return request('/api/step/compress', json('POST', {task_id: taskId, step_id: stepId}))
  },

  getConfig(): Promise<ConfigView> {
    return request<ConfigView>('/api/config')
  },

  saveConfig(cfg: Partial<ConfigSave>): Promise<{ status: string }> {
    return request<{ status: string }>('/api/config', json('PUT', cfg))
  },

  testLlm(): Promise<{ ok: boolean; error?: string; model?: string }> {
    return request<{ ok: boolean; error?: string; model?: string }>('/api/config/test-llm', json('POST'))
  },

  fsTree(path?: string, opts?: { recursive?: boolean }): Promise<FsTree> {
    const params = new URLSearchParams()
    if (path) params.set('path', path)
    if (opts?.recursive) params.set('recursive', 'true')
    const q = params.toString()
    return request<FsTree>(`/api/fs/tree${q ? `?${q}` : ''}`)
  },

  fsRead(path: string): Promise<FsFile> {
    return request<FsFile>(`/api/fs/file?path=${encodeURIComponent(path)}`)
  },

  fsWrite(path: string, content: string, baseMtime?: number): Promise<{ status: string; size: number }> {
    const body = baseMtime === undefined ? {path, content} : {path, content, baseMtime}
    return request<{ status: string; size: number }>('/api/fs/file', json('PUT', body))
  },
}
