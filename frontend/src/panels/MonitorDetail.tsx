// ═══════════════════════════════════════════════════════════════
// MonitorDetail（SWP4-C / WP4-3 §3.8，2026-08-21 实体化重构）
// - Props：{taskId, stepId}（路由 = 实体步骤 id：monitor-init / monitor-N /
//   monitor-intervene-N / review / report）
// - 数据源单一：getStepMessages(taskId, instId)（审查/收尾步骤为真实行，消息
//   完整存于 step_messages）；状态 monitor_steps[instId] / token step_tokens[instId]
//   均由端点 29 按实体 id 提供——删除 artifact 快照拼接与介入实例双拉取
// - SSE 订阅：事件 stepId === 当前步骤 id 精确匹配（无 _ 前缀/别名特判）
// - 旧路由重定向：_plan→monitor-init、_final→review、_report→report、
//   _monitor:*→monitor-*、step-X→monitor-step-X（迁移后旧书签/链接兼容）
// ═══════════════════════════════════════════════════════════════

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
import {ContextMeter, TokenDetails, TokenMetrics} from '../components/TokenUsageBar' // Token 展示
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

/** M9：决策卡判定——toolName=dcflow_adjust_flow 且 tool_input.action ≠ "no_change" */
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

/** 路由 stepId → 实体步骤 id（2026-08-21 去绑定普通化：旧数据已全部 data
 * patch——monitor-step-X → monitor-N、monitor-intervene → monitor-intervene-1，
 * 前端无映射需求；路由 stepId 即实体 id，原样返回。旧书签（monitor-step-X）
 * 打开后无对应行，页面显示空对话。 */
function canonicalMonitorStepId(stepId: string): string {
  return stepId
}

