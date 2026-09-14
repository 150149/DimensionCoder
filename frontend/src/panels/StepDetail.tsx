import {Fragment, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {
  GateApproval,
  GateDoc,
  LlmErrorEvent,
  Message,
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
import CodeDiagram from '../components/CodeDiagram'
import {Markdown} from '../components/Markdown'
import ConfirmDialog, {type ConfirmRow} from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ProgressRail from '../components/ProgressRail'
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
import {extractAskUserPackage, extractDecisionPackage, parseDecisionPackage, toGateApproval,} from '../utils/decisionPkg'
import CodeTab from '../editor/CodeTab'

interface StepDetailProps {
  taskId: string
  stepId: string
}

interface StreamRound {
  id: number
  thinking: string
  text: string
  toolCallIds: string[]
}


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

export default function StepDetail({taskId, stepId}: StepDetailProps) {
  const navigate = useNavigate()
  const [data, setData] = useState<StepData | null>(null)
  const [task, setTask] = useState<TaskSummary | null>(null)
  const [streaming, setStreaming] = useState(false)
  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, settleStep, mergeBackend, resetAll,
  } = useStepMetrics()
  const localMsgIdRef = useRef(0)
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
    if (l.streaming) setStreaming(true)
    const up = storeRef.current.upsertPending
    const rid = l.roundUid ?? 'live'
    if (l.thinking) up({entityId: rid, kind: 'thinking', role: 'assistant', content: l.thinking})
    if (l.text) up({entityId: rid, kind: 'text', role: 'assistant', content: l.text})
    for (const t of l.tools ?? []) {
      up({entityId: t.callId, kind: 'tool', role: 'tool', toolName: t.name, input: t.input, output: t.output ?? undefined, status: t.status})
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

  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [approveBusy, setApproveBusy] = useState(false)
  const [selectedOpt, setSelectedOpt] = useState<number | null>(null)
  const [customText, setCustomText] = useState('')
  const [choiceOpen, setChoiceOpen] = useState(false)
  const [choiceNote, setChoiceNote] = useState('')
  const [approvalReady, setApprovalReady] = useState<GateApproval | null>(null)
  const [gateDoc, setGateDoc] = useState<GateDoc | null>(null)
  const [gateDocLoading, setGateDocLoading] = useState(false)
  const gateDocMsg = useMemo<Message | null>(
      () => (gateDoc?.content ? {role: 'assistant', content: gateDoc.content} : null),
      [gateDoc?.content])
  const [askSelected, setAskSelected] = useState<number | null>(null)
  const [askAnswer, setAskAnswer] = useState('')
  const latestAssistant = useMemo(() => {
    const msgs = data?.conversation ?? []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'assistant' && (m.content || '').trim()) return String(m.content)
    }
    return ''
  }, [data])
  const pkg = useMemo(() => parseDecisionPackage(latestAssistant), [latestAssistant])
  const approval = useMemo<GateApproval | null>(() => {
    if (approvalReady?.options?.length) return approvalReady
    if (data?.decision?.options?.length) return data.decision
    return pkg ? toGateApproval(pkg) : null
  }, [approvalReady, data, pkg])
  const hasDecisionPkg = useMemo(
      () => Boolean(approval?.options?.length)
          || (data?.conversation ?? []).some((m) => m.role === 'assistant' && parseDecisionPackage(m.content ?? '')),
      [approval, data],
  )
  const lastAskUserSeq = useMemo(() => {
    let seq: number | null = null
    for (const m of data?.conversation ?? []) {
      if (m.role === 'assistant' && m.seq != null && extractAskUserPackage(m.content ?? '')) seq = m.seq
    }
    return seq
  }, [data])
  const handleChooseConfirm = useCallback(async () => {
    const custom = customText.trim()
    if (selectedOpt == null && !custom) return
    setApproveBusy(true)
    try {
      let msg: string
      if (custom) {
        msg = `【决策选择】用户自定义决策：${custom}`
      } else if (selectedOpt != null && approval?.options?.[selectedOpt]) {
        const opt = approval.options[selectedOpt]
        const label = String.fromCharCode(65 + selectedOpt)
        const note = choiceNote.trim()
        msg = `【决策选择】用户选择选项 ${label}：${opt.option}${note ? `\n补充说明：${note}` : ''}`
      } else {
        return
      }
      await api.approveGate(taskId, stepId, msg)
      loadMeta()
      setChoiceOpen(false)
      setChoiceNote('')
      setSelectedOpt(null)
      setCustomText('')
      setToast('已提交你的决策，流程继续')
    } catch (err) {
      setToast(err instanceof Error ? err.message : '发送失败，请稍后重试')
    } finally {
      setApproveBusy(false)
    }
  }, [selectedOpt, customText, approval, choiceNote, taskId, stepId])
  const handleApprove = useCallback(async () => {
    setApproveBusy(true)
    try {
      await api.approveGate(taskId, stepId)
      loadMeta()
    } catch (err) {
      setToast(err instanceof Error ? err.message : '审批失败，请稍后重试')
    } finally {
      setApproveBusy(false)
    }
  }, [taskId, stepId])
  const handleRejectConfirm = useCallback(async () => {
    const reason = rejectReason.trim()
    if (!reason) return
    setApproveBusy(true)
    try {
      await api.rejectGate(taskId, stepId, reason)
      loadMeta()
      setRejectOpen(false)
      setRejectReason('')
    } catch (err) {
      setToast(err instanceof Error ? err.message : '拒绝失败，请稍后重试')
    } finally {
      setApproveBusy(false)
    }
  }, [taskId, stepId, rejectReason])

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
        const d = await api.getStep(taskId, stepId, {limit: 200})
        setData(d)
        storeRef.current.loadPage((d.conversation ?? []) as unknown[])
        absorbLive(d.live)
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
  }, [taskId, stepId, absorbLive])
  const [loadingMore, setLoadingMore] = useState(false)
  const loadEarlier = useCallback(() => {
    const msgs = data?.messages
    const oldest = msgs && msgs.length ? msgs[0].seq : undefined
    if (oldest == null || loadingMore) return
    setLoadingMore(true)
    api
        .getStep(taskId, stepId, {limit: 200, beforeSeq: oldest})
        .then((d) => {
          setData((prev) => (prev ? mergeEarlierPage(prev, d) : prev))
          storeRef.current.prependPage(((d.conversation ?? d.messages) ?? []) as unknown[])
        })
        .finally(() => setLoadingMore(false))
  }, [data, loadingMore, taskId, stepId])
  useEffect(() => {
    loadStep()
  }, [loadStep])

  const loadMeta = useCallback(() => api.getTask(taskId).then((d) => {
    setTask(d.task)
    const step = d.task?.steps?.find((s) => s.step_id === stepId)
    mergeBackend(step)
  }), [taskId, stepId, mergeBackend])

  useEffect(() => {
    loadMeta()
  }, [loadMeta])

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
    sampleFirstChunk()
    accrueRoundComp(e.chunk.length)
    storeRef.current.upsertPending({entityId: e.entityId ?? 'live-text', kind: 'text', role: 'assistant', append: e.chunk})
  }, [sampleFirstChunk, accrueRoundComp])

  const handleStepStart = useCallback(() => {
    setStreaming(true)
    const at = Date.now()
    resetStep(at)
    setLlmError(null)
    storeRef.current.clearPending()
  }, [resetStep])


  const handleStreamEnd = useCallback(() => {
    setStreaming(false)
    const at = Date.now()
    endRound(at)
    storeRef.current.clearPending('thinking')
    void loadMeta()
  }, [endRound, loadMeta])

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
      status: 'done',
    })
  }, [])

  const handleThinkingChunk = useCallback(
      (e: ThinkingChunkEvent) => {
        sampleFirstChunk()
        accrueRoundComp(e.chunk.length)
        storeRef.current.upsertPending({entityId: e.entityId ?? 'live-thinking', kind: 'thinking', role: 'assistant', append: e.chunk})
      },
      [sampleFirstChunk, accrueRoundComp],
  )

  const handleToolStart = useCallback(
      (e: ToolCallStartEvent) => {
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', toolName: e.toolName, input: e.input, status: 'streaming'})
      },
      [],
  )

  const handleToolParam = useCallback(
      (e: ToolCallParamEvent) => {
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', inputAppend: e.delta})
        accrueRoundComp(e.delta.length)
      },
      [accrueRoundComp],
  )

  const handleToolExecuting = useCallback(() => {
    setStreaming(false)
    storeRef.current.clearPending('thinking')
  }, [])

  const handleToolResult = useCallback(
      (e: ToolCallResultEvent) => {
        storeRef.current.upsertPending({entityId: e.entityId ?? e.callId, kind: 'tool', role: 'tool', output: e.output, status: 'done'})
        setStreaming(true)
        const req = metricRequestRef.current
        if (req) req.reqStartedAt = Date.now()
      },
      [],
  )

  const handleLlmError = useCallback(
      (e: LlmErrorEvent) => {
        if (e.stepId === stepId) setLlmError(e)
        setToast(e.message || 'LLM 错误')
        setStreaming(false)
        settleStep(Date.now())
      },
      [stepId, settleStep],
  )

  const handleRetry = useCallback(async () => {
    setBusy(true)
    try {
      await api.resumeStep(taskId, stepId)
      await api.startTask(taskId)
      setLlmError(null)
      loadMeta()
    } catch (err) {
      setToast(err instanceof Error ? err.message : '重试失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, stepId])

  useStepStream(taskId, stepId, {
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
    onStepStatsUpdated: (e) => mergeBackend(e.stats),
    onGateApprovalReady: (e) => setApprovalReady(e.approval),
    onMessagesConfirmed: (e) => storeRef.current.confirm(e.items),
    onMessagesReset: () => {
      storeRef.current.reset()
      void loadStep()
    },
    onReconnect: () => {
      void loadStep()
    },
  }, undefined, () => storeRef.current.lastSeq)

  const [confirmReset, setConfirmReset] = useState(false)
  const [pendingMsg, setPendingMsg] = useState('')
  const doIntervene = useCallback(
      async (mode: 'send' | 'force_inject' | 'stop', content: string) => {
        setBusy(true)
        const isMsg = mode !== 'stop'
        const localId = `local-${Date.now()}-${++localMsgIdRef.current}`
        if (isMsg) storeRef.current.upsertPending({entityId: localId, role: 'user', content, status: 'streaming'})
        try {
          await api.stepIntervene(taskId, stepId, mode, content, isMsg ? localId : undefined)
          if (mode === 'stop') {
            setStreaming(false)
          }
        } catch (err) {
          if (isMsg) storeRef.current.dropPending(localId)
          setToast(err instanceof Error ? err.message : '发送失败，请稍后重试')
        } finally {
          setBusy(false)
        }
      },
      [taskId, stepId, loadStep],
  )
  const intervene = useCallback(
      async (mode: 'send' | 'force_inject' | 'stop', content: string) => {
        if (mode === 'send') {
          const cur = (task?.steps ?? []).find((s) => s.step_id === stepId)
          if (cur && cur.status === 'completed') {
            setPendingMsg(content)
            setConfirmReset(true)
            return
          }
        }
        await doIntervene(mode, content)
      },
      [task, stepId, doIntervene],
  )
  const handleConfirmReset = useCallback(() => {
    setConfirmReset(false)
    void doIntervene('send', pendingMsg)
  }, [doIntervene, pendingMsg])
  const resetRows: ConfirmRow[] = useMemo(() => {
    const cur = (task?.steps ?? []).find((s) => s.step_id === stepId)
    if (!cur) return []
    const curOrder = cur.sort_order ?? 0
    return (task?.steps ?? [])
        .filter((s) => s.step_id !== stepId && !s.step_id.startsWith('_') && (s.sort_order ?? 0) > curOrder)
        .map((s) => ({
          step_id: s.step_id,
          title: s.title,
          required: s.required,
          human_attention: s.human_attention,
        }))
  }, [task, stepId])

  const handleCompress = useCallback(async () => {
    setBusy(true)
    try {
      await api.compressStep(taskId, stepId)
      void loadStep()
    } catch (err) {
      setToast(err instanceof Error ? err.message : '压缩失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, stepId, loadStep])

  const toggleTool = useCallback((callId: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev)
      if (next.has(callId)) next.delete(callId)
      else next.add(callId)
      return next
    })
  }, [])

  const step = data?.step ?? null
  const steps = task?.steps ?? []
  const railPausedPendingId = task?.status === 'paused'
      ? [...steps].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
          .find((s) => s.status === 'pending')?.step_id
      : undefined

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
        if (!last) {
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
  const stepRunning = streaming || steps.find((s) => s.step_id === stepId)?.status === 'active'
  const autoScroll = useChatAutoScroll(
      contentRef,
      [
        pendingMsgs.reduce((n, m) => n + m.content.length
            + (typeof m.input === 'string' ? m.input.length : 0)
            + (m.output?.length ?? 0), 0),
        pendingMsgs.length,
        confirmedTailSig,
        toolExpandedSig,
        data?.max_seq,
        llmError,
        retryProgress,
        streaming,
      ],
      autoScrollOn && stepRunning,
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
    setRejectOpen(false)
    setRejectReason('')
    setChoiceOpen(false)
    setSelectedOpt(null)
    setCustomText('')
    setChoiceNote('')
    setAskSelected(null)
    setAskAnswer('')
    setApprovalReady(null)
    setGateDoc(null)
    setGateDocLoading(false)
    resetAll()
  }, [stepId])
  const curStep = steps.find((s) => s.step_id === stepId) ?? step
  const contextTokens = curStep?.context_tokens ?? 0
  const tokenPrompt = curStep?.token_prompt ?? 0
  const tokenCached = curStep?.token_cached ?? 0
  const tokenCompletion = metrics.startedAt != null ? metrics.completion : (curStep?.token_completion ?? 0)
  const prices = (curStep?.model_tier ?? 'power') === 'light' ? lightPrices : powerPrices
  const totalCost = (Math.max(0, tokenPrompt - tokenCached) * prices.in
      + tokenCached * prices.cached + tokenCompletion * prices.out) / 1e6

  const isGatePending =
      curStep?.status === 'active' && curStep?.human_attention === 'gate' && task?.status === 'paused'

  useEffect(() => {
    if (!isGatePending || !approval) {
      setGateDoc(null)
      setGateDocLoading(false)
      return
    }
    let cancelled = false
    setGateDocLoading(true)
    api.getGateDoc(taskId, stepId)
        .then((d) => {
          if (!cancelled) setGateDoc(d)
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setGateDoc({
              path: approval.doc_path ?? '', content: '', truncated: false,
              error: err instanceof Error ? err.message : '文档读取失败',
            })
          }
        })
        .finally(() => {
          if (!cancelled) setGateDocLoading(false)
        })
    return () => {
      cancelled = true
    }
  }, [isGatePending, approval, taskId, stepId])

  const askPending = Boolean(lastAskUserSeq != null && curStep?.status === 'stopped')
  const askPkg = useMemo(() => {
    for (let i = (data?.conversation ?? []).length - 1; i >= 0; i--) {
      const m = data!.conversation[i]
      if (m.role !== 'assistant') continue
      const ap = extractAskUserPackage(m.content ?? '')
      if (ap) return ap.pkg
    }
    return null
  }, [data])
  const handleAskSubmit = useCallback(async () => {
    if (!askPkg) return
    const custom = askAnswer.trim()
    if (askSelected == null && !custom) return
    const answer = custom || String(askPkg.options?.[askSelected ?? -1] ?? '')
    if (!answer) return
    await doIntervene('send', `【问答回复】${askPkg.question}\n→ ${answer}`)
    setAskSelected(null)
    setAskAnswer('')
  }, [askPkg, askAnswer, askSelected, doIntervene])

  const canResume = curStep?.status === 'stopped'
      || (curStep?.status === 'pending' && task?.status === 'paused')
  const handleResumeStep = useCallback(async () => {
    if (!curStep) return
    if (curStep.status === 'stopped') {
      setBusy(true)
      try {
        await api.resumeStep(taskId, stepId)
        await api.startTask(taskId)
        loadMeta()
      } catch (err) {
        setToast(err instanceof Error ? err.message : '操作失败，请稍后重试')
      } finally {
        setBusy(false)
      }
    } else if (curStep.status === 'pending' && task?.status === 'paused') {
      setBusy(true)
      try {
        await api.startTask(taskId)
        loadMeta()
      } catch (err) {
        setToast(err instanceof Error ? err.message : '操作失败，请稍后重试')
      } finally {
        setBusy(false)
      }
    }
  }, [taskId, stepId, curStep, task?.status])

  const handleToggleAutoApprove = useCallback(async () => {
    if (!task) return
    const next = !task.auto_approve
    setTask({...task, auto_approve: next})
    try {
      await api.setAutoApprove(taskId, next)
      loadMeta()
    } catch (err) {
      setTask({...task, auto_approve: !next})
      setToast(err instanceof Error ? err.message : '切换自动审批模式失败，请稍后重试')
    }
  }, [taskId, task, loadMeta])


  const systemMsg = confirmedMsgs.find((m) => m.role === 'system' && m.seq === 0)
  const restMsgs = confirmedMsgs.filter((m) => m.role !== 'system'
      && !(m.role === 'assistant' && !(m.content ?? '').trim()))

  const {from, atHead, expand} = useChatWindow({
    scrollRef: contentRef,
    count: restMsgs.length,
    oldestKey: restMsgs[0]?.seq,
    newestKey: restMsgs[restMsgs.length - 1]?.seq,
    resetKey: stepId,
    truncated: !!data?.truncated,
    onReachTop: data?.truncated ? loadEarlier : undefined,
  })

  return (
      <>
        <div className="top-bar top-bar-rail">
          <a className="back-btn" onClick={() => navigate(`/task/${taskId}`)}>
            <Icon name="arrowLeft" size={13} gap={2}/>
            返回
          </a>
          <ProgressRail
              steps={steps}
              pausedPendingId={railPausedPendingId}
              currentStepId={stepId}
              onDotClick={(sid) => navigate(`/task/${taskId}/step/${sid}`)}
          />
          {canResume && (
              <button className="run-toggle-btn" onClick={handleResumeStep} disabled={busy} title="恢复当前步骤">
                <Icon name="play" size={13} gap={4}/>
                恢复
              </button>
          )}
          <button
              className={`auto-approve-toggle${task?.auto_approve ? ' active' : ''}`}
              onClick={handleToggleAutoApprove}
              title="自动审批：用户不在线时 gate 审批自动走决策路径放行"
          >
            <span className="toggle-dot"/>
            自动审批
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
                <div className="step-header">{step?.title ?? stepId}</div>
                {step && (
                    <div className="step-meta">
                      {step.human_attention === 'gate' && <span className="step-badge badge-gate">Gate 审批</span>}
                      <span className={`step-badge ${step.model_tier === 'light' ? 'badge-light' : 'badge-power'}`}>
              {step.model_tier === 'light' ? '轻量模型' : '强力模型'}
            </span>
                      <span className={`step-badge ${step.required ? 'badge-required' : 'badge-optional'}`}>
              {step.required ? '必做' : '可选'}
            </span>
                      <span className="step-badge">状态: {curStep?.status ?? step.status}</span>
                    </div>
                )}
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
                  {atHead && systemMsg && <ChatMessage msg={systemMsg}/>}
                  {atHead && data?.prep?.step_context && (
                      <div className="msg-user">
                        <div className="user-bubble">
                          <Markdown>{data.prep.step_context}</Markdown>
                        </div>
                      </div>
                  )}
                  {restMsgs.slice(from).map((m, i) => {
                    const ap = m.role === 'assistant' ? extractAskUserPackage(m.content ?? '') : null
                    if (ap) {
                      const isLastAsk = m.seq != null && m.seq === lastAskUserSeq
                      return (
                          <Fragment key={m.seq != null ? m.seq : i}>
                            <ChatMessage
                                msg={{...m, content: ap.before + ap.after}}
                                onToggleTool={toggleTool}
                                expandedTools={expandedTools}
                            />
                            {askPending && isLastAsk && (
                                <div className="ask-card">
                                  <div className="ask-head">
                                    <Icon name="message" size={13} gap={6}/>
                                    需求澄清 — AI 在等待你的回答
                                  </div>
                                  <div className="ask-question">
                                    <Markdown>{ap.pkg.question}</Markdown>
                                  </div>
                                  {ap.pkg.options && ap.pkg.options.length > 0 && (
                                      <div className="ask-options">
                                        {ap.pkg.options.map((opt, j) => (
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
                          </Fragment>
                      )
                    }
                    const dp = m.role === 'assistant' ? extractDecisionPackage(m.content ?? '') : null
                    if (dp) {
                      return (
                          <Fragment key={m.seq != null ? m.seq : i}>
                            <ChatMessage
                                msg={{...m, content: dp.before + dp.after}}
                                onToggleTool={toggleTool}
                                expandedTools={expandedTools}
                            />
                          </Fragment>
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
                              !r.thinking && (
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
                  {curStep?.human_attention === 'gate' ? <CodeDiagram taskId={taskId}/> : null}
                  {isGatePending && approval && (
                      <div className="gate-doc">
                        {gateDocLoading ? (
                            <div className="gate-doc-loading">
                              <span className="loading-spinner"/>
                              正在读取方案文档…
                            </div>
                        ) : gateDoc?.error ? (
                            <div className="gate-doc-error">{gateDoc.error}</div>
                        ) : gateDocMsg ? (
                            <>
                              <ChatMessage msg={gateDocMsg}/>
                              {gateDoc?.truncated && (
                                  <div className="gate-doc-truncated">
                                    文档过大已截断，完整内容见 {gateDoc.path}
                                  </div>)}
                            </>
                        ) : null}
                      </div>
                  )}
                  {isGatePending && hasDecisionPkg && approval && (
                      <div className="gate-review-card gate-action-card">
                        <div className="gate-review-head">
                          <Icon name="settings" size={13} gap={6}/>
                          人工审批 — 请审阅上方方案文档后拍板
                        </div>
                        {approval.recommendation ? (
                            <div className="gate-rec">推荐：{approval.recommendation}</div>
                        ) : null}
                        {(approval.questions ?? []).length > 0 && (
                            <div className="gate-qs">
                              <ul>
                                {(approval.questions ?? []).map((q, j) => <li key={j}>{q}</li>)}
                              </ul>
                            </div>
                        )}
                        <div className="gate-options">
                          {approval.options.map((opt, j) => (
                              <button
                                  key={j}
                                  className={`gate-option${selectedOpt === j ? ' selected' : ''}`}
                                  onClick={() => {
                                    setSelectedOpt(j)
                                    setCustomText('')
                                  }}
                              >
                                <div className="gate-option-label">选项 {String.fromCharCode(65 + j)}</div>
                                <div className="gate-option-text">{opt.option}</div>
                              </button>
                          ))}
                        </div>
                        <div className="gate-custom">
                          <div className="gate-custom-label">
                            自定义方向 — 直接输入你的决策（不限于以上选项）
                          </div>
                          <textarea
                              rows={2}
                              placeholder="例如：投入超算求解，预算上限 500 元 / 接受收尾但保留重开入口…"
                              value={customText}
                              onChange={(e) => {
                                setCustomText(e.target.value)
                                setSelectedOpt(null)
                              }}
                          />
                        </div>
                        <div className="gate-review-actions">
                          <button
                              className="btn btn-primary"
                              disabled={approveBusy || (selectedOpt == null && !customText.trim())}
                              onClick={() => {
                                if (customText.trim()) {
                                  void handleChooseConfirm()
                                } else {
                                  setChoiceOpen(true)
                                }
                              }}
                          >
                            <Icon name="check" size={13} gap={5}/>
                            按所选选项继续
                          </button>
                          <button
                              className="btn"
                              disabled={approveBusy}
                              onClick={() => {
                                setRejectReason('')
                                setRejectOpen(true)
                              }}
                          >
                            <Icon name="close" size={13} gap={5}/>
                            拒绝
                          </button>
                        </div>
                      </div>
                  )}
                  {isGatePending && !hasDecisionPkg && (
                      <div className="gate-review-card">
                        <div className="gate-review-head">
                          <Icon name="settings" size={13} gap={6}/>
                          人工审批 — 请审阅后拍板
                        </div>
                        <div className="gate-review-body">
                          {latestAssistant ? (
                              <div className="gate-review-info">
                                <Markdown>{latestAssistant}</Markdown>
                              </div>
                          ) : (
                              <div className="gate-review-info muted">决策信息整理中...</div>
                          )}
                        </div>
                        <div className="gate-review-actions">
                          <button className="btn" disabled={approveBusy} onClick={handleApprove}>
                            审批通过
                          </button>
                          <button className="btn btn-danger" disabled={approveBusy} onClick={() => setRejectOpen(true)}>
                            <Icon name="close" size={12} gap={5}/>
                            拒绝
                          </button>
                        </div>
                      </div>
                  )}
                  {!data && <div className="chat-status">加载中...</div>}
                </div>
                {autoScroll.showJump && (
                    <button className="scroll-jump-btn" onClick={autoScroll.jumpToBottom} title="回到最新输出">
                      <Icon name="chevronDown" size={16} gap={0}/>
                    </button>
                )}
              </div>
              <div className="step-footer">
                <ContextMeter windowSize={contextWindow} prompt={contextTokens}/>
                <InterveneBar
                    mode="step"
                    onSend={(content) => intervene('send', content)}
                    onForce={(content) => intervene('force_inject', content)}
                    onStop={streaming || curStep?.status === 'active' ? () => intervene('stop', '打断') : undefined}
                    onCompress={handleCompress}
                    running={streaming || curStep?.status === 'active'}
                    busy={busy}
                    autoScroll={autoScrollOn}
                    onToggleAutoScroll={() => setAutoScrollOn((v) => !v)}
                />
                <TokenDetails prompt={tokenPrompt} cached={tokenCached} completion={tokenCompletion}
                              cost={totalCost}
                              trailing={
                                <TokenMetrics startedAt={metrics.startedAt} endedAt={metrics.endedAt}
                                              activeSince={metrics.activeSince}
                                              ttftMs={metrics.ttftSamples ? metrics.ttftMs
                                                  : ((curStep?.ttft_samples ?? 0) > 0
                                                      ? ((curStep?.ttft_total_ms ?? 0) / (curStep?.ttft_samples ?? 1))
                                                      : null)}
                                              active={curStep?.status === 'active' || streaming}
                                              runDurationMs={metrics.startedAt != null
                                                  ? metrics.runDurationMs : (curStep?.run_duration_ms ?? 0)}
                                              outputDurationMs={metrics.startedAt != null
                                                  ? metrics.outputDurationMs : (curStep?.output_duration_ms ?? 0)}
                                              requestCount={metrics.startedAt != null
                                                  ? metrics.requests : (curStep?.requests ?? 0)}
                                              completion={tokenCompletion}
                                              roundFirstAt={metrics.startedAt != null
                                                  ? (metricRequestRef.current?.firstTokenAt ?? null) : null}
                                              roundComp={metrics.startedAt != null ? roundCompRef.current : 0}/>
                              }
                />
              </div>
            </>
        )}
        {choiceOpen && approval?.options?.[selectedOpt ?? -1] && (
            <div className="modal-overlay show" onClick={() => setChoiceOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h3>按所选选项继续</h3>
                <div className="field">
                  <div className="field-label">选项 {String.fromCharCode(65 + (selectedOpt ?? 0))}</div>
                  <div className="choice-option-text">{approval?.options?.[selectedOpt ?? -1]?.option}</div>
                </div>
                <div className="field">
                  <div className="field-label">补充说明（可选）</div>
                  <textarea
                      rows={3}
                      placeholder="如：调查结果内容、授权信息、限制条件等"
                      value={choiceNote}
                      onChange={(e) => setChoiceNote(e.target.value)}
                  />
                </div>
                <div className="modal-actions">
                  <button onClick={() => setChoiceOpen(false)}>取消</button>
                  <button className="primary" disabled={approveBusy} onClick={handleChooseConfirm}>
                    确认发送
                  </button>
                </div>
              </div>
            </div>
        )}
        {rejectOpen && (
            <div className="modal-overlay show" onClick={() => setRejectOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h3>拒绝审批</h3>
                <div className="field">
                  <div className="field-label">原因（必填）</div>
                  <textarea
                      rows={3}
                      placeholder="请输入拒绝原因"
                      value={rejectReason}
                      onChange={(e) => setRejectReason(e.target.value)}
                  />
                </div>
                <div className="modal-actions">
                  <button onClick={() => setRejectOpen(false)}>取消</button>
                  <button className="primary" disabled={!rejectReason.trim()} onClick={handleRejectConfirm}>
                    确认拒绝
                  </button>
                </div>
              </div>
            </div>
        )}
        <ConfirmDialog
            open={confirmReset}
            title="重置后续流程？"
            description={`发送后将从当前步骤开始重新执行，清除以下后续步骤的消息记录并重置为未开始（当前步骤重新进入进行中）：`}
            rows={resetRows}
            onConfirm={handleConfirmReset}
            onCancel={() => setConfirmReset(false)}
            confirmText="确认发送并重置"
        />
        {toast && <ErrorToast message={toast} onClose={() => setToast('')}/>}
      </>
  )
}
