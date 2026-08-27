import {
  DC_FULL_MARKER,
  DC_RETRY_PATTERN,
  type LlmErrorEvent,
  type RefreshDataEvent,
  type SseEvent,
  type StepStartEvent,
  type StreamChunkEvent,
  type StreamEndEvent,
  type ThinkingChunkEvent,
  type ToolCallParamEvent,
  type ToolCallResultEvent,
  type ToolCallStartEvent,
  type ToolExecutingEvent,
  type UserMessageEvent,
} from './types'

/** per-task lastSeq（C3：Map<taskId, number>，禁止模块级单值） */
const lastSeqRef = new Map<string, number>()

/** 读取某 task 已收到的最大 seq（重连参数与测试断言用） */
export function getLastSeq(taskId: string): number {
  return lastSeqRef.get(taskId) ?? 0
}

/** useStepStream 的事件回调集合（消费方 = SWP4-C 面板） */
export interface StepStreamHandlers {
  onStepStart?: (e: StepStartEvent) => void
  onChunk?: (e: StreamChunkEvent) => void
  onStreamEnd?: (e: StreamEndEvent) => void
  onToolStart?: (e: ToolCallStartEvent) => void
  /** V-18：工具参数流式增量（逐字动画） */
  onToolParam?: (e: ToolCallParamEvent) => void
  /** V-18：工具开始执行（参数流结束后） */
  onToolExecuting?: (e: ToolExecutingEvent) => void
  onToolResult?: (e: ToolCallResultEvent) => void
  onUserMessage?: (e: UserMessageEvent) => void
  onLlmError?: (e: LlmErrorEvent) => void
  onRefresh?: (e: RefreshDataEvent) => void
  /** __DC_FULL__ 全量重拉触发（旧 __DC_FULL__ 语义） */
  onFullRerender?: (e: StreamChunkEvent) => void
  /** __DC_RETRY__N/10__DC_RETRY__：重试进度（宽度 = N*10%，文案「限流重试 N/10」） */
  onRetry?: (e: StreamChunkEvent, retryCount: number) => void
  /** 后续不含 RETRY 标记的 chunk 或 streamEnd 时清除进度条 */
  onRetryClear?: () => void
  /** thinkingChunk：思考过程流（仅展示、不落盘） */
  onThinkingChunk?: (e: ThinkingChunkEvent) => void
}

export interface StepStream {
  connect(lastSeq?: number): void

  close(): void

  /** 设置当前激活步骤；null 期间事件入 pendingEvents，设置后按序回放 */
  setActiveStep(stepId: string | null): void
}

const RECONNECT_DELAY_MS = 3000

/** 介入消息内联标记：__DC_USER_MSG__{json}__DC_USER_MSG__（解析为 userMessage 事件，
 *  不追加到 AI 文本——修复历史「用户消息串进 assistant 流」污染问题） */
export const DC_USER_MSG_PATTERN = /__DC_USER_MSG__(.*?)__DC_USER_MSG__/s

/** 从 chunk 中提取内联用户消息 JSON（无标记返回 null） */
export function parseInlineUserMessage(chunk: string): { stepId: string; content: string } | null {
  const m = chunk.match(DC_USER_MSG_PATTERN)
  if (!m) return null
  try {
    const obj = JSON.parse(m[1])
    if (obj && typeof obj.stepId === 'string' && typeof obj.content === 'string') {
      return {stepId: obj.stepId, content: obj.content}
    }
  } catch {
    // 解析失败按普通 chunk 忽略
  }
  return null
}

/** 创建一条 per-task 的 SSE 流（§3.4 唯一实现；useStepStream 的底层内核）
 *
 * 2026-08-27 修订：移除 getSkipBelow（曾按 getStep.max_seq 跳过内容事件防补发
 * 重放）——max_seq 是 DB 消息级 seq，与 SSE 事件级 seq 不同源（服务重启后事件
 * seq 从 0 重新计数）→ 重启后实时事件全部被误杀（页面不更新）。补发过滤由后端
 * sse_hub.subscribe 的 should_skip（仅补发阶段）负责。 */
