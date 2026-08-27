// ═══════════════════════════════════════════════════════════════
// API 客户端（SWP4-A / WP4-2 T4.2）
// - baseURL 同源（dev 经 Vite 代理指向 8501，生产由 Python 静态托管同源）
// - 无鉴权头（本期不做令牌，WP4-1 §3.1 无登录守卫）
// - 错误处理约定（第 7 轮 B5 统一）：非 2xx 抛 ApiError{status, message}，
//   message 提取链固定：body.error → body.detail → `HTTP ${status}`
// - 禁止：client 内不做重试；不加任何鉴权头（WP4-2 T4.2 禁止）
// ═══════════════════════════════════════════════════════════════

import type {ConfigSave, ConfigView, CreateTaskResult, FsBrowse, FsFile, FsTree, Message, MonitorConversations, StepData, TaskDetail, TaskOverview,} from './types'

/** 统一错误类型：status=0 表示网络层错误（原始 TypeError 被收敛，B5） */
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const BASE_URL = '' // 同源

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
    // 网络错误：吞掉原生 TypeError，统一为 ApiError（调用方如 useTaskPolling 静默）
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
      // 非 JSON 响应体：保留 HTTP 兜底文案
    }
    throw new ApiError(res.status, message)
  }

  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function json(method: string, body?: unknown): RequestInit {
  return {method, body: body === undefined ? undefined : JSON.stringify(body)}
}