export default function MonitorDetail({taskId, stepId}: MonitorDetailProps) {
  const navigate = useNavigate()
  // 2026-08-21 实体化：路由 stepId 统一为实体步骤 id（旧虚拟 id 映射重定向）
  const instId = canonicalMonitorStepId(stepId)
  // 旧路由重定向（迁移前书签/链接兼容）：_plan/_final/_report/_monitor:*/step-X
  // → 实体 id——旧 id 无真实行，不重定向则消息空、SSE 不匹配；replace 不增历史
  useEffect(() => {
    if (instId !== stepId) {
      navigate(`/task/${taskId}/monitor/${instId}`, {replace: true})
    }
  }, [taskId, stepId, instId, navigate])
  const [messages, setMessages] = useState<Message[]>([])
  // 2026-08-22：提示词折叠（system_prompt + step_context，经 getStep 拉取——
  // 编排等待页已重定向到本页，用户需要像正常步骤一样查看完整提示词）
  const [prepInfo, setPrepInfo] = useState<{ system_prompt: string; step_context: string } | null>(null)
  // 2026-08-22：llmError 重试卡（编排失败时 FlowOverview 等待页已移除启动按钮，
  // 重试由本卡承担——与 StepDetail §3.7.5 同款交互）
  const [llmError, setLlmError] = useState<LlmErrorEvent | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  // 2026-08-21：思考分段（对齐 StepDetail V-19）——工具轮开始清空当前思考，
  // 只显示当前大模型请求的思考（2026-08-22：不再归档累积历史轮次）；此前
  // 单块持续累加（用户反馈）。ref 供 onToolStart 读取当前值（回调闭包拿不到最新 state）
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
  // 2026-08-20：任务状态（InterveneBar running/恢复按钮判定）+ 介入提交中 + rebuild 确认弹窗
  const [taskStatus, setTaskStatus] = useState('')
  // 首次数据加载完成前显示「加载中...」（避免闪「暂无 Monitor 对话」）
  const [initialLoading, setInitialLoading] = useState(true)
  // 暂停级别：'step'=monitor 被打断（发送=恢复执行）；'flow'=介入中/流程暂停
  // （当前步骤已完成时发送=弹窗清理重跑）
  const [taskPauseLevel, setTaskPauseLevel] = useState('')
  // 该步骤 Monitor 对话是否已完成（实例 completed，真实行状态持久）——已完成
  // 的 monitor 不显示恢复执行按钮（只有被打断未完成、真正暂停时才显示）
  const [monitorSaved, setMonitorSaved] = useState(false)
  // 2026-08-20 多实例：当前实例状态（monitor_steps[instId]，active=运行中）
  const [instStatus, setInstStatus] = useState('')
  // 2026-08-22：运行统计实时化（与 StepDetail 同 hook）——SSE 事件驱动实时
  // 采样/结算 + 轮询后端 stats 校准（max 合并）；此前纯 DB 驱动每轮落库才更新
  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, mergeBackend
  } = useStepMetrics()
  const [busy, setBusy] = useState(false)
  const [rebuildOpen, setRebuildOpen] = useState(false)
  const [rebuildPending, setRebuildPending] = useState('')
  const liveTools = useLiveTools()

  // 提示词：getStep 聚合端点对 monitor 步骤走 _monitor_prep（orchestrator
  // get_step_prep），返回 system_prompt/step_context；失败静默（页面主体不受影响）。
  // 2026-08-22：system_prompt 由 DB system 消息展示（执行时已落库），此处只取
  // step_context 渲染注入消息用户气泡（对齐 StepDetail）
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

  // Token 展示：上下文窗口总容量（config contextWindow，设置页可编辑）；拉取失败保持默认
  const [contextWindow, setContextWindow] = useState(400000)
  // 2026-08-23：light/power 六项价格——monitor 步骤固定 model_tier=power（用 power 组）
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

  // Token 展示：当前路由的虚拟 Monitor 步骤累计用量（端点 29 step_tokens，1s 轮询实时）
  const [stepTokens, setStepTokens] = useState<{ prompt: number; cached: number; completion: number } | null>(null)
  // 2026-08-21：当前实例运行统计（端点 29 step_stats——orchestrator 每轮 LLM
  // 流结束落库；TokenMetrics 展示输出速度/首字延迟/运行时长/请求数）
  const [monitorStats, setMonitorStats] = useState<Record<string, StepStats>>({})

  // 聊天流自动滚动：仅实例运行中（流式输出或 monitor_steps active）跟随底部；
  // 已完成/暂停实例自动滚动不生效——用户自由浏览历史，不会被 1s 轮询刷新拉回
  // （2026-08-23 对齐 StepDetail；2026-08-20「始终自动滚动」决策废除）
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
        liveTools.tools, // 工具结果到达/卡展开增高时也跟随（否则距底>阈值被误判滚离 → 自动滚动失效）
        streaming,
      ],
      autoScrollOn && monitorRunning,
  )
  // 进入页面初始定位到底部（看最新消息）；此后非运行实例滚动完全自由
  const initialScrolledRef = useRef(false)
  useEffect(() => {
    if (!initialLoading && !initialScrolledRef.current) {
      initialScrolledRef.current = true
      const el = contentRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [initialLoading])

  // 2026-08-21 实体化：数据源单一——审查/收尾步骤为真实行，消息直查
  // getStepMessages(taskId, instId)；状态（monitor_steps）/token（step_tokens）
  // 由端点 29 按实体 id 提供；删除 artifact 快照拼接与介入实例双拉取（介入
  // 也是 monitor-intervene 真实行，本页发消息/恢复时事件/消息都指向它）
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
      // 2026-08-21：stop 路径后端不发 streamEnd/llmError（用户打断直接退出）→
      // streaming 卡 true 会导致介入栏 running 误判（显示终止而非发送）；
      // 轮询发现实例非运行态（停止/完成/排队）时重置流式状态（与 StepDetail
      // 的 stop 后重拉同语义）
      if (instLive && instLive !== 'active') {
        setStreaming(false)
        setStreamText('')
        clearThinking()
        setLiveCallIds(new Set())
      }
      // 实例 completed = 本轮审查已完成（真实行状态持久，artifact 已保存）——
      // 已完成的 monitor 不显示恢复执行按钮
      setMonitorSaved(instLive === 'completed')
      // 2026-08-23：getStepMessages 失败（live 为 null）时保留旧消息——此前
      // setMessages([]) 清空全部对话（1s 轮询失败即闪断，持续失败则空白到
      // 刷新）；仅成功时更新，其余状态（instStatus/monitorSaved/stats）照常刷新
      if (live) setMessages(live.messages ?? [])
      // Token 展示：step_tokens（实时 DB 查询）中取当前实例（key = 步骤 id 直查）
      const st = mdata?.step_tokens?.[instId]
      setStepTokens(
          st
              ? {prompt: st.token_prompt ?? 0, cached: st.token_cached ?? 0, completion: st.token_completion ?? 0}
              : null,
      )
      setMonitorStats(mdata?.step_stats ?? {})
      // 2026-08-22：后端校准——当前实例 DB stats（tiktoken 精确、每轮落库）
      // max 合并到前端实时 metrics（SSE 事件丢失/断连时后端补齐）
      mergeBackend(mdata?.step_stats?.[instId])
      setInitialLoading(false)
    })
  }, [taskId, instId])
  useTaskPolling(1000, loadConv, [loadConv])

  // 2026-08-21 实体化：SSE 事件 step_id 即实体步骤 id，精确匹配当前实例
  // （删除 _ 前缀/介入别名特判——介入也是 monitor-intervene 真实行，本页
  // 发消息/恢复时事件自然匹配该行）
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
          // 2026-08-22：重置时序点（requests 由 streamEnd 计数）
          resetStep(Date.now())
        },
        onChunk: (e) => {
          setStreamText((prev) => prev + e.chunk)
          sampleFirstChunk()
          accrueRoundComp(e.chunk.length) // 2026-08-22：文本吐字计入进行中轮估算
        },
        onThinkingChunk: (e) => {
          appendThinking(e.chunk)
          sampleFirstChunk()
          accrueRoundComp(e.chunk.length) // 2026-08-22：思考吐字计入进行中轮估算
        },
        onStreamEnd: () => {
          setStreaming(false)
          endRound(Date.now()) // 2026-08-22：结算本轮（时长/请求数/completion）
          setStreamText('')
          clearThinking()
          setLiveCallIds(new Set())
          loadConv()
        },
        onToolStart: (e) => {
          // 2026-08-22：工具轮开始 → 清空当前思考（只保留当前大模型请求的
          // 思考，不再归档累积历史轮次）
          clearThinking()
          setLiveCallIds((prev) => {
            const next = new Set(prev)
            next.add(e.callId)
            return next
          })
          liveTools.start(e.callId, e.toolName, e.input)
          liveTools.markRendered(e.callId)
        },
        // 2026-08-22：工具参数流式逐片累积（V-18，与 StepDetail 对齐——此前
        // MonitorDetail 漏绑 onToolParam，参数逐字动画丢失）
        onToolParam: (e) => {
          liveTools.appendParam(e.callId, e.delta)
          // 2026-08-22：工具参数吐字计入进行中轮估算（输出速度分子实时）
          accrueRoundComp(e.delta.length)
        },
        onToolResult: (e) => liveTools.result(e.callId, e.output),
        onToolExecuting: () => {
          // 2026-08-21（用户反馈，与 thinking 同款问题）：工具开始执行 → 清空
          // 流式文本块——assistant 文本已由后端落库（轮询 1s 拉取展示），不清空
          // 则工具轮间的多段文本全部叠加在同一流式块。thinking 不落库故用归档
          // （thinkingBlocks），文本走清空（避免与消息流重复显示）
          setStreamText('')
        },
        onFullRerender: () => loadConv(),
        onRefresh: () => loadConv(),
        onLlmError: (e) => {
          setLlmError(e)
          setToast(e.message || 'LLM 错误')
          // 后端 llmError 路径不发 streamEnd → 流式文本残留 → 重拉清残留
          setStreaming(false)
          setStreamText('')
          clearThinking()
          setLiveCallIds(new Set())
          loadConv()
        },
      },
      matchStep,
  )

  // 轮询响应不覆盖已渲染卡：跳过 DB 中已 live 渲染（liveCallIds）的 tool 消息
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

  // 2026-08-22：顺序对齐后端组装（base_msgs = system_prompt → step_context →
  // 对话历史）——system 消息置顶（折叠区），注入消息（step_context 气泡）紧随
  // 其后，其余对话消息再排（与 StepDetail 同序）
  const systemMsg = visibleMessages.find((m) => m.role === 'system')
  const restMsgs = visibleMessages.filter((m) => m.role !== 'system')

  // 2026-08-21 实体化：标题按实体步骤 id（monitor-init/monitor-N/review/report）
  // 2026-08-21 去绑定普通化：monitor-N 独立编号审查实例；monitor-intervene-N 介入实例
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

  // 2026-08-20：介入栏回调（mode="flow"，语义与 FlowOverview 一致）——
  // 发送：
  // ① step 级暂停（pause_level='step'，monitor 被打断）→ 自动恢复执行
  //   （monitorControl resume 带消息，不弹窗不排队——用户发送即继续）；
  // ② 任务 completed / 当前触发步骤已完成 / 实例已完成（monitorSaved）→
  //   弹窗确认后 rebuild（清理后续+重跑 Monitor）；
  // ③ 其他 paused（flow 介入中等）→ 恢复执行；否则 pending 排队
  // 2026-08-21 修复：传当前页面 monitor id（instId）——此前不传由后端自动定位
  // stopped 的 monitor 步骤，会定位到 monitor-init 残留导致消息串台
  // （DB 实证 e726f3e6 13:36：用户在 monitor-step-X 页发送，消息被 monitor-init 消费）
  // 2026-08-21：monitor-init/review/report 无 rebuild 语义（无前驱真实步骤可清理）
  // ——完成态发送 = 排队等流程处理，不弹 rebuild（后端 rebuild 对它们已 400）
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
            // 2026-08-21 去绑定普通化（用户决策）：发送 = 注入当前实例续跑——
            // step 级 send 到本页 monitor 实例（消息写入该实例 intervention，
            // 实例重置 pending 续跑，原对话上下文保留）；不再走流程级
            // flowIntervene('pending')（flow_pending 被任意下一个 monitor 步骤
            // 消费是消息串台/无上下文的根因，DB 实证 e726f3e6）
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
      // 传当前触发步骤（实体 id：真实步骤或 monitor 步骤——后端锚点解析
      // 统一处理；2026-08-21 统一传 instId，此前传路由 stepId 可能与
      // 重定向前旧 id 不一致）
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
      // 2026-08-21：传当前页面 monitor id——此前不传由后端自动定位 active
      // 的 monitor-* 审查步骤，可能停错实例；后端无 step_id 已改为直接报错
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
      // 同 stop：传当前页面 monitor id（顶栏恢复按钮——恢复当前页面的实例）
      await api.monitorControl(taskId, 'resume', '', instId)
    } catch (err) {
      setToast(err instanceof Error ? err.message : '恢复失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }, [taskId, instId])

  // 2026-08-22：llmError 卡「🔄 重试」（与 StepDetail §3.7.5 同款——resume
  // 仅恢复状态，须显式 start 重启执行循环；Monitor 实例为实体步骤同样适用）
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

  // 压缩 Monitor 实时对话（2026-08-20：介入栏压缩按钮——压缩当前实例
  // 的最近一轮消息；后端 compress_step 不校验步骤归属，审查步骤可压缩）
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
        {/* §3.8 顶栏返回按钮 + Monitor 标题 + 恢复执行（右上角，仅任务 paused
          显示——与 StepDetail 顶栏恢复按钮同位置同款；暂停由介入栏「终止」承担） */}
        <div className="top-bar top-bar-monitor">
          <a className="back-btn" onClick={() => navigate(`/task/${taskId}`)}>
            <Icon name="arrowLeft" size={13} gap={2}/>
            返回
          </a>
          <div className="step-header">
            <span className="monitor-title">Monitor 思考过程 — {title}</span>
            <span className="monitor-badge">Monitor Agent</span>
          </div>
          {/* 恢复执行：实例 stopped 无条件显示（2026-08-23：任务 active + llmError
            stopped 时此前无任何恢复入口——顶栏恢复要求任务 paused，错误卡 retryable
            =false 无重试按钮 → 流程停摆无自救；与 StepDetail canResume 对齐，stopped
            即显示；pending 保留任务暂停限定——排队中不误显） */}
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
          {/* 消息流（同 StepDetail 消息组件）+ M9 决策卡 */}
          <div className="chat-log">
            {/* 顺序 = 后端组装：系统提示（DB system 折叠）→ 注入消息（step_context
              气泡）→ 对话消息 */}
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
            {/* SSE 实时工具卡 */}
            {liveCards.map(([callId, t]) => (
                <div className="ai-tool-inline" key={callId}>
                  <ToolCard callId={callId} toolName={t.toolName} input={t.input} output={t.output} status={t.status}/>
                </div>
            ))}
            {/* 2026-08-22：llmError 重试卡（编排失败时等待页已移除启动按钮，
              重试由本卡承担——与 StepDetail §3.7.5 同款） */}
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
                    {/* 2026-08-23：retryable=false（400/unknown）时无重试按钮但可恢复——
                      直接恢复执行循环（monitorControl resume + start_task），与顶栏
                      恢复按钮同语义（此前 stopped 后无任何入口，流程停摆） */}
                    {!llmError.retryable && (
                        <button className="retry-btn" onClick={handleResume} disabled={busy}>
                          <Icon name="play" size={13} gap={5}/>
                          恢复
                        </button>
                    )}
                  </div>
                </div>
            )}
            {/* 当前流式思考块 */}
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
            {/* 流式 assistant 块（Monitor 编排虚拟 stepId 内容） */}
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
          {/* 「回到底部」悬浮按钮：仅滚离底部较远时显示（sticky 贴右下角；自由滚动时回底用） */}
          {autoScroll.showJump && (
              <button className="scroll-jump-btn" onClick={autoScroll.jumpToBottom} title="回到最新输出">
                <Icon name="chevronDown" size={16} gap={0}/>
              </button>
          )}
        </div>
        {/* 2026-08-21：底部区与 StepDetail 同布局（§3.7.6）——上下文占用条在
          介入栏上方、token 明细在下方；介入栏（mode="flow" 与 FlowOverview 同款：
          发送=排队/强制=immediate/终止=暂停 Monitor/恢复=重触发 Monitor） */}
        <div className="step-footer">
          <ContextMeter windowSize={contextWindow} prompt={stepTokens?.prompt ?? 0}/>
          <InterveneBar
              mode="flow"
              onSend={handleSend}
              onForce={handleForce}
              /* 2026-08-20：stop 仅当前实例运行中显示（instStatus active 或流式
                 进行中）——monitor 对话已完成（任务 active 只是其他步骤在跑）不显示
                 方形终止按钮（用户反馈） */
              onStop={instStatus === 'active' || streaming ? handleStop : undefined}
              onCompress={handleCompress}
              /* 2026-08-20 多实例：running=当前实例在跑（monitor_steps active 或
                流式进行中）——monitor 已完成、其他步骤在跑时不显示运行态 */
              running={instStatus === 'active' || streaming}
              busy={busy}
              autoScroll={autoScrollOn}
              onToggleAutoScroll={() => setAutoScrollOn((v) => !v)}
          />
          {/* Token 展示：token 明细（ContextMeter 在介入栏上方——与 StepDetail
            同布局；数据源 = 端点 29 step_tokens 实体行直查）
            2026-08-23：总花费——monitor 实体步骤固定 model_tier=power（系统创建），用 power 组价格
            2026-08-24（修复缓存命中双重计价）：token_prompt 含缓存命中——未缓存
            输入（prompt-cached）按 in 单价，缓存命中按 cached 单价，不再重复计价 */}
          <TokenDetails
              prompt={stepTokens?.prompt ?? 0}
              cached={stepTokens?.cached ?? 0}
              completion={stepTokens?.completion ?? 0}
              cost={(stepTokens ? (Math.max(0, stepTokens.prompt - stepTokens.cached) * powerPrices.in
                  + stepTokens.cached * powerPrices.cached
                  + stepTokens.completion * powerPrices.out) / 1e6 : 0)}
              trailing={
                // 2026-08-21：运行统计——实时 metrics（SSE 采样/结算 + DB 校准
                // max 合并）优先，刷新后（metrics 重置 startedAt=null）回退 DB
                // 实体行 step_stats（orchestrator 每轮 LLM 流结束落库，1s 轮询）；
                // 无请求记录时全 -- 占位（与 StepDetail 未运行态一致）
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
        {/* rebuild 确认弹窗：monitor 已完成时发送 → 清理后续步骤消息/产出 + 重跑当前 monitor */}
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
