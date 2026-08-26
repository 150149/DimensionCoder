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

const lastSeqRef = new Map<string, number>()

export function getLastSeq(taskId: string): number {
  return lastSeqRef.get(taskId) ?? 0
}

export interface StepStreamHandlers {
  onStepStart?: (e: StepStartEvent) => void
  onChunk?: (e: StreamChunkEvent) => void
  onStreamEnd?: (e: StreamEndEvent) => void
  onToolStart?: (e: ToolCallStartEvent) => void

  onToolParam?: (e: ToolCallParamEvent) => void

  onToolExecuting?: (e: ToolExecutingEvent) => void
  onToolResult?: (e: ToolCallResultEvent) => void
  onUserMessage?: (e: UserMessageEvent) => void
  onLlmError?: (e: LlmErrorEvent) => void
  onRefresh?: (e: RefreshDataEvent) => void

  onFullRerender?: (e: StreamChunkEvent) => void

  onRetry?: (e: StreamChunkEvent, retryCount: number) => void

  onRetryClear?: () => void

  onThinkingChunk?: (e: ThinkingChunkEvent) => void
}

export interface StepStream {
  connect(lastSeq?: number): void

  close(): void

  setActiveStep(stepId: string | null): void
}

const RECONNECT_DELAY_MS = 3000

export const DC_USER_MSG_PATTERN = /__DC_USER_MSG__(.*?)__DC_USER_MSG__/s

export function parseInlineUserMessage(chunk: string): { stepId: string; content: string } | null {
  const m = chunk.match(DC_USER_MSG_PATTERN)
  if (!m) return null
  try {
    const obj = JSON.parse(m[1])
    if (obj && typeof obj.stepId === 'string' && typeof obj.content === 'string') {
      return {stepId: obj.stepId, content: obj.content}
    }
  } catch {

  }
  return null
}

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

  function stepMatches(stepId: string): boolean {
    if (matchStepId) return matchStepId(stepId)
    return activeStepId != null && stepId === activeStepId
  }

  function process(ev: SseEvent): void {

    if (renderedSeqs.has(ev.seq)) return
    renderedSeqs.add(ev.seq)

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

        const inline = parseInlineUserMessage(ev.chunk)
        if (inline) {
          handlers.onUserMessage?.({command: 'userMessage', taskId: ev.taskId, seq: ev.seq, stepId: inline.stepId, message: inline.content})
          break
        }

        if (ev.chunk.includes(DC_FULL_MARKER)) {
          handlers.onFullRerender?.(ev)
          break
        }

        const m = ev.chunk.match(DC_RETRY_PATTERN)
        if (m) {
          retryVisible = true
          handlers.onRetry?.(ev, Number(m[1]))
          break
        }

        handlers.onChunk?.(ev)
        break
      }
      case 'streamEnd':
        handlers.onStreamEnd?.(ev)
        break
      case 'toolCallStart':

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

    lastSeqRef.set(taskId, ev.seq)

    if (activeStepId == null) {
      pendingEvents.push(ev)
      return
    }

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
        return
      }
      handleEvent(ev)
    }
    es.onerror = () => {

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

export interface PlanStreamHandlers {

  onChunk?: (text: string) => void

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
