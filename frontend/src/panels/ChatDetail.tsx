import {Fragment, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {Navigate, useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {
  LlmErrorEvent,
  StepData,
  StepLive,
  StepStatusEvent,
  StreamChunkEvent,
  TaskSummary,
  TaskUpdatedEvent,
  ThinkingChunkEvent,
  ToolCallParamEvent,
  ToolCallResultEvent,
  ToolCallStartEvent,
  UserMessageEvent,
} from '../api/types'
import ChatMessage from '../components/ChatMessage'
import {Markdown} from '../components/Markdown'
import ConfirmDialog from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ToolCard from '../components/ToolCard'
import {ContextMeter, TokenDetails, TokenMetrics} from '../components/TokenUsageBar'
import {Icon} from '../components/icons'
import {useMessageStore} from '../hooks/useMessageStore'
import {useConfirmedViews} from '../hooks/useConfirmedViews'
import {useToolExpanded} from '../hooks/useToolExpanded'
import {useStepStream} from '../hooks/useStepStream'
import {useStepMetrics} from '../hooks/useStepMetrics'
import {useChatAutoScroll} from '../hooks/useChatAutoScroll'
import {useChatWindow} from '../hooks/useChatWindow'
import {extractAskUserPackage} from '../utils/decisionPkg'
import {compressResultText} from '../utils/compressResult'
import CodeTab from '../editor/CodeTab'

interface ChatDetailProps {
  taskId: string
}

const STEP_ID = 'chat'

export function mergeEarlierPage(prev: StepData, page: StepData): StepData {
  return {
    ...prev,
    messages: [...page.messages, ...prev.messages],
    conversation: [...page.messages, ...prev.conversation],
    total: page.total,
    truncated: page.truncated,
    max_seq: Math.max(prev.max_seq ?? 0, page.max_seq ?? 0),
  }
}

export default function ChatDetail({taskId}: ChatDetailProps) {
  const navigate = useNavigate()
  const [data, setData] = useState<StepData | null>(null)
  const [task, setTask] = useState<TaskSummary | null>(null)
  const [taskGone, setTaskGone] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, settleRound, settleStep, mergeBackend, resetAll, noteActivity,
  } = useStepMetrics()
  const localMsgIdRef = useRef(0)
  const toolExecutingRef = useRef(false)
  const stepStatusRef = useRef('')
  const [llmError, setLlmError] = useState<LlmErrorEvent | null>(null)
  const [retryProgress, setRetryProgress] = useState<{ count: number, total: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [expandedTools, setExpandedTools] = useState<Set<string>>(() => new Set())
  const store = useMessageStore()
  const storeRef = useRef(store)
  storeRef.current = store

  const absorbLive = useCallback((l: StepLive | null | undefined) => {
    if (!l) return
    setStreaming(l.streaming === true)
    const up = storeRef.current.upsertPending
    const rid = l.roundUid ?? 'live'
    if (l.thinking) up({entityId: rid, kind: 'thinking', role: 'assistant', content: l.thinking})
    if (l.text) up({entityId: rid, kind: 'text', role: 'assistant', content: l.text})
    for (const t of l.tools ?? []) {
      up({entityId: t.callId, kind: 'tool', role: 'tool', toolName: t.name, input: t.input, output: t.output ?? undefined, status: t.status, roundUid: l.roundUid ?? undefined})
    }
  }, [])

  const [view, setView] = useState<'chat' | 'code'>('chat')
  const [editorDirty, setEditorDirty] = useState(false)
  const [externalModified, setExternalModified] = useState<string[]>([])
  const handleExternalModified = useCallback((path: string, modified: boolean) => {
    setExternalModified((prev) =>
        modified ? Array.from(new Set([...prev, path])) : prev.filter((p) => p !== path),
    )
  }, [])

  const [askSelected, setAskSelected] = useState<number | null>(null)
  const [askAnswer, setAskAnswer] = useState('')
  const [askUserEvt, setAskUserEvt] = useState<{
    question: string; options?: string[]; callId: string
  } | null>(null)

  const [contextWindow, setContextWindow] = useState(1_048_576)
  const [lightPrices, setLightPrices] = useState({in: 0, cached: 0, out: 0})
  const [powerPrices, setPowerPrices] = useState({in: 0, cached: 0, out: 0})
  useEffect(() => {
    let alive = true
    api
        .getConfig()
        .then((cfg) => {
          if (alive) {
            setContextWindow(cfg.contextWindow ?? 1_048_576)
            setLightPrices({in: cfg.lightInputPrice ?? 0, cached: cfg.lightCachedPrice ?? 0, out: cfg.lightOutputPrice ?? 0})
            setPowerPrices({in: cfg.powerInputPrice ?? 0, cached: cfg.powerCachedPrice ?? 0, out: cfg.powerOutputPrice ?? 0})
          }
        })
        .catch(() => {
        })
    return () => {
      alive = false
    }
  }, [])

  const loadStep = useCallback(async (): Promise<StepData | null> => {
    let lastErr: unknown
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const d = await api.getStep(taskId, STEP_ID, {limit: 200})
        setData(d)
        storeRef.current.loadPage((d.conversation ?? []) as unknown[], true)
        absorbLive(d.live)
        if (d.pendingQuestion) {
          setAskUserEvt({
            question: d.pendingQuestion.question,
            options: d.pendingQuestion.options,
            callId: '',
          })
        }
        if (!d.live && d.step?.status === 'active') {
          setStreaming(true)
        }
        return d
      } catch (err) {
        lastErr = err
        if (attempt < 4) {
          await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt))
        }
      }
    }
    setToast(lastErr instanceof Error ? lastErr.message : '消息加载失败，请刷新重试')
    return null
  }, [taskId, absorbLive])
  const [loadingMore, setLoadingMore] = useState(false)
  const loadEarlier = useCallback(() => {
    const msgs = data?.messages
    const oldest = msgs && msgs.length ? msgs[0].seq : undefined
    if (oldest == null || loadingMore) return
    setLoadingMore(true)
    api
        .getStep(taskId, STEP_ID, {limit: 200, beforeSeq: oldest})
        .then((d) => {
          setData((prev) => (prev ? mergeEarlierPage(prev, d) : prev))
          storeRef.current.prependPage(((d.conversation ?? d.messages) ?? []) as unknown[])
        })
        .finally(() => setLoadingMore(false))
  }, [data, loadingMore, taskId])
  useEffect(() => {
    loadStep()
  }, [loadStep])

  const loadMeta = useCallback(() => api.getTask(taskId).then((d) => {
    setTask(d.task)
    const step = d.task?.steps?.find((s) => s.step_id === STEP_ID)
    mergeBackend(step)
  }).catch(() => {
    setTaskGone(true)
  }), [taskId, mergeBackend])
  useEffect(() => {
    loadMeta()
  }, [loadMeta])

  const finalizeStep = useCallback(async () => {
    const st = storeRef.current
    if (!st.messages.some((m) => m.state === 'pending' && m.kind === 'tool')) return
    if (st.lastSeq >= 0) {
      try {
        const d = await api.getStepMessages(taskId, STEP_ID, {afterSeq: st.lastSeq, limit: 200})
        storeRef.current.loadPage((d.messages ?? []) as unknown[])
      } catch {
        return
      }
    }
    storeRef.current.clearPending('thinking')
    storeRef.current.clearPending('tool')
  }, [taskId])

  const curStepStatus = task?.steps?.find((s) => s.step_id === STEP_ID)?.status ?? ''
  useEffect(() => {
    const prev = stepStatusRef.current
    stepStatusRef.current = curStepStatus
    if (!prev || prev === curStepStatus) return
    if (curStepStatus === 'pending' || curStepStatus === 'active') return
    const t = setTimeout(() => {
      void finalizeStep()
    }, 1500)
    return () => clearTimeout(t)
  }, [curStepStatus, finalizeStep])

  const handleTaskUpdated = useCallback((e: TaskUpdatedEvent) => {
    setTask((prev) => {
      if (!prev) {
        void loadMeta()
        return prev
      }
      const steps = prev.steps?.map((s) => {
        const patch = e.steps.find((p) => p.stepId === s.step_id)
        return patch ? {...s, status: patch.status as typeof s.status} : s
      })
      return {
        ...prev,
        status: (e.status ?? prev.status) as typeof prev.status,
        pause_level: (e.pauseLevel ?? prev.pause_level) as typeof prev.pause_level,
        steps,
      }
    })
  }, [loadMeta])

  const handleStepStatus = useCallback((e: StepStatusEvent) => {
    setTask((prev) => {
      if (!prev) return prev
      const steps = prev.steps?.map((s) =>
          s.step_id === e.stepId
              ? {...s, status: (e.status ?? s.status) as typeof s.status}
              : s)
      return {...prev, steps}
    })
  }, [])

  const handleChunk = useCallback((e: StreamChunkEvent) => {
    noteActivity()
    sampleFirstChunk()
    accrueRoundComp(e.chunk.length)
    storeRef.current.upsertPending({entityId: e.entityId ?? 'live-text', kind: 'text', role: 'assistant', append: e.chunk})
  }, [sampleFirstChunk, accrueRoundComp, noteActivity])

  const handleStepStart = useCallback(() => {
    setStreaming(true)
    const at = Date.now()
    resetStep(at)
    setLlmError(null)
    storeRef.current.clearPending()
    toolExecutingRef.current = false
  }, [resetStep])

  const handleStreamEnd = useCallback(() => {
    setStreaming(false)
    const at = Date.now()
    endRound(at)
    storeRef.current.clearPending('thinking')
    toolExecutingRef.current = false
    void loadMeta()
    setTimeout(() => {
      void finalizeStep()
    }, 1500)
  }, [endRound, loadMeta, finalizeStep])

  const handleUserMessage = useCallback((e: UserMessageEvent) => {
    const st = storeRef.current
    const local = st.messages.find(
        (m) => m.state === 'pending' && m.role === 'user' && m.status === 'streaming' && m.content === e.message,
    )
    if (local?.entityId && local.entityId !== e.entityId) {
      st.dropPending(local.entityId)
    }
    st.upsertPending({
      entityId: e.entityId ?? (e.msgSeq != null ? `db:${e.msgSeq}` : `um-local-${e.seq}`),
      role: 'user',
      content: e.message,
    })
  }, [])

  const handleThinkingChunk = useCallback(
      (e: ThinkingChunkEvent) => {
        noteActivity()
        if (toolExecutingRef.current) return
        sampleFirstChunk()
        accrueRoundComp(e.chunk.length)
        storeRef.current.upsertPending({entityId: e.entityId ?? 'live-thinking', kind: 'thinking', role: 'assistant', append: e.chunk})
      },
      [sampleFirstChunk, accrueRoundComp, noteActivity],
  )

  const handleToolStart = useCallback(
      (e: ToolCallStartEvent) => {
        noteActivity()
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', toolName: e.toolName, input: e.input, status: 'streaming'})
      },
      [noteActivity],
  )

  const handleToolParam = useCallback(
      (e: ToolCallParamEvent) => {
        noteActivity()
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', inputAppend: e.delta})
        accrueRoundComp(e.delta.length)
      },
      [accrueRoundComp, noteActivity],
  )

  const handleToolExecuting = useCallback(() => {
    setStreaming(false)
    storeRef.current.clearPending('thinking')
    toolExecutingRef.current = true
    settleRound()
    noteActivity()
  }, [settleRound, noteActivity])

  const handleToolResult = useCallback(
      (e: ToolCallResultEvent) => {
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', output: e.output, status: 'done'})
        setStreaming(true)
        toolExecutingRef.current = false
        const req = metricRequestRef.current
        if (req) req.reqStartedAt = Date.now()
        noteActivity()
      },
      [noteActivity],
  )

  const handleLlmError = useCallback(
      (e: LlmErrorEvent) => {
        if (e.stepId === STEP_ID) setLlmError(e)
        setToast(e.message || 'LLM 错误')
        setStreaming(false)
        settleStep(Date.now())
      },
      [settleStep],
  )

  const handleRetry = useCallback(async () => {
    setBusy(true)
    try {
      await api.startTask(taskId)
      setLlmError(null)
      loadMeta()
    } catch (err) {
      setToast(err instanceof Error ? err.message : '重试失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId])

  useStepStream(taskId, STEP_ID, {
    onStepStart: handleStepStart,
    onChunk: handleChunk,
    onStreamEnd: handleStreamEnd,
    onToolStart: handleToolStart,
    onToolParam: handleToolParam,
    onToolExecuting: handleToolExecuting,
    onToolResult: handleToolResult,
    onUserMessage: handleUserMessage,
    onThinkingChunk: handleThinkingChunk,
    onLlmError: handleLlmError,
    onRetryProgress: (e) => setRetryProgress({count: e.retryCount, total: e.total}),
    onRetryClear: () => setRetryProgress(null),
    onRoundAborted: (e) => {
      if (e.roundUid) storeRef.current.dropPending(e.roundUid)
      for (const cid of e.toolCallIds ?? []) storeRef.current.dropPending(cid)
    },
    onTaskUpdated: handleTaskUpdated,
    onStepStatus: handleStepStatus,
    onStepStatsUpdated: (e) => {
      mergeBackend(e.stats)
      noteActivity()
    },
    onAskUser: (e) => setAskUserEvt({
      question: e.question, options: e.options, callId: e.callId
    }),
    onMessagesConfirmed: (e) => storeRef.current.confirm(e.items),
    onMessagesReset: () => {
      storeRef.current.reset()
      void loadStep()
    },
    onReconnect: () => {
      void loadMeta()
      if (stepStatusRef.current === 'active') void loadStep()
      else void finalizeStep()
    },
  }, undefined, () => storeRef.current.lastSeq)

  const doIntervene = useCallback(
      async (mode: 'send' | 'force_inject' | 'stop', content: string) => {
        setBusy(true)
        const isMsg = mode !== 'stop'
        const localId = `local-${Date.now()}-${++localMsgIdRef.current}`
        if (isMsg) storeRef.current.upsertPending({entityId: localId, role: 'user', content, status: 'streaming'})
        try {
          await api.stepIntervene(taskId, STEP_ID, mode, content, isMsg ? localId : undefined)
          if (mode === 'stop') {
            setStreaming(false)
          }
        } catch (err) {
          if (isMsg) storeRef.current.dropPending(localId)
          const msg = err instanceof Error ? err.message : ''
          if (mode === 'stop' && /cannot stop|not active/i.test(msg)) {
            setStreaming(false)
            void loadMeta()
            setToast('当前没有正在生成的回复，已刷新状态')
          } else {
            setToast(msg || '发送失败，请稍后重试')
          }
        } finally {
          setBusy(false)
        }
      },
      [taskId, loadMeta],
  )

  const handleCompress = useCallback(async () => {
    setBusy(true)
    try {
      const r = await api.compressStep(taskId, STEP_ID)
      setToast(compressResultText(r))
    } catch (err) {
      setToast(err instanceof Error ? err.message : '压缩失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId])

  const [confirmSwitch, setConfirmSwitch] = useState(false)
  const [switchBusy, setSwitchBusy] = useState(false)
  const handleSwitchToFlow = useCallback(async () => {
    setSwitchBusy(true)
    try {
      await api.switchToFlow(taskId)
      navigate(`/task/${taskId}`)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '切换失败，请稍后重试')
      setSwitchBusy(false)
    }
  }, [taskId])

  const [confirmDelete, setConfirmDelete] = useState(false)
  const handleDelete = useCallback(async () => {
    setBusy(true)
    try {
      if (curStepStatus === 'active' || streaming) {
        await api.stepIntervene(taskId, STEP_ID, 'stop', '删除前停止')
      }
      await api.deleteTask(taskId)
      navigate('/')
    } catch (err) {
      setToast(err instanceof Error ? err.message : '删除失败，请稍后重试')
      setBusy(false)
    }
  }, [taskId, curStepStatus, streaming])

  const toggleTool = useCallback((callId: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev)
      if (next.has(callId)) next.delete(callId)
      else next.add(callId)
      return next
    })
  }, [])

  const step = data?.step ?? null
  const allMsgs = store.messages
  const confirmedMsgs = useConfirmedViews(allMsgs)
  const pendingMsgs = useMemo(() => allMsgs.filter((m) => m.state === 'pending'), [allMsgs])
  const confirmedTailSig = useMemo(() => {
    const m = confirmedMsgs[confirmedMsgs.length - 1]
    if (!m) return ''
    return `${m.seq ?? 'x'}:${(m.content?.length ?? 0) + (m.output?.length ?? 0)
    + (typeof m.input === 'string' ? m.input.length : 0)}`
  }, [confirmedMsgs])
  const roundGroups = useMemo(() => {
    const groups: { id: string; thinking: string; text: string; tools: typeof pendingMsgs }[] = []
    const byId = new Map<string, (typeof groups)[number]>()
    let last: (typeof groups)[number] | null = null
    for (const m of pendingMsgs) {
      if (m.role === 'user') continue
      if (m.kind === 'tool') {
        if (m.toolName === 'dcflow_ask_user') continue
        if (m.roundUid) {
          let g = byId.get(m.roundUid)
          if (!g) {
            g = {id: m.roundUid, thinking: '', text: '', tools: []}
            byId.set(m.roundUid, g)
            groups.push(g)
          }
          g.tools.push(m)
          last = g
          continue
        }
        if (!last || last.thinking || last.text) {
          last = {id: `g-${m.key}`, thinking: '', text: '', tools: []}
          groups.push(last)
        }
        last.tools.push(m)
        continue
      }
      const rid = m.entityId ?? m.key
      let g = byId.get(rid)
      if (!g) {
        g = {id: rid, thinking: '', text: '', tools: []}
        byId.set(rid, g)
        groups.push(g)
      }
      if (m.kind === 'thinking') g.thinking += m.content
      else if (m.kind === 'text') g.text = m.content
      last = g
    }
    return groups
  }, [pendingMsgs])
  const sentBubbles = useMemo(() => pendingMsgs.filter((m) => m.role === 'user'), [pendingMsgs])
  const {set: toolExpanded, sig: toolExpandedSig} = useToolExpanded(allMsgs, expandedTools)
  const contentRef = useRef<HTMLDivElement>(null)
  const [autoScrollOn, setAutoScrollOn] = useState(true)
  const stepRunning = curStepStatus === 'active'
      || (streaming && !['stopped', 'completed', 'skipped'].includes(curStepStatus))
  const restMsgs = confirmedMsgs.filter((m) => m.role !== 'system'
      && !(m.role === 'assistant' && !(m.content ?? '').trim()))
  const {from, atHead, expand, showJump, setShowJump, jumpToBottom} = useChatWindow({
    scrollRef: contentRef,
    count: restMsgs.length,
    oldestKey: restMsgs[0]?.seq,
    newestKey: restMsgs[restMsgs.length - 1]?.seq,
    resetKey: taskId,
    truncated: !!data?.truncated,
    onReachTop: data?.truncated ? loadEarlier : undefined,
  })
  const autoScroll = useChatAutoScroll(
      contentRef,
      [
        pendingMsgs.reduce((n, m) => n + m.content.length
            + (typeof m.input === 'string' ? m.input.length : 0)
            + (m.output?.length ?? 0), 0),
        pendingMsgs.length,
        confirmedTailSig,
        data?.max_seq,
        llmError,
        retryProgress,
        streaming,
      ],
      autoScrollOn && stepRunning,
      setShowJump,
  )
  const initialScrolledRef = useRef(false)
  useEffect(() => {
    if (data && !initialScrolledRef.current) {
      initialScrolledRef.current = true
      const el = contentRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [data])
  useEffect(() => {
    setStreaming(false)
    store.reset()
    setExpandedTools(new Set())
    setLlmError(null)
    setLoadingMore(false)
    setRetryProgress(null)
    initialScrolledRef.current = false
    setAskSelected(null)
    setAskAnswer('')
    resetAll()
  }, [taskId])

  const askPending = askUserEvt != null
  const handleAskSubmit = useCallback(async () => {
    if (!askUserEvt) return
    const custom = askAnswer.trim()
    if (askSelected == null && !custom) return
    const answer = custom || String(askUserEvt.options?.[askSelected ?? -1] ?? '')
    if (!answer) return
    setBusy(true)
    try {
      const res = await fetch('/api/step/answer', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task_id: taskId, step_id: STEP_ID, answer}),
      })
      if (!res.ok) {
        console.error('ask_user answer failed:', res.status)
        return
      }
      setAskUserEvt(null)
      setAskSelected(null)
      setAskAnswer('')
    } finally {
      setBusy(false)
    }
  }, [askUserEvt, askAnswer, askSelected, taskId])

  const curStep = task?.steps?.find((s) => s.step_id === STEP_ID) ?? step
  const contextTokens = curStep?.context_tokens ?? 0
  const tokenPrompt = curStep?.token_prompt ?? 0
  const tokenCached = curStep?.token_cached ?? 0
  const tokenCompletion = Math.max(metrics.completion, curStep?.token_completion ?? 0)
  const prices = (curStep?.model_tier ?? 'light') === 'light' ? lightPrices : powerPrices
  const totalCost = (Math.max(0, tokenPrompt - tokenCached) * prices.in
      + tokenCached * prices.cached + tokenCompletion * prices.out) / 1e6

  const chatStatusLabel = curStepStatus === 'active' ? '对话中' : '等待输入'

  if (taskGone) return <Navigate to="/" replace/>
  if (task && task.mode !== 'single') return <Navigate to={`/task/${taskId}`} replace/>

  return (
      <>
        <div className="top-bar top-bar-rail chat-top-bar">
          <a className="back-btn" onClick={() => navigate('/')}>
            <Icon name="arrowLeft" size={13} gap={2}/>
            返回
          </a>
          <div className="chat-title">{task?.title || '对话'}</div>
          <span className={`step-badge ${stepRunning ? 'badge-light' : 'badge-required'}`}>
            {chatStatusLabel}
          </span>
          <button
              className="btn chat-switch-btn"
              disabled={switchBusy || busy || streaming}
              title="把本任务转换为多步骤流程：AI 将基于当前对话编排流程，对话记录转交编排上下文，产物文件保留"
              onClick={() => setConfirmSwitch(true)}
          >
            <Icon name="settings" size={13} gap={4}/>
            <span className="btn-label-text">切换为多步骤</span>
          </button>
          <button className="btn btn-danger" disabled={busy}
                  title="删除本对话任务"
                  onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={13}/>
            <span className="btn-label-text">删除</span>
          </button>
          <div className="seg-tabs">
            <button className={`seg-btn${view === 'chat' ? ' active' : ''}`} onClick={() => setView('chat')}>
              <Icon name="message" size={13} gap={5}/>
              对话
            </button>
            <button className={`seg-btn${view === 'code' ? ' active' : ''}`} onClick={() => setView('code')}>
              <Icon name="folder" size={13} gap={5}/>
              代码
              {editorDirty && <span className="tab-dirty"/>}
            </button>
          </div>
        </div>
        {view === 'code' && (
            <CodeTab
                taskId={taskId}
                externalModifiedPaths={externalModified}
                onDirtyChange={setEditorDirty}
                onExternalModified={handleExternalModified}
            />
        )}
        {view === 'chat' && (
            <>
              <div
                  className="content-area"
                  ref={contentRef}
              >
                <div className="chat-log">
                  {(from > 0 || data?.truncated) && (
                      <div className="history-fold">
                        {from > 0 ? (
                            <button className="history-load-more" onClick={expand}>
                              {`显示更早消息（还有 ${from} 条已加载未显示）`}
                            </button>
                        ) : (
                            <button className="history-load-more" onClick={loadEarlier} disabled={loadingMore}>
                              {loadingMore ? '加载中...' : `加载更早消息（共 ${data?.total ?? '?'} 条，已显示 ${data?.messages.length ?? 0} 条）`}
                            </button>
                        )}
                      </div>
                  )}
                  {restMsgs.length === 0 && !roundGroups.length && (
                      <div className="chat-status chat-empty-hint">
                        发送第一句开始对话——AI 可使用全部执行工具（读写文件/运行代码），
                        也可以向你提问澄清需求
                      </div>
                  )}
                  {restMsgs.slice(from).map((m, i) => {
                    const ap = m.role === 'assistant' ? extractAskUserPackage(m.content ?? '') : null
                    if (ap) {
                      return (
                          <ChatMessage
                              key={m.seq != null ? m.seq : i}
                              msg={{...m, content: ap.before + ap.after}}
                              onToggleTool={toggleTool}
                              expandedTools={expandedTools}
                          />
                      )
                    }
                    return (
                        <Fragment key={m.seq != null ? m.seq : i}>
                          <ChatMessage msg={m} onToggleTool={toggleTool} expandedTools={toolExpanded}
                                       autoScroll={autoScrollOn && stepRunning}/>
                        </Fragment>
                    )
                  })}
                  {roundGroups.map((r, ri) => {
                    const isLast = ri === roundGroups.length - 1
                    return (
                        <Fragment key={r.id}>
                          {r.thinking && (
                              <div className="ai-thinking-inline">
                                <div className="think-box">
                                  <div className="think-body" style={{display: 'block'}}>
                                    <div className="ai-text">
                                      {r.thinking}
                                      {isLast && <span className="stream-cursor"/>}
                                    </div>
                                  </div>
                                </div>
                              </div>
                          )}
                          {r.text ? (
                              <div className="ai-block">
                                <div className="ai-text">
                                  {isLast && streaming ? (
                                      <>
                                        {r.text}
                                        <span className="stream-cursor"/>
                                      </>
                                  ) : (
                                      <Markdown>{r.text}</Markdown>
                                  )}
                                </div>
                              </div>
                          ) : (
                              isLast &&
                              streaming &&
                              !r.thinking &&
                              r.tools.length === 0 && (
                                  <div className="ai-block">
                                    <div className="ai-text">
                        <span className="loading-text">
                          <span className="loading-spinner"/>
                          AI 正在思考...
                        </span>
                                    </div>
                                  </div>
                              )
                          )}
                          {r.tools.map((m) => {
                            const callId = m.entityId ?? m.key
                            return (
                                <div className="ai-tool-inline" key={m.key}>
                                  <ToolCard
                                      callId={callId}
                                      toolName={m.toolName ?? ''}
                                      input={m.input}
                                      output={m.output}
                                      status={m.status === 'done' ? 'done' : 'running'}
                                      defaultExpanded={toolExpanded.has(callId)}
                                      autoScroll={autoScrollOn && stepRunning}
                                  />
                                </div>
                            )
                          })}
                        </Fragment>
                    )
                  })}
                  {askPending && askUserEvt && (
                      <div className="ask-card">
                        <div className="ask-head">
                          <Icon name="message" size={13} gap={6}/>
                          AI 在等待你的回答
                        </div>
                        <div className="ask-question">
                          <Markdown>{askUserEvt.question}</Markdown>
                        </div>
                        {askUserEvt.options && askUserEvt.options.length > 0 && (
                            <div className="ask-options">
                              {askUserEvt.options.map((opt, j) => (
                                  <button
                                      key={j}
                                      className={`ask-option${askSelected === j ? ' selected' : ''}`}
                                      onClick={() => {
                                        setAskSelected(j)
                                        setAskAnswer('')
                                      }}
                                  >
                                    {opt}
                                  </button>
                              ))}
                            </div>
                        )}
                        <div className="ask-custom">
                          <textarea
                              rows={2}
                              placeholder="或输入你自己的回答…"
                              value={askAnswer}
                              onChange={(e) => {
                                setAskAnswer(e.target.value)
                                setAskSelected(null)
                              }}
                          />
                        </div>
                        <div className="ask-actions">
                          <button
                              className="btn btn-primary"
                              disabled={busy || (askSelected == null && !askAnswer.trim())}
                              onClick={() => void handleAskSubmit()}
                          >
                            <Icon name="check" size={13} gap={5}/>
                            发送回答
                          </button>
                        </div>
                      </div>
                  )}
                  {roundGroups.length === 0 && streaming && (
                      <div className="ai-block">
                        <div className="ai-text">
                          <span className="loading-text">
                            <span className="loading-spinner"/>
                            AI 正在思考...
                          </span>
                        </div>
                      </div>
                  )}
                  {sentBubbles.map((p) => (
                      <div className="msg-user" key={p.key}>
                        <div className={`user-bubble${p.status === 'streaming' ? ' sending' : ''}`}>
                          {p.content}
                          {p.status === 'streaming' && (
                              <span className="sending-tag">
                                <span className="loading-spinner"/>
                                发送中…
                              </span>
                          )}
                        </div>
                      </div>
                  ))}
                  {retryProgress != null && (
                      <div className="retry-progress">
                        <Icon name="refresh" size={13} gap={8}/>
                        请求被限流，正在重试 ({retryProgress.count}/{retryProgress.total})...
                      </div>
                  )}
                  {llmError && (
                      <div className="llm-error-card">
                        <div className="error-icon">
                          <Icon name="error" size={20} gap={0}/>
                        </div>
                        <div className="error-body">
                          <strong>LLM 错误: {llmError.code || 'unknown'}</strong>
                          <div>{llmError.message || '未知错误'}</div>
                          <div className="error-detail">
                            已重试 {llmError.retryCount || 0} 次{llmError.retryable ? '（可重试）' : '（不可重试）'}
                            {llmError.model ? ` · 模型 ${llmError.model}` : ''}
                          </div>
                          {llmError.retryable && (
                              <button className="retry-btn" onClick={handleRetry}>
                                <Icon name="refresh" size={13} gap={5}/>
                                重试
                              </button>
                          )}
                        </div>
                      </div>
                  )}
                  {!data && <div className="chat-status">加载中...</div>}
                </div>
                {showJump && (
                    <button className="scroll-jump-btn" onClick={jumpToBottom} title="回到最新输出">
                      <Icon name="chevronDown" size={16} gap={0}/>
                    </button>
                )}
              </div>
              <div className="step-footer">
                <ContextMeter windowSize={contextWindow} prompt={contextTokens}/>
                <InterveneBar
                    mode="step"
                    onSend={(content) => doIntervene('send', content)}
                    onForce={(content) => doIntervene('force_inject', content)}
                    onStop={stepRunning ? () => doIntervene('stop', '停止生成') : undefined}
                    onCompress={handleCompress}
                    running={stepRunning}
                    busy={busy}
                    autoScroll={autoScrollOn}
                    onToggleAutoScroll={() => setAutoScrollOn((v) => !v)}
                />
                <TokenDetails prompt={tokenPrompt} cached={tokenCached} completion={tokenCompletion}
                              cost={totalCost}
                              trailing={
                                <TokenMetrics startedAt={metrics.startedAt} endedAt={metrics.endedAt}
                                              baseAt={metrics.baseAt}
                                              ttftMs={metrics.ttftSamples ? metrics.ttftMs
                                                  : ((curStep?.ttft_samples ?? 0) > 0
                                                      ? ((curStep?.ttft_total_ms ?? 0) / (curStep?.ttft_samples ?? 1))
                                                      : null)}
                                              active={curStep?.status === 'active' || streaming}
                                              runDurationMs={Math.max(metrics.runDurationMs, curStep?.run_duration_ms ?? 0)}
                                              outputDurationMs={Math.max(metrics.outputDurationMs, curStep?.output_duration_ms ?? 0)}
                                              requestCount={Math.max(metrics.requests, curStep?.requests ?? 0)}
                                              completion={tokenCompletion}
                                              roundFirstAt={metrics.startedAt != null
                                                  ? (metricRequestRef.current?.firstTokenAt ?? null) : null}
                                              roundComp={metrics.startedAt != null ? roundCompRef.current : 0}/>
                              }
                />
              </div>
            </>
        )}
        <ConfirmDialog
            open={confirmSwitch}
            title="切换为多步骤流程？"
            description="将把本任务转换为多步骤流程：AI 会基于当前完整对话编排流程步骤，对话记录转交编排上下文，已产生的文件产物全部保留（本任务位置不变）。"
            rows={[]}
            confirmText="确认切换"
            onConfirm={() => {
              setConfirmSwitch(false)
              void handleSwitchToFlow()
            }}
            onCancel={() => setConfirmSwitch(false)}
        />
        <ConfirmDialog
            open={confirmDelete}
            title="删除本对话任务？"
            rows={[{step_id: taskId, title: '将删除对话记录与任务数据，不可恢复', required: false}]}
            onConfirm={() => {
              setConfirmDelete(false)
              void handleDelete()
            }}
            onCancel={() => setConfirmDelete(false)}
            confirmText="确认删除"
        />
        {toast && <ErrorToast message={toast} onClose={() => setToast('')}/>}
      </>
  )
}
