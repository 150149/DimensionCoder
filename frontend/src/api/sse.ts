import {
  type GateApprovalReadyEvent,
  type LlmErrorEvent,
  type MessagesConfirmedEvent,
  type MessagesResetEvent,
  type RetryProgressEvent,
  type RoundAbortedEvent,
  type SseEvent,
  type StepStartEvent,
  type StepStatsUpdatedEvent,
  type StepStatusEvent,
  type StepTokensUpdatedEvent,
  type StreamChunkEvent,
  type StreamEndEvent,
  type TaskUpdatedEvent,
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
  onRetryProgress?: (e: RetryProgressEvent) => void
  onRetryClear?: () => void
  onRoundAborted?: (e: RoundAbortedEvent) => void
  onThinkingChunk?: (e: ThinkingChunkEvent) => void
  onMessagesConfirmed?: (e: MessagesConfirmedEvent) => void
  onTaskUpdated?: (e: TaskUpdatedEvent) => void
  onStepStatus?: (e: StepStatusEvent) => void
  onStepTokensUpdated?: (e: StepTokensUpdatedEvent) => void
  onStepStatsUpdated?: (e: StepStatsUpdatedEvent) => void
  onGateApprovalReady?: (e: GateApprovalReadyEvent) => void
  onMessagesReset?: (e: MessagesResetEvent) => void
  onReconnect?: () => void
}

export interface ConnectOpts {
  stepId?: string
  getLastMsgSeq?: () => number
}

export interface StepStream {
  connect(lastSeq?: number, opts?: ConnectOpts): void

  close(): void

  setActiveStep(stepId: string | null): void
}

const RECONNECT_DELAY_MS = 3000

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
  let hadOpen = false
  let lastOpts: ConnectOpts | undefined
  const pendingEvents: SseEvent[] = []

  function stepMatches(stepId: string): boolean {
    if (matchStepId) return matchStepId(stepId)
    return activeStepId != null && stepId === activeStepId
  }

  function process(ev: SseEvent): void {
    const isRetryEvent = ev.command === 'retryProgress'
    if (!isRetryEvent && retryVisible) {
      retryVisible = false
      handlers.onRetryClear?.()
    }

    switch (ev.command) {
      case 'stepStart':
        handlers.onStepStart?.(ev)
        break
      case 'streamChunk':
        handlers.onChunk?.(ev)
        break
      case 'retryProgress':
        retryVisible = true
        handlers.onRetryProgress?.(ev as RetryProgressEvent)
        break
      case 'roundAborted':
        handlers.onRoundAborted?.(ev as RoundAbortedEvent)
        break
      case 'streamEnd':
        handlers.onStreamEnd?.(ev)
        break
      case 'toolCallStart':
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
      case 'messagesConfirmed':
        handlers.onMessagesConfirmed?.(ev as MessagesConfirmedEvent)
        break
      case 'taskUpdated':
        handlers.onTaskUpdated?.(ev as TaskUpdatedEvent)
        break
      case 'stepTokensUpdated':
        handlers.onStepTokensUpdated?.(ev as StepTokensUpdatedEvent)
        break
      case 'stepStatsUpdated':
        handlers.onStepStatsUpdated?.(ev as StepStatsUpdatedEvent)
        break
      case 'gateApprovalReady':
        handlers.onGateApprovalReady?.(ev as GateApprovalReadyEvent)
        break
      case 'messagesReset':
        handlers.onMessagesReset?.(ev as MessagesResetEvent)
        break
      case 'stepStatus':
        handlers.onStepStatus?.(ev as StepStatusEvent)
        break
      case 'thinkingChunk':
        handlers.onThinkingChunk?.(ev as ThinkingChunkEvent)
        break
      case 'llmError':
        handlers.onLlmError?.(ev)
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

  function connect(lastSeq?: number, opts?: ConnectOpts): void {
    if (closed) return
    if (es) {
      es.close()
      es = null
    }
    if (opts) lastOpts = opts
    const ls = lastSeq ?? lastSeqRef.get(taskId) ?? 0
    const parts = [`taskId=${encodeURIComponent(taskId)}`, `lastSeq=${ls}`]
    if (opts?.stepId) parts.push(`stepId=${encodeURIComponent(opts.stepId)}`)
    const lms = opts?.getLastMsgSeq?.()
    if (lms != null) parts.push(`lastMsgSeq=${lms}`)
    const url = `/sse?${parts.join('&')}`
    es = new EventSource(url)
    es.onopen = () => {
      if (hadOpen) handlers.onReconnect?.()
      hadOpen = true
    }
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
        connect(undefined, lastOpts)
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