export function createStepStream(
    taskId: string,
    handlers: StepStreamHandlers,
    matchStepId?: (eventStepId: string) => boolean,
): StepStream {
  let es: EventSource | null = null
  let closed = false
  let reconnectTimer: number | null = null
  let activeStepId: string | null = null
  let retryVisible = false
  const pendingEvents: SseEvent[] = []
  const renderedSeqs = new Set<number>()
  const renderedCallIds = new Set<string>()

  /** M10：事件按 stepId 过滤，默认 stepId 相等（llmError 除外，N10） */
  function stepMatches(stepId: string): boolean {
    if (matchStepId) return matchStepId(stepId)
    return activeStepId != null && stepId === activeStepId
  }

  function process(ev: SseEvent): void {
    // seq 幂等（审查五第 12 条）：已渲染的事件跳过（防重连重放重复渲染）
    if (renderedSeqs.has(ev.seq)) return
    renderedSeqs.add(ev.seq)

    // 2026-08-24（用户反馈：恢复后进度条不消失）：重试成功后任意非 marker 事件
    // （思考流/工具轮/文本/流结束）都清除进度条——此前只认文本 chunk 与 streamEnd，
    // 恢复后先输出 thinking 或直接工具调用（无文本）时进度条一直残留到该轮结束
    // （用户观察：成功几个请求/一段时间后才消失）
    const isRetryMarker = ev.command === 'streamChunk' && DC_RETRY_PATTERN.test(ev.chunk)
    if (!isRetryMarker && retryVisible) {
      retryVisible = false
      handlers.onRetryClear?.()
    }

    switch (ev.command) {
      case 'stepStart':
        handlers.onStepStart?.(ev)
        break
      case 'streamChunk': {
        // __DC_USER_MSG__：介入消息内联标记 → 转 userMessage 事件（不追加 AI 文本）
        const inline = parseInlineUserMessage(ev.chunk)
        if (inline) {
          handlers.onUserMessage?.({command: 'userMessage', taskId: ev.taskId, seq: ev.seq, stepId: inline.stepId, message: inline.content})
          break
        }
        // __DC_FULL__：触发全量重渲染（期间的 chunk 同样计入 lastSeq，见 handleEvent）
        if (ev.chunk.includes(DC_FULL_MARKER)) {
          handlers.onFullRerender?.(ev)
          break
        }
        // __DC_RETRY__N/10__DC_RETRY__（C1/E1：旧代码同款格式）
        const m = ev.chunk.match(DC_RETRY_PATTERN)
        if (m) {
          retryVisible = true
          handlers.onRetry?.(ev, Number(m[1]))
          break
        }
        // 清除已统一在 process 开头（任意非 marker 事件）
        handlers.onChunk?.(ev)
        break
      }
      case 'streamEnd':
        handlers.onStreamEnd?.(ev)
        break
      case 'toolCallStart':
        // callId 去重：已渲染的工具卡不重复插入
        if (renderedCallIds.has(ev.callId)) break
        renderedCallIds.add(ev.callId)
        handlers.onToolStart?.(ev)
        break
      case 'toolCallParam':
        handlers.onToolParam?.(ev as ToolCallParamEvent)
        break
      case 'toolExecuting':
        handlers.onToolExecuting?.(ev as ToolExecutingEvent)
        break
      case 'toolCallResult':
        handlers.onToolResult?.(ev)
        break
      case 'userMessage':
        handlers.onUserMessage?.(ev)
        break
      case 'thinkingChunk':
        handlers.onThinkingChunk?.(ev as ThinkingChunkEvent)
        break
      case 'llmError':
        handlers.onLlmError?.(ev)
        break
      case 'refreshData':
        handlers.onRefresh?.(ev)
        break
    }
  }

  function handleEvent(ev: SseEvent): void {
    // 每条事件记录 ev.seq 到 lastSeqRef（__DC_FULL__ 全量重拉期间的 chunk 同样计入）
    lastSeqRef.set(taskId, ev.seq)

    // 竞态保护：当前无 activeStepId 时收到的事件入 pendingEvents，激活后按序回放
    if (activeStepId == null) {
      pendingEvents.push(ev)
      return
    }

    // M10：按 stepId 过滤（llmError 除外）；不匹配的事件不入渲染
    const evStepId = (ev as { stepId?: string }).stepId
    if (evStepId != null && ev.command !== 'llmError' && !stepMatches(evStepId)) return

    process(ev)
  }

  function connect(lastSeq?: number): void {
    if (closed) return
    if (es) {
      es.close()
      es = null
    }
    const ls = lastSeq ?? lastSeqRef.get(taskId) ?? 0
    const url = `/sse?taskId=${encodeURIComponent(taskId)}&lastSeq=${ls}`
    es = new EventSource(url)
    es.onmessage = (msg: MessageEvent<string>) => {
      let ev: SseEvent
      try {
        ev = JSON.parse(msg.data) as SseEvent
      } catch {
        return // 忽略非法 data 行
      }
      handleEvent(ev)
    }
    es.onerror = () => {
      // 断线：关闭后 3s 手动重建（原生自动重连无法携带新 lastSeq，P0-3）
      if (es) {
        es.close()
        es = null
      }
      if (closed) return
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        connect()
      }, RECONNECT_DELAY_MS)
    }
  }

  function close(): void {
    closed = true
    if (reconnectTimer != null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (es) {
      es.close()
      es = null
    }
  }

  function setActiveStep(stepId: string | null): void {
    activeStepId = stepId
    if (stepId == null || pendingEvents.length === 0) return
    // loadStep 后按序回放（旧 _pendingChunks 等价物）
    const queued = pendingEvents.splice(0, pendingEvents.length)
    for (const ev of queued) {
      if (renderedSeqs.has(ev.seq)) continue
      const evStepId = (ev as { stepId?: string }).stepId
      if (evStepId != null && ev.command !== 'llmError' && !stepMatches(evStepId)) continue
      process(ev)
    }
  }

  return {connect, close, setActiveStep}
}

