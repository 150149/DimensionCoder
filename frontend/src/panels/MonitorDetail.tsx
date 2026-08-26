import {Fragment, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {useNavigate} from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {api} from '../api/client'
import type {LlmErrorEvent, Message, StepStats} from '../api/types'
import ChatMessage from '../components/ChatMessage'
import ConfirmDialog from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ToolCard from '../components/ToolCard'
import {ContextMeter, TokenDetails, TokenMetrics} from '../components/TokenUsageBar'
import {Icon} from '../components/icons'
import {useLiveTools} from '../hooks/useLiveTools'
import {useStepStream} from '../hooks/useStepStream'
import {useStepMetrics} from '../hooks/useStepMetrics'
import {useTaskPolling} from '../hooks/useTaskPolling'
import {useChatAutoScroll} from '../hooks/useChatAutoScroll'

interface MonitorDetailProps {
  taskId: string
  stepId: string
}

function decisionOf(m: Message): { action: string; reasoning: string } | null {
  const toolName = m.toolName ?? m.tool_name ?? ''
  if (toolName !== 'dcflow_adjust_flow') return null
  let input: unknown = m.input
  if (typeof input === 'string') {
    try {
      input = JSON.parse(input)
    } catch {
      return null
    }
  }
  if (input == null || typeof input !== 'object') return null
  const obj = input as Record<string, unknown>
  const action = typeof obj.action === 'string' ? obj.action : ''
  if (!action || action === 'no_change') return null
  const reasoning = typeof obj.reasoning === 'string' ? obj.reasoning : ''
  return {action, reasoning}
}

function canonicalMonitorStepId(stepId: string): string {
  return stepId
}

export default function MonitorDetail({taskId, stepId}: MonitorDetailProps) {
  const navigate = useNavigate()

  const instId = canonicalMonitorStepId(stepId)

  useEffect(() => {
    if (instId !== stepId) {
      navigate(`/task/${taskId}/monitor/${instId}`, {replace: true})
    }
  }, [taskId, stepId, instId, navigate])
  const [messages, setMessages] = useState<Message[]>([])

  const [prepInfo, setPrepInfo] = useState<{ system_prompt: string; step_context: string } | null>(null)

  const [llmError, setLlmError] = useState<LlmErrorEvent | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')

  const [thinkingText, setThinkingText] = useState('')
  const thinkingTextRef = useRef('')
  const appendThinking = useCallback((chunk: string) => {
    thinkingTextRef.current += chunk
    setThinkingText(thinkingTextRef.current)
  }, [])
  const clearThinking = useCallback(() => {
    thinkingTextRef.current = ''
    setThinkingText('')
  }, [])
  const [liveCallIds, setLiveCallIds] = useState<Set<string>>(() => new Set())
  const [toast, setToast] = useState('')

  const [taskStatus, setTaskStatus] = useState('')

  const [initialLoading, setInitialLoading] = useState(true)

  const [taskPauseLevel, setTaskPauseLevel] = useState('')

  const [monitorSaved, setMonitorSaved] = useState(false)

  const [instStatus, setInstStatus] = useState('')

  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, mergeBackend
  } = useStepMetrics()
  const [busy, setBusy] = useState(false)
  const [rebuildOpen, setRebuildOpen] = useState(false)
  const [rebuildPending, setRebuildPending] = useState('')
  const liveTools = useLiveTools()

  useEffect(() => {
    let alive = true
    api
        .getStep(taskId, instId)
        .then((d) => {
          if (alive && d?.prep?.step_context) {
            setPrepInfo({system_prompt: d.prep.system_prompt || '', step_context: d.prep.step_context})
          }
        })
        .catch(() => {
        })
    return () => {
      alive = false
    }
  }, [taskId, instId])

  const [contextWindow, setContextWindow] = useState(400000)

  const [lightPrices, setLightPrices] = useState({in: 0, cached: 0, out: 0})
  const [powerPrices, setPowerPrices] = useState({in: 0, cached: 0, out: 0})
  useEffect(() => {
    let alive = true
    api
        .getConfig()
        .then((cfg) => {
          if (alive) {
            setContextWindow(cfg.contextWindow ?? 400000)
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

  const [stepTokens, setStepTokens] = useState<{ prompt: number; cached: number; completion: number } | null>(null)

  const [monitorStats, setMonitorStats] = useState<Record<string, StepStats>>({})

  const contentRef = useRef<HTMLDivElement>(null)
  const [autoScrollOn, setAutoScrollOn] = useState(true)
  const monitorRunning = streaming || instStatus === 'active'
  const autoScroll = useChatAutoScroll(
      contentRef,
      [
        streamText,
        thinkingText,
        messages,
        liveCallIds.size,
        liveTools.tools,
        streaming,
      ],
      autoScrollOn && monitorRunning,
  )

  const initialScrolledRef = useRef(false)
  useEffect(() => {
    if (!initialLoading && !initialScrolledRef.current) {
      initialScrolledRef.current = true
      const el = contentRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [initialLoading])

  const loadConv = useCallback(() => {
    return Promise.all([
      api.getMonitorConversations(taskId).catch(() => null),
      api.getTask(taskId).catch(() => null),
      api.getStepMessages(taskId, instId).catch(() => null),
    ]).then(([mdata, task, live]) => {
      if (task) {
        setTaskStatus(task.task.status)
        setTaskPauseLevel(task.task.pause_level ?? '')
      }
      const instLive = mdata?.monitor_steps?.[instId] ?? ''
      setInstStatus(instLive)

      if (instLive && instLive !== 'active') {
        setStreaming(false)
        setStreamText('')
        clearThinking()
        setLiveCallIds(new Set())
      }

      setMonitorSaved(instLive === 'completed')

      if (live) setMessages(live.messages ?? [])

      const st = mdata?.step_tokens?.[instId]
      setStepTokens(
          st
              ? {prompt: st.token_prompt ?? 0, cached: st.token_cached ?? 0, completion: st.token_completion ?? 0}
              : null,
      )
      setMonitorStats(mdata?.step_stats ?? {})

      mergeBackend(mdata?.step_stats?.[instId])
      setInitialLoading(false)
    })
  }, [taskId, instId])
  useTaskPolling(1000, loadConv, [loadConv])

  const matchStep = useCallback(
      (eventStepId: string): boolean => eventStepId === instId,
      [instId],
  )

  useStepStream(
      taskId,
      instId,
      {
        onStepStart: () => {
          setStreaming(true)

          resetStep(Date.now())
        },
        onChunk: (e) => {
          setStreamText((prev) => prev + e.chunk)
          sampleFirstChunk()
          accrueRoundComp(e.chunk.length)
        },
        onThinkingChunk: (e) => {
          appendThinking(e.chunk)
          sampleFirstChunk()
          accrueRoundComp(e.chunk.length)
        },
        onStreamEnd: () => {
          setStreaming(false)
          endRound(Date.now())
          setStreamText('')
          clearThinking()
          setLiveCallIds(new Set())
          loadConv()
        },
        onToolStart: (e) => {

          clearThinking()
          setLiveCallIds((prev) => {
            const next = new Set(prev)
            next.add(e.callId)
            return next
          })
          liveTools.start(e.callId, e.toolName, e.input)
          liveTools.markRendered(e.callId)
        },

        onToolParam: (e) => {
          liveTools.appendParam(e.callId, e.delta)

          accrueRoundComp(e.delta.length)
        },
        onToolResult: (e) => liveTools.result(e.callId, e.output),
        onToolExecuting: () => {

          setStreamText('')
        },
        onFullRerender: () => loadConv(),
        onRefresh: () => loadConv(),
        onLlmError: (e) => {
          setLlmError(e)
          setToast(e.message || 'LLM 错误')

          setStreaming(false)
          setStreamText('')
          clearThinking()
          setLiveCallIds(new Set())
          loadConv()
        },
      },
      matchStep,
  )

  const visibleMessages = useMemo(() => {
    return messages.filter((m) => {
      if (m.role !== 'tool') return true
      const callId = m.tool_call_id ?? (m.seq != null ? `seq-${m.seq}` : '')
      return !liveCallIds.has(callId)
    })
  }, [messages, liveCallIds])

  const liveCards = useMemo(
      () => [...liveTools.tools.entries()].filter(([callId]) => liveCallIds.has(callId)),
      [liveTools.tools, liveCallIds],
  )

  const systemMsg = visibleMessages.find((m) => m.role === 'system')
  const restMsgs = visibleMessages.filter((m) => m.role !== 'system')

  const title =
      instId === 'review'
          ? '最终审查'
          : instId === 'report'
              ? '产出报告'
              : instId.startsWith('monitor-intervene')
                  ? '介入审查'
                  : instId === 'monitor-init'
                      ? '初始编排'
                      : instId.startsWith('monitor-')
                          ? `Monitor 审查 ${instId}`
                          : instId

  const isTailStep = instId === 'monitor-init' || instId === 'review' || instId === 'report'
  const handleSend = useCallback(
      async (content: string) => {
        if (taskStatus === 'paused' && taskPauseLevel === 'step' && !monitorSaved) {
          setBusy(true)
          try {
            await api.monitorControl(taskId, 'resume', content, instId)
          } catch (err) {
            setToast(err instanceof Error ? err.message : '发送失败，请稍后重试')
          } finally {
            setBusy(false)
          }
          return
        }
        if ((taskStatus === 'completed' || monitorSaved) && !isTailStep) {
          setRebuildPending(content)
          setRebuildOpen(true)
          return
        }
        setBusy(true)
        try {
          if (taskStatus === 'paused' && !monitorSaved) {
            await api.monitorControl(taskId, 'resume', content, instId)
          } else {

            await api.stepIntervene(taskId, instId, 'send', content)
          }
        } catch (err) {
          setToast(err instanceof Error ? err.message : '发送失败，请稍后重试')
        } finally {
          setBusy(false)
        }
      },
      [taskId, taskStatus, taskPauseLevel, monitorSaved, isTailStep, instId],
  )

  const handleRebuildConfirm = useCallback(async () => {
    const content = rebuildPending
    setRebuildOpen(false)
    setBusy(true)
    try {

      await api.flowIntervene(taskId, 'rebuild', content, instId)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '重跑失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, rebuildPending, instId])

  const handleForce = useCallback(
      async (content: string) => {
        setBusy(true)
        try {
          await api.flowIntervene(taskId, 'immediate', content)
        } catch (err) {
          setToast(err instanceof Error ? err.message : '强制介入失败，请稍后重试')
        } finally {
          setBusy(false)
        }
      },
      [taskId],
  )

  const handleStop = useCallback(async () => {
    setBusy(true)
    try {

      await api.monitorControl(taskId, 'stop', '', instId)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '暂停失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, instId])

  const handleResume = useCallback(async () => {
    setBusy(true)
    try {

      await api.monitorControl(taskId, 'resume', '', instId)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '恢复失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, instId])

  const handleRetry = useCallback(async () => {
    setBusy(true)
    try {
      await api.resumeStep(taskId, instId)
      await api.startTask(taskId)
      setLlmError(null)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '重试失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, instId])

  const handleCompress = useCallback(async () => {
    setBusy(true)
    try {
      await api.compressStep(taskId, instId)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '压缩失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, instId])

  return (
      <>
        {}
        <div className="top-bar top-bar-monitor">
          <a className="back-btn" onClick={() => navigate(`/task/${taskId}`)}>
            <Icon name="arrowLeft" size={13} gap={2}/>
            返回
          </a>
          <div className="step-header">
            <span className="monitor-title">Monitor 思考过程 — {title}</span>
            <span className="monitor-badge">Monitor Agent</span>
          </div>
          {}
          {(!monitorSaved && (instStatus === 'stopped' ||
              (taskStatus === 'paused' && instStatus === 'pending'))) && (
              <button className="run-toggle-btn" onClick={handleResume} disabled={busy} title="恢复执行 Monitor">
                <Icon name="play" size={13} gap={4}/>
                恢复
              </button>
          )}
        </div>
        <div
            className="content-area"
            ref={contentRef}
        >
          {}
          <div className="chat-log">
            {}
            {systemMsg && <ChatMessage msg={systemMsg}/>}
            {prepInfo?.step_context && (
                <div className="msg-user">
                  <div className="user-bubble">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{prepInfo.step_context}</ReactMarkdown>
                  </div>
                </div>
            )}
            {restMsgs.map((m, i) => {
              const decision = decisionOf(m)
              return (
                  <Fragment key={m.seq != null ? m.seq : i}>
                    <ChatMessage msg={m}/>
                    {decision && (
                        <div className="decision-card">
                          <div className="dc-label">Monitor 决策: {decision.action}</div>
                          <div className="dc-reasoning">{decision.reasoning}</div>
                        </div>
                    )}
                  </Fragment>
              )
            })}
            {}
            {liveCards.map(([callId, t]) => (
                <div className="ai-tool-inline" key={callId}>
                  <ToolCard callId={callId} toolName={t.toolName} input={t.input} output={t.output} status={t.status}/>
                </div>
            ))}
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
                        <button className="retry-btn" onClick={handleRetry} disabled={busy}>
                          <Icon name="refresh" size={13} gap={5}/>
                          重试
                        </button>
                    )}
                    {}
                    {!llmError.retryable && (
                        <button className="retry-btn" onClick={handleResume} disabled={busy}>
                          <Icon name="play" size={13} gap={5}/>
                          恢复
                        </button>
                    )}
                  </div>
                </div>
            )}
            {}
            {thinkingText && (
                <div className="ai-thinking-inline">
                  <div className="think-box">
                    <div className="think-body" style={{display: 'block'}}>
                      <div className="ai-text">
                        {thinkingText}
                        <span className="stream-cursor"/>
                      </div>
                    </div>
                  </div>
                </div>
            )}
            {}
            {streaming && (
                <div className="ai-block">
                  <div className="ai-text">
                    {streamText ? (
                        <>
                          {streamText}
                          <span className="stream-cursor"/>
                        </>
                    ) : (
                        <span className="loading-text">
                    <span className="loading-spinner"/>
                    Monitor 正在思考...
                  </span>
                    )}
                  </div>
                </div>
            )}
            {initialLoading ? (
                <div className="chat-status">加载中...</div>
            ) : messages.length === 0 && !streaming ? (
                <div className="chat-status">
                  该步骤暂无 Monitor 对话记录
                  <br/>
                  <small>Monitor 只在实际产生了编排决策时运行</small>
                </div>
            ) : null}
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
          <ContextMeter windowSize={contextWindow} prompt={stepTokens?.prompt ?? 0}/>
          <InterveneBar
              mode="flow"
              onSend={handleSend}
              onForce={handleForce}

              onStop={instStatus === 'active' || streaming ? handleStop : undefined}
              onCompress={handleCompress}

              running={instStatus === 'active' || streaming}
              busy={busy}
              autoScroll={autoScrollOn}
              onToggleAutoScroll={() => setAutoScrollOn((v) => !v)}
          />
          {}
          <TokenDetails
              prompt={stepTokens?.prompt ?? 0}
              cached={stepTokens?.cached ?? 0}
              completion={stepTokens?.completion ?? 0}
              cost={(stepTokens ? (Math.max(0, stepTokens.prompt - stepTokens.cached) * powerPrices.in
                  + stepTokens.cached * powerPrices.cached
                  + stepTokens.completion * powerPrices.out) / 1e6 : 0)}
              trailing={

                (() => {
                  const st = monitorStats?.[instId]
                  const hasStats = !!st && (st.requests ?? 0) > 0
                  const live = metrics.startedAt != null
                  return (
                      <TokenMetrics
                          startedAt={metrics.startedAt} endedAt={metrics.endedAt}
                          activeSince={metrics.activeSince}
                          ttftMs={metrics.ttftSamples ? metrics.ttftMs
                              : (hasStats && (st?.ttft_samples ?? 0) > 0
                                  ? (st?.ttft_total_ms ?? 0) / (st?.ttft_samples ?? 1)
                                  : null)}
                          active={instStatus === 'active' || streaming}
                          runDurationMs={live ? metrics.runDurationMs : (hasStats ? (st?.run_duration_ms ?? 0) : 0)}
                          outputDurationMs={live ? metrics.outputDurationMs : (hasStats ? (st?.output_duration_ms ?? 0) : 0)}
                          requestCount={live ? metrics.requests : (hasStats ? (st?.requests ?? 0) : 0)}
                          completion={live ? metrics.completion : (stepTokens?.completion ?? 0)}
                          roundFirstAt={live ? (metricRequestRef.current?.firstTokenAt ?? null) : null}
                          roundComp={live ? roundCompRef.current : 0}
                      />
                  )
                })()
              }
          />
        </div>
        {}
        <ConfirmDialog
            open={rebuildOpen}
            title="重新执行当前 Monitor 步骤？"
            description="当前 monitor 步骤已完成，发送后将清理后续步骤的消息和产出，并重新执行当前 monitor 步骤。"
            rows={[]}
            confirmText="确认发送并重跑"
            onConfirm={handleRebuildConfirm}
            onCancel={() => setRebuildOpen(false)}
        />
        {toast && <ErrorToast message={toast} onClose={() => setToast('')}/>}
      </>
  )
}
