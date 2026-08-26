import {Fragment, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {LlmErrorEvent, StepData, StreamChunkEvent, TaskSummary, ThinkingChunkEvent, ToolCallParamEvent, ToolCallResultEvent, ToolCallStartEvent,} from '../api/types'
import ChatMessage from '../components/ChatMessage'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ConfirmDialog, {type ConfirmRow} from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ProgressRail from '../components/ProgressRail'
import ToolCard from '../components/ToolCard'
import FileTree from '../editor/FileTree'
import {ContextMeter, TokenDetails, TokenMetrics} from '../components/TokenUsageBar'
import {Icon} from '../components/icons'
import {useLiveTools} from '../hooks/useLiveTools'
import {useStepStream} from '../hooks/useStepStream'
import {useStepMetrics} from '../hooks/useStepMetrics'
import {useTaskPolling} from '../hooks/useTaskPolling'
import {useChatAutoScroll} from '../hooks/useChatAutoScroll'
import {extractDecisionPackage, parseDecisionPackage,} from '../utils/decisionPkg'

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

export default function StepDetail({taskId, stepId}: StepDetailProps) {
  const navigate = useNavigate()
  const [data, setData] = useState<StepData | null>(null)
  const [task, setTask] = useState<TaskSummary | null>(null)
  const [streaming, setStreaming] = useState(false)

  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, settleStep, mergeBackend
  } = useStepMetrics()

  const [streamRounds, setStreamRounds] = useState<StreamRound[]>([])
  const roundIdRef = useRef(0)
  const [llmError, setLlmError] = useState<LlmErrorEvent | null>(null)
  const [retryProgress, setRetryProgress] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')

  const [pendingSends, setPendingSends] = useState<{ id: number; content: string }[]>([])
  const pendingIdRef = useRef(0)

  const [liveCallIds, setLiveCallIds] = useState<Set<string>>(() => new Set())

  const [expandedTools, setExpandedTools] = useState<Set<string>>(() => new Set())
  const liveTools = useLiveTools()

  const newRound = useCallback((): StreamRound => ({id: ++roundIdRef.current, thinking: '', text: '', toolCallIds: []}), [])
  const appendRound = useCallback((fn: (rounds: StreamRound[]) => StreamRound[]) => {
    setStreamRounds((prev) => fn(prev.length ? prev : [newRound()]))
  }, [newRound])

  const CodeEditor = lazy(() => import('../editor/CodeEditor'))
  const [view, setView] = useState<'chat' | 'code'>('chat')
  const [openedPath, setOpenedPath] = useState<string | null>(null)
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

  const latestAssistant = useMemo(() => {
    const msgs = data?.conversation ?? []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'assistant' && (m.content || '').trim()) return String(m.content)
    }
    return ''
  }, [data])

  const pkg = useMemo(() => parseDecisionPackage(latestAssistant), [latestAssistant])

  const hasDecisionPkg = useMemo(
      () => (data?.conversation ?? []).some((m) => m.role === 'assistant' && parseDecisionPackage(m.content ?? '')),
      [data],
  )

  const lastPkgSeq = useMemo(() => {
    let seq: number | null = null
    for (const m of data?.conversation ?? []) {
      if (m.role === 'assistant' && m.seq != null && extractDecisionPackage(m.content ?? '')) seq = m.seq
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
      } else if (selectedOpt != null && pkg?.options?.[selectedOpt]) {
        const opt = pkg.options[selectedOpt]
        const label = String.fromCharCode(65 + selectedOpt)
        const note = choiceNote.trim()
        msg = `【决策选择】用户选择选项 ${label}：${opt.option}${note ? `\n补充说明：${note}` : ''}`
      } else {
        return
      }

      await api.approveGate(taskId, stepId, msg)
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
  }, [selectedOpt, customText, pkg, choiceNote, taskId, stepId])
  const handleApprove = useCallback(async () => {
    setApproveBusy(true)
    try {
      await api.approveGate(taskId, stepId)
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

  const loadStep = useCallback(async () => {
    let lastErr: unknown
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const d = await api.getStep(taskId, stepId, {limit: 200})
        setData(d)
        return
      } catch (err) {
        lastErr = err
        if (attempt < 4) {
          await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt))
        }
      }
    }
    setToast(lastErr instanceof Error ? lastErr.message : '消息加载失败，请刷新重试')
  }, [taskId, stepId])

  const [loadingMore, setLoadingMore] = useState(false)
  const loadEarlier = useCallback(() => {
    const msgs = data?.messages
    const oldest = msgs && msgs.length ? msgs[0].seq : undefined
    if (oldest == null || loadingMore) return
    setLoadingMore(true)
    api
        .getStep(taskId, stepId, {limit: 200, beforeSeq: oldest})
        .then((d) => {
          setData((prev) =>
              prev
                  ? {
                    ...prev,
                    messages: [...d.messages, ...prev.messages],
                    conversation: [...d.messages, ...prev.conversation],
                    total: d.total,
                    truncated: d.truncated,
                    max_seq: d.max_seq,
                  }
                  : prev,
          )
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
  useTaskPolling(1000, loadMeta, [loadMeta])

  const addLiveCall = useCallback((callId: string) => {
    setLiveCallIds((prev) => {
      const next = new Set(prev)
      next.add(callId)
      return next
    })
  }, [])

  const clearLive = useCallback(() => {
    setLiveCallIds(new Set())
  }, [])

  const handleChunk = useCallback((e: StreamChunkEvent) => {
    sampleFirstChunk()
    accrueRoundComp(e.chunk.length)
    appendRound((rounds) => {
      const next = [...rounds]
      const last = next[next.length - 1]
      next[next.length - 1] = {...last, text: last.text + e.chunk}
      return next
    })
  }, [appendRound, sampleFirstChunk, accrueRoundComp])

  const handleStepStart = useCallback(() => {
    setStreaming(true)
    const at = Date.now()

    resetStep(at)
    setLlmError(null)

    setStreamRounds([])
  }, [resetStep])

  useEffect(() => {
    setRetryProgress(null)
  }, [stepId])

  const handleStreamEnd = useCallback(() => {
    setStreaming(false)
    const at = Date.now()
    endRound(at)
    clearLive()
    void loadStep().then(() => setStreamRounds([]))
  }, [clearLive, loadStep, endRound])

  const handleFullRerender = useCallback(() => {
    setStreaming(false)
    clearLive()
    void loadStep().then(() => setStreamRounds([]))
  }, [clearLive, loadStep])

  const handleUserMessage = useCallback(() => {
    void loadStep().then(() => setPendingSends([]))
  }, [loadStep])

  const handleThinkingChunk = useCallback(
      (e: ThinkingChunkEvent) => {
        sampleFirstChunk()
        accrueRoundComp(e.chunk.length)
        appendRound((rounds) => {
          const next = [...rounds]
          let last = next[next.length - 1]
          if (last.text) {
            last = newRound()
            next.push(last)
          }
          next[next.length - 1] = {...last, thinking: last.thinking + e.chunk}
          return next
        })
      },
      [appendRound, newRound, sampleFirstChunk, accrueRoundComp],
  )

  const handleRefresh = useCallback(() => {

    loadStep()
  }, [loadStep])

  const handleToolStart = useCallback(
      (e: ToolCallStartEvent) => {
        addLiveCall(e.callId)
        liveTools.start(e.callId, e.toolName, e.input)
        liveTools.markRendered(e.callId)
        appendRound((rounds) => {
          const next = [...rounds]
          const last = next[next.length - 1]
          next[next.length - 1] = {...last, toolCallIds: [...last.toolCallIds, e.callId]}
          return next
        })
      },
      [addLiveCall, liveTools, appendRound],
  )

  const handleToolParam = useCallback(
      (e: ToolCallParamEvent) => {
        liveTools.appendParam(e.callId, e.delta)

        accrueRoundComp(e.delta.length)
      },
      [liveTools, accrueRoundComp],
  )

  const handleToolExecuting = useCallback(() => {
    setStreaming(false)
    setStreamRounds((prev) => {
      if (!prev.length) return prev
      const last = prev[prev.length - 1]

      if (!last.thinking && !last.text && !last.toolCallIds.length) return prev

      return [
        ...prev.slice(0, -1),
        {...last, thinking: ''},
        newRound(),
      ]
    })
  }, [newRound])

  const handleToolResult = useCallback(
      (e: ToolCallResultEvent) => {
        liveTools.result(e.callId, e.output)

        setStreaming(true)

        const req = metricRequestRef.current
        if (req) req.reqStartedAt = Date.now()
      },
      [liveTools],
  )

  const handleLlmError = useCallback(
      (e: LlmErrorEvent) => {
        if (e.stepId === stepId) setLlmError(e)
        setToast(e.message || 'LLM 错误')

        setStreaming(false)

        settleStep(Date.now())
        clearLive()
        void loadStep().then(() => setStreamRounds([]))
      },
      [stepId, clearLive, loadStep, settleStep],
  )

  const handleRetry = useCallback(async () => {
    setBusy(true)
    try {
      await api.resumeStep(taskId, stepId)
      await api.startTask(taskId)
      setLlmError(null)
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
    onRefresh: handleRefresh,
    onFullRerender: handleFullRerender,
    onRetry: (_e, n) => setRetryProgress(n),
    onRetryClear: () => setRetryProgress(null),
  })

  const [confirmReset, setConfirmReset] = useState(false)
  const [pendingMsg, setPendingMsg] = useState('')
  const doIntervene = useCallback(
      async (mode: 'send' | 'force_inject' | 'stop', content: string) => {
        setBusy(true)

        const isMsg = mode !== 'stop'
        const id = ++pendingIdRef.current
        if (isMsg) setPendingSends((prev) => [...prev, {id, content}])
        try {
          await api.stepIntervene(taskId, stepId, mode, content)
          if (mode === 'stop') {

            setStreaming(false)
            clearLive()
            void loadStep().then(() => setStreamRounds([]))
          }
        } catch (err) {
          if (isMsg) setPendingSends((prev) => prev.filter((p) => p.id !== id))
          setToast(err instanceof Error ? err.message : '发送失败，请稍后重试')
        } finally {
          setBusy(false)
        }
      },
      [taskId, stepId, clearLive, loadStep],
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
      clearLive()
      loadStep()
    } catch (err) {
      setToast(err instanceof Error ? err.message : '压缩失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, stepId, clearLive, loadStep])

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

  const contentRef = useRef<HTMLDivElement>(null)
  const [autoScrollOn, setAutoScrollOn] = useState(true)
  const stepRunning = streaming || steps.find((s) => s.step_id === stepId)?.status === 'active'
  const autoScroll = useChatAutoScroll(
      contentRef,
      [
        streamRounds,
        pendingSends,
        liveCallIds.size,
        liveTools.tools,
        data,
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

  const canResume = curStep?.status === 'stopped'
      || (curStep?.status === 'pending' && task?.status === 'paused')
  const handleResumeStep = useCallback(async () => {
    if (!curStep) return
    if (curStep.status === 'stopped') {
      setBusy(true)
      try {
        await api.resumeStep(taskId, stepId)
        await api.startTask(taskId)
      } catch (err) {
        setToast(err instanceof Error ? err.message : '操作失败，请稍后重试')
      } finally {
        setBusy(false)
      }
    } else if (curStep.status === 'pending' && task?.status === 'paused') {

      setBusy(true)
      try {
        await api.startTask(taskId)
      } catch (err) {
        setToast(err instanceof Error ? err.message : '操作失败，请稍后重试')
      } finally {
        setBusy(false)
      }
    }
  }, [taskId, stepId, curStep, task?.status])

  const handleToggleBestEffort = useCallback(async () => {
    if (!task) return
    const next = !task.best_effort
    setTask({...task, best_effort: next})
    try {
      await api.setBestEffort(taskId, next)
      loadMeta()
    } catch (err) {
      setTask({...task, best_effort: !next})
      setToast(err instanceof Error ? err.message : '切换尽力模式失败，请稍后重试')
    }
  }, [taskId, task, loadMeta])

  const visibleMessages = useMemo(() => {
    if (!data) return []
    return data.conversation.filter((m) => {
      if (m.role !== 'tool') return true
      const callId = m.tool_call_id ?? (m.seq != null ? `seq-${m.seq}` : '')
      return !liveCallIds.has(callId)
    })
  }, [data, liveCallIds])

  const systemMsg = visibleMessages.find((m) => m.role === 'system')
  const restMsgs = visibleMessages.filter((m) => m.role !== 'system')

  const toolExpanded = useMemo(() => {
    const expanded = new Set<string>()
    let lastDoneId: string | null = null
    for (const r of streamRounds) {
      for (const callId of r.toolCallIds) {
        const t = liveTools.tools.get(callId)
        if (!t) continue
        if (t.status === 'running') expanded.add(callId)
        else if (t.status === 'done') lastDoneId = callId
      }
    }
    if (lastDoneId) expanded.add(lastDoneId)
    return expanded
  }, [streamRounds, liveTools.tools])
  return (
      <>
        {}
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
          {}
          {canResume && (
              <button className="run-toggle-btn" onClick={handleResumeStep} disabled={busy} title="恢复当前步骤">
                <Icon name="play" size={13} gap={4}/>
                恢复
              </button>
          )}
          {}
          <button
              className={`best-effort-toggle${task?.best_effort ? ' active' : ''}`}
              onClick={handleToggleBestEffort}
              title="尽力模式：用户不在线时 gate 自动放行、AI 丧气时自动提醒继续"
          >
            <span className="toggle-dot"/>
            尽力模式
          </button>
          {}
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
        {}
        {view === 'code' && (
            <div className="flow-code-split">
              <FileTree onOpenFile={setOpenedPath} externalModifiedPaths={externalModified}/>
              <Suspense fallback="加载编辑器...">
                <CodeEditor
                    path={openedPath}
                    onDirtyChange={setEditorDirty}
                    onExternalModified={handleExternalModified}
                />
              </Suspense>
            </div>
        )}
        {view === 'chat' && (
            <>
              <div
                  className="content-area"
                  ref={contentRef}
              >
                {}
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
                      <span className="step-badge">状态: {step.status}</span>
                    </div>
                )}
                {}
                <div className="chat-log">
                  {}
                  {data?.truncated && (
                      <div className="history-fold">
                        <button className="history-load-more" onClick={loadEarlier} disabled={loadingMore}>
                          {loadingMore ? '加载中...' : `加载更早消息（共 ${data.total ?? '?'} 条，已显示 ${data.messages.length} 条）`}
                        </button>
                      </div>
                  )}
                  {}
                  {systemMsg && <ChatMessage msg={systemMsg}/>}
                  {data?.prep?.step_context && (

                      <div className="msg-user">
                        <div className="user-bubble">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.prep.step_context}</ReactMarkdown>
                        </div>
                      </div>
                  )}
                  {restMsgs.map((m, i) => {
                    const dp = m.role === 'assistant' ? extractDecisionPackage(m.content ?? '') : null
                    if (dp) {
                      const isLastPkg = m.seq != null && m.seq === lastPkgSeq
                      return (
                          <Fragment key={m.seq != null ? m.seq : i}>
                            {}
                            <ChatMessage
                                msg={{...m, content: dp.before + dp.after}}
                                onToggleTool={toggleTool}
                                expandedTools={expandedTools}
                            />
                            {}
                            {isGatePending && isLastPkg && (
                                <div className="gate-review-card gate-action-card">
                                  <div className="gate-review-head">
                                    <Icon name="settings" size={13} gap={6}/>
                                    人工审批 — 请审阅上方决策信息后拍板
                                  </div>
                                  <div className="gate-options">
                                    {dp.pkg.options?.map((opt, j) => (
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
                                  {}
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
                                  </div>
                                </div>
                            )}
                          </Fragment>
                      )
                    }
                    return (
                        <Fragment key={m.seq != null ? m.seq : i}>
                          <ChatMessage msg={m} onToggleTool={toggleTool} expandedTools={expandedTools}/>
                        </Fragment>
                    )
                  })}
                  {}
                  {pendingSends.map((p) => (
                      <div className="msg-user" key={p.id}>
                        <div className="user-bubble sending">
                          {p.content}
                          <span className="sending-tag">
                  <span className="loading-spinner"/>
                  发送中…
                </span>
                        </div>
                      </div>
                  ))}
                  {}
                  {streamRounds.map((r, ri) => {
                    const isLast = ri === streamRounds.length - 1
                    return (
                        <Fragment key={r.id}>
                          {}
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
                          {}
                          {r.text ? (
                              <div className="ai-block">
                                <div className="ai-text">
                                  {isLast && streaming ? (
                                      <>
                                        {r.text}
                                        <span className="stream-cursor"/>
                                      </>
                                  ) : (
                                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.text}</ReactMarkdown>
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
                          {}
                          {r.toolCallIds.map((callId) => {
                            const t = liveTools.tools.get(callId)
                            if (!t) return null
                            return (
                                <div className="ai-tool-inline" key={callId}>
                                  <ToolCard
                                      callId={callId}
                                      toolName={t.toolName}
                                      input={t.input}
                                      output={t.output}
                                      status={t.status}
                                      defaultExpanded={toolExpanded.has(callId)}
                                      autoScroll={autoScrollOn && stepRunning}
                                  />
                                </div>
                            )
                          })}
                        </Fragment>
                    )
                  })}
                  {}
                  {retryProgress != null && (
                      <div className="retry-progress">
                        <Icon name="refresh" size={13} gap={8}/>
                        请求被限流，正在重试 ({retryProgress}/10)...
                      </div>
                  )}
                  {}
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
                  {}
                  {isGatePending && !hasDecisionPkg && (
                      <div className="gate-review-card">
                        <div className="gate-review-head">
                          <Icon name="settings" size={13} gap={6}/>
                          人工审批 — 请审阅后拍板
                        </div>
                        <div className="gate-review-body">
                          {latestAssistant ? (
                              <div className="gate-review-info">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>{latestAssistant}</ReactMarkdown>
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
                {}
                {autoScroll.showJump && (
                    <button className="scroll-jump-btn" onClick={autoScroll.jumpToBottom} title="回到最新输出">
                      <Icon name="chevronDown" size={16} gap={0}/>
                    </button>
                )}
              </div>
              {}
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
        {}
        {choiceOpen && pkg?.options?.[selectedOpt ?? -1] && (
            <div className="modal-overlay show" onClick={() => setChoiceOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h3>按所选选项继续</h3>
                <div className="field">
                  <div className="field-label">选项 {String.fromCharCode(65 + (selectedOpt ?? 0))}</div>
                  <div className="choice-option-text">{pkg?.options?.[selectedOpt ?? -1]?.option}</div>
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
        {}
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
        {}
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