// ═══════════════════════════════════════════════════════════════════
// 编排流（v2：创建流程时的 AI 思考实时展示）
//
// 频道：plan:<uuid>（rest_api create_task planner 分支广播 planStart/
// planChunk/planDone）。短生命周期（创建请求返回即关闭），不做重连；
// POST /api/task 的 finally 兜底关闭。
// ═══════════════════════════════════════════════════════════════════

export interface PlanStreamHandlers {
  /** planChunk：AI 编排输出的文本片 */
  onChunk?: (text: string) => void
  /** planDone：编排结束（成功/失败） */
  onDone?: () => void
}

export interface PlanStream {
  connect(): void

  close(): void
}

export function createPlanStream(channel: string, handlers: PlanStreamHandlers): PlanStream {
  let es: EventSource | null = null
  let closed = false

  function connect(): void {
    if (closed) return
    es = new EventSource(`/sse?taskId=${encodeURIComponent(channel)}&lastSeq=0`)
    es.onmessage = (msg: MessageEvent<string>) => {
      let ev: SseEvent
      try {
        ev = JSON.parse(msg.data) as SseEvent
      } catch {
        return
      }
      if (ev.command === 'planChunk' && typeof (ev as { text?: string }).text === 'string') {
        handlers.onChunk?.((ev as { text: string }).text)
      } else if (ev.command === 'planDone') {
        handlers.onDone?.()
      }
    }
    // 断线不重连：创建请求本身是兜底（POST 返回即关闭）
    es.onerror = () => {
      es?.close()
      es = null
    }
  }

  function close(): void {
    closed = true
    es?.close()
    es = null
  }

  return {connect, close}
}