/** WP4-2 §3.3 方法清单（签名固定，禁止改动） */
export const api = {
  // 端点 4
  listTasks(): Promise<TaskOverview> {
    return request<TaskOverview>('/api/tasks')
  },
  // 端点 3（无显式 task_type/steps + description → planner 动态规划）
  // 2026-08-26（用户需求：创建流程可选工作目录）：extra.workspace_dir 非空 =
  // 自定义工作区（AI 相对路径基准）；缺省 = 系统自动分配 workspace/<tid>/
  createTask(description: string, extra?: { workspace_dir?: string }): Promise<CreateTaskResult> {
    return request<CreateTaskResult>('/api/task', json('POST', {description, ...(extra ?? {})}))
  },
  // 端点 31（V2「立即启动」唯一通道；paused 任务继续亦走此端点，J3）
  startTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}/start`, json('POST'))
  },
  // 尽力模式开关（2026-08-16 用户需求）：开启后 gate 审批自动放行 + 防放弃提醒 + 收尾复核
  setBestEffort(taskId: string, enabled: boolean): Promise<{ status: string; best_effort: boolean }> {
    return request<{ status: string; best_effort: boolean }>(
        `/api/task/${encodeURIComponent(taskId)}/best-effort`,
        json('POST', {enabled}),
    )
  },
  // 优雅重启/关闭（admin）：restart（默认）/ shutdown / ''（取消排空）
  gracefulRestart(action: 'restart' | 'shutdown' | '' = 'restart'): Promise<{ status: string; action: string; message: string }> {
    return request<{ status: string; action: string; message: string }>(
        '/api/admin/graceful-restart',
        json('POST', {action}),
    )
  },
  // 端点 30（J3：任务级「暂停」，pause_level="user"）
  pauseTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}/pause`, json('POST', {pause_level: 'user'}))
  },
  // 端点 5
  getTask(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(`/api/task/${encodeURIComponent(taskId)}`)
  },
  // 端点 29（A2：MonitorDetail 数据源）
  getMonitorConversations(taskId: string): Promise<MonitorConversations> {
    return request<MonitorConversations>(`/api/task/${encodeURIComponent(taskId)}/monitor-conversations`)
  },
  // 端点 27（MonitorDetail 实时消息源：get_step_messages 不校验步骤归属，
  // 虚拟步骤 _monitor/_review 等可直接查询——2026-08-20 实时聊天）
  getStepMessages(taskId: string, stepId: string, afterSeq: number = -1): Promise<{ messages: Message[]; max_seq: number; after_seq: number }> {
    const q = new URLSearchParams({task_id: taskId, after_seq: String(afterSeq)})
    return request(`/api/step/${encodeURIComponent(stepId)}/messages?${q.toString()}`)
  },
  // Monitor 页控制（2026-08-20：暂停/恢复当前 Monitor 输出；stepId 可选——
  // 多实例：stop/resume 精确到当前实例（缺省后端找 active 实例/介入实例））
  monitorControl(taskId: string, action: 'stop' | 'resume', message: string = '', stepId?: string): Promise<{ status: string; task_id: string; action: string }> {
    return request(`/api/monitor/control`, json('POST', {task_id: taskId, action, message, step_id: stepId}))
  },
  // 端点 6
  deleteTask(taskId: string): Promise<{ status: string }> {
    return request<{ status: string }>(`/api/task/${encodeURIComponent(taskId)}`, json('DELETE'))
  },
  // 端点 32（J1：StepDetail 数据源聚合端点，禁止前端多端点拼装）
  // opts：分页加载——limit 默认 200（最近 N 条，历史折叠）；beforeSeq 往前翻页
  getStep(taskId: string, stepId: string, opts?: { limit?: number; beforeSeq?: number }): Promise<StepData> {
    const q = new URLSearchParams({task_id: taskId})
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.beforeSeq != null) q.set('before_seq', String(opts.beforeSeq))
    return request<StepData>(`/api/step/${encodeURIComponent(stepId)}?${q.toString()}`)
  },
  // 端点 23
  stepIntervene(taskId: string, stepId: string, mode: 'send' | 'force_inject' | 'stop', content: string): Promise<unknown> {
    return request<unknown>('/api/intervene/step', json('POST', {task_id: taskId, step_id: stepId, intervention_type: mode, message: content}))
  },
  // 端点 24（C2：content 作为 reason 提交，必填；rebuild=2026-08-20
  // Monitor 已完成时发送：清理后续步骤消息/产出 + 重跑 Monitor；
  // stepId（可选）——rebuild 时传当前触发步骤（步骤已完成但任务未完成）
  flowIntervene(taskId: string, mode: 'pending' | 'immediate' | 'rebuild', content: string, stepId?: string): Promise<unknown> {
    return request<unknown>('/api/intervene/flow', json('POST', {task_id: taskId, mode, reason: content, ...(stepId ? {step_id: stepId} : {})}))
  },
  // 端点 20（2026-08-20：reason 可选——选项类 gate 的决策内容，后端追加进
  // summary 供后续步骤可见）
  approveGate(taskId: string, stepId: string, reason: string = ''): Promise<unknown> {
    return request<unknown>('/api/step/advance', json('POST', {task_id: taskId, step_id: stepId, decision: 'approved', reason}))
  },
  // 端点 20
  rejectGate(taskId: string, stepId: string, reason: string): Promise<unknown> {
    return request<unknown>('/api/step/advance', json('POST', {task_id: taskId, step_id: stepId, decision: 'rejected', reason}))
  },
  // 端点 22
  resumeStep(taskId: string, stepId: string): Promise<unknown> {
    return request<unknown>('/api/step/resume', json('POST', {task_id: taskId, step_id: stepId}))
  },
  // 端点 21（2026-08-20：返回类型补充——Monitor 页压缩按钮判断 skipped）
  compressStep(taskId: string, stepId: string): Promise<{
    status: string; reason?: string; count?: number;
    original_count?: number; compressed_count?: number;
  }> {
    return request('/api/step/compress', json('POST', {task_id: taskId, step_id: stepId}))
  },
  // WP3 §2.2：GET /api/config
  getConfig(): Promise<ConfigView> {
    return request<ConfigView>('/api/config')
  },
  // WP3 §2.2：PUT /api/config（apiKey 空串保留旧值，后端规则）
  saveConfig(cfg: Partial<ConfigSave>): Promise<{ status: string }> {
    return request<{ status: string }>('/api/config', json('PUT', cfg))
  },
  // WP3 §2.2：POST /api/config/test-llm
  testLlm(): Promise<{ ok: boolean; error?: string; model?: string }> {
    return request<{ ok: boolean; error?: string; model?: string }>('/api/config/test-llm', json('POST'))
  },
  // WP3 §2.2：GET /api/fs/tree（opts.recursive=true → query recursive=true，
  // 后端契约 rest_api.py:1348 fs_tree(path, recursive) query 参数）
  fsTree(path?: string, opts?: { recursive?: boolean }): Promise<FsTree> {
    const params = new URLSearchParams()
    if (path) params.set('path', path)
    if (opts?.recursive) params.set('recursive', 'true')
    const q = params.toString()
    return request<FsTree>(`/api/fs/tree${q ? `?${q}` : ''}`)
  },
  // 目录浏览（2026-08-26 创建配置弹窗「选择文件夹」）：空 path → 盘符列表；
  // 绝对路径 → 下一级目录/文件（任意路径可浏览，本地单用户工具）
  fsBrowse(path: string): Promise<FsBrowse> {
    const params = new URLSearchParams()
    if (path) params.set('path', path)
    const q = params.toString()
    return request<FsBrowse>(`/api/fs/browse${q ? `?${q}` : ''}`)
  },
  // WP3 §2.2：GET /api/fs/file
  fsRead(path: string): Promise<FsFile> {
    return request<FsFile>(`/api/fs/file?path=${encodeURIComponent(path)}`)
  },
  // WP3 §2.2：PUT /api/fs/file（baseMtime 乐观锁，V-11；第三参有值才携带，
  // 现有调用方零改动——向后兼容）
  fsWrite(path: string, content: string, baseMtime?: number): Promise<{ status: string; size: number }> {
    const body = baseMtime === undefined ? {path, content} : {path, content, baseMtime}
    return request<{ status: string; size: number }>('/api/fs/file', json('PUT', body))
  },
}
