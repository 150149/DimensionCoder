// ═══════════════════════════════════════════════════════════════
// StepDetail（SWP4-C / WP4-3 §3.7）
// - Props：{taskId, stepId}
// - api.getStep 拉初始对话；useTaskPolling(1000) 拉 getTask 作 meta（进度条）
// - useStepStream 处理 SSE：streamChunk 追加到当前 assistant 块尾部（不覆盖）；
//   __DC_FULL__ → 重新 getStep 全量渲染（保留已展开工具卡状态：callId 集合）；
//   __DC_RETRY__N/10__DC_RETRY__ → 重试进度条
// - 5 类消息经 ChatMessage 渲染（system 可折叠 / user 气泡 / assistant
//   markdown / tool 卡 / thinking 灰色斜体）
// - 工具卡 callId 防覆盖：liveCallIds 集合记录已渲染卡（_liveContentRendered
//   等价物），轮询/全量重拉跳过 DB 中相同 callId 的 tool 消息
// - 错误卡：llmError → .llm-error-card（code/message/重试次数/可重试标记 +
//   「🔄 重试」→ api.resumeStep）；同时弹全局 ErrorToast（§3.6.12 J4：
//   并行另一条流错误无感知）
// - 底部 InterveneBar mode="step"：💬发送（send）/ ⛔强制插入（force_inject）/
//   ⏹打断（stop，仅流式进行中可用）/ 📦压缩（api.compressStep，成功后刷新）
// ═══════════════════════════════════════════════════════════════

import {Fragment, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {LlmErrorEvent, StepData, StepLive, StreamChunkEvent, TaskSummary, ThinkingChunkEvent, ToolCallParamEvent, ToolCallResultEvent, ToolCallStartEvent,} from '../api/types'
import ChatMessage from '../components/ChatMessage'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ConfirmDialog, {type ConfirmRow} from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ProgressRail from '../components/ProgressRail'
import ToolCard from '../components/ToolCard'
import FileTree from '../editor/FileTree'
import {ContextMeter, TokenDetails, TokenMetrics} from '../components/TokenUsageBar' // Token 展示
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

/** V-19：流式轮次块（每轮：思考 → 文本 → 工具卡；toolExecuting 时定稿归档） */
interface StreamRound {
  id: number
  thinking: string
  text: string
  toolCallIds: string[]
}

/** 消息流内决策请求包：不渲染卡片——JSON 块剥离后 ChatMessage 完整 markdown 展示
 * （2026-08-20 用户要求：像 final report 一样聊天框展示，卡片把信息挤成碎片）。
 * JSON 块保留在消息中仅用于 G3d 操作区解析选项按钮。 */

export default function StepDetail({taskId, stepId}: StepDetailProps) {
  const navigate = useNavigate()
  const [data, setData] = useState<StepData | null>(null)
  const [task, setTask] = useState<TaskSummary | null>(null)
  const [streaming, setStreaming] = useState(false)
  // 2026-08-22：运行统计提取为 useStepMetrics（StepDetail/MonitorDetail 共用）
  // ——SSE 事件驱动实时采样/结算 + 轮询后端 stats 校准（max 合并）；
  // metricRequestRef 记录当前 LLM 流时序点（stepStart 建、streamEnd 结算）
  const {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, settleStep, mergeBackend
  } = useStepMetrics()
  // V-19：流式轮次块模型——每轮（思考 → 文本 → 工具卡）独立成块，
  // toolExecuting 时定稿归档、下一轮开新块；替代旧的全局 streamText/thinkingText
  // 累积（旧模型多轮内容全拼在一起：思考全堆思考框、文本全堆最下面）
  const [streamRounds, setStreamRounds] = useState<StreamRound[]>([])
  const roundIdRef = useRef(0)
  const [llmError, setLlmError] = useState<LlmErrorEvent | null>(null)
  const [retryProgress, setRetryProgress] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  // 待发送气泡：发送后立即可见（发送中…），userMessage 事件（已注入落库）后清除
  const [pendingSends, setPendingSends] = useState<{ id: number; content: string }[]>([])
  const pendingIdRef = useRef(0)
  // 已渲染 live 工具卡 callId 集合（`_liveContentRendered` 等价物）：轮询/
  // 全量重拉时据此跳过 DB 中相同 callId 的 tool 消息（不覆盖已渲染卡）
  const [liveCallIds, setLiveCallIds] = useState<Set<string>>(() => new Set())
  // 已展开工具卡 callId 集合（__DC_FULL__ 全量重拉时保留展开状态）
  const [expandedTools, setExpandedTools] = useState<Set<string>>(() => new Set())
  const liveTools = useLiveTools()

  // V-19：流式轮次结构（toolCallIds 引用 liveTools.tools 的最新卡状态）
  const newRound = useCallback((): StreamRound => ({id: ++roundIdRef.current, thinking: '', text: '', toolCallIds: []}), [])
  const appendRound = useCallback((fn: (rounds: StreamRound[]) => StreamRound[]) => {
    setStreamRounds((prev) => fn(prev.length ? prev : [newRound()]))
  }, [newRound])

  // 2026-08-27（用户反馈：从总览进详情页「AI 正在思考」/「正在执行的命令」不显示，
  // 刷新才恢复）：getStep 附带 live 快照 → 首屏直接渲染进行中状态。
  // - streaming → 开启流式标记（无内容时触发「AI 正在思考...」占位）
  // - thinking/text → 首轮块预置（SSE 补发已由后端按 seq 过滤，无重复）
  // - tool → liveTools 预置执行中工具卡（markRendered 防 DB 重拉覆盖）
  // 注意：liveTools 对象每次渲染重建（tools state 变化）→ 经 ref 取方法，
  // 保证 initLive 稳定（否则 loadStep 依赖漂移 → getStep 无限重拉）
  const liveToolsRef = useRef(liveTools)
  liveToolsRef.current = liveTools
  const initLive = useCallback((l: StepLive | null | undefined) => {
    if (!l) return
    if (l.streaming) setStreaming(true)
    const rounds: StreamRound[] = []
    if (l.thinking || l.text || l.tool || l.streaming || l.completedTools?.length) {
      rounds.push({...newRound(), thinking: l.thinking ?? '', text: l.text ?? ''})
    }
    // 2026-08-27（用户反馈：刷新丢已完成工具卡）：整轮未落库期间已完成的工具
    // 先渲染（done 卡），随后执行中工具（running 卡）——保持执行顺序；两者都
    // 加入 liveCallIds（visibleMessages 过滤 DB 同 callId 消息防重复）
    for (const ct of l.completedTools ?? []) {
      const id = ct.callId
      setLiveCallIds((prev) => {
        const next = new Set(prev)
        next.add(id)
        return next
      })
      liveToolsRef.current.start(id, ct.name, ct.input)
      liveToolsRef.current.result(id, ct.output)
      liveToolsRef.current.markRendered(id)
      const last = rounds[rounds.length - 1]
      if (last) last.toolCallIds.push(id)
      else rounds.push({...newRound(), toolCallIds: [id]})
    }
    if (l.tool) {
      // 与 handleToolStart 对齐：加入 liveCallIds（visibleMessages 据此过滤 DB 中
      // 同 callId 工具消息，防止命令完成后落库消息与 live 卡重复渲染）
      const liveCallId = l.tool.callId
      setLiveCallIds((prev) => {
        const next = new Set(prev)
        next.add(liveCallId)
        return next
      })
      liveToolsRef.current.start(liveCallId, l.tool.name, l.tool.input)
      liveToolsRef.current.markRendered(liveCallId)
      const last = rounds[rounds.length - 1]
      if (last) last.toolCallIds.push(liveCallId)
      else rounds.push({...newRound(), toolCallIds: [liveCallId]})
    }
    if (rounds.length) setStreamRounds(rounds)
  }, [newRound])

  // §3.9：顶栏「对话/代码」tab（state 切换，非路由）：代码视图 = FileTree +
  // CodeEditor（与总览页同款）；dirty 标记 tab 标题 ●
  const CodeEditor = lazy(() => import('../editor/CodeEditor'))
  const [view, setView] = useState<'chat' | 'code'>('chat')
  const [openedPath, setOpenedPath] = useState<string | null>(null)
  const [editorDirty, setEditorDirty] = useState(false)
  const [externalModified, setExternalModified] = useState<string[]>([])
  // J2b 外部修改角标路径增删（CodeEditor 轮询上报）
  const handleExternalModified = useCallback((path: string, modified: boolean) => {
    setExternalModified((prev) =>
        modified ? Array.from(new Set([...prev, path])) : prev.filter((p) => p !== path),
    )
  }, [])

  // G3：人工审批（gate 待审批步骤）——AI 整理决策信息，人类在此拍板；
  // 判定条件与总览页 isGate 一致：步骤 active + human_attention=gate + 任务 paused
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [approveBusy, setApproveBusy] = useState(false)
  // G3b：决策请求包结构化展示——选项可点选，选中后按所选选项继续（发消息给 AI）；
  // 另硬编码「自定义方向」输入框（用户可直接输入自己的决策，不限于 AI 给出的选项）
  const [selectedOpt, setSelectedOpt] = useState<number | null>(null)
  const [customText, setCustomText] = useState('')
  const [choiceOpen, setChoiceOpen] = useState(false)
  const [choiceNote, setChoiceNote] = useState('')
  // AI 整理的决策信息 = 对话中最后一条 assistant 文本（gate 步骤完成后保留在消息流）
  const latestAssistant = useMemo(() => {
    const msgs = data?.conversation ?? []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'assistant' && (m.content || '').trim()) return String(m.content)
    }
    return ''
  }, [data])
  // 解析后的决策请求包（无 JSON 块时为 null → 退化为纯文本展示）
  const pkg = useMemo(() => parseDecisionPackage(latestAssistant), [latestAssistant])
  // 对话中是否有 AI 输出的决策请求包（有 → 选项类交互；无 → 审批类通过/拒绝）
  const hasDecisionPkg = useMemo(
      () => (data?.conversation ?? []).some((m) => m.role === 'assistant' && parseDecisionPackage(m.content ?? '')),
      [data],
  )
  // 最后一条决策包消息的 seq（操作区只跟随最后一条渲染）
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
        // 自定义方向：输入框内容即决策内容，直接发送（无需补充弹窗）
        msg = `【决策选择】用户自定义决策：${custom}`
      } else if (selectedOpt != null && pkg?.options?.[selectedOpt]) {
        const opt = pkg.options[selectedOpt]
        const label = String.fromCharCode(65 + selectedOpt)
        const note = choiceNote.trim()
        msg = `【决策选择】用户选择选项 ${label}：${opt.option}${note ? `\n补充说明：${note}` : ''}`
      } else {
        return
      }
      // 2026-08-20 修复：选项选择 = 审批通过（此前走 stepIntervene send 排队
      // 注入——只重跑 AI 不审批，gate 步骤永远 active 卡死，DB 实证 e726f3e6
      // step-3 用户选 C 后无推进）。决策文本作为 reason 提交，后端追加进
      // summary 供后续步骤可见；不再触发 AI 重跑
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
    if (!reason) return // 拒绝原因必填
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

  // Token 展示：上下文窗口总容量（config contextWindow，设置页可编辑；
  // 默认 1M = DeepSeek V4 真实窗口上限，400K 只是压缩触发线不是窗口上限）；拉取失败保持默认
  const [contextWindow, setContextWindow] = useState(1_048_576)
  // 2026-08-23：light/power 六项价格（每 1M token 单价，缺省 0）——按步骤 model_tier 选组算花费
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

  // 聊天流自动滚动开关（InterveneBar 控制）：仅「步骤运行中」时生效——
  // 非运行步骤（已完成/暂停）enabled=false，自由浏览历史不被轮询刷新拉回
  // §3.7.1：getStep 拉初始对话（__DC_FULL__ / refreshData / 压缩成功后复用）
  // 分页：默认最近 200 条（历史折叠——全量曾达 21MB 拖慢页面打开；流式期间
  // 消息经 SSE 增量追加，全量重拉后历史折叠回窗口，可再点「加载更早消息」展开）
  // 失败自动重试（最多 5 次，指数退避）：getStep 是 step-meta（状态徽章/暂停
  // 恢复按钮）的唯一数据源，瞬时失败（LLM 写库高峰/网络抖动）若不重试，
  // meta 区永久消失——页面只剩底部介入栏，按钮不回来。重试耗尽后弹错提示
  const loadStep = useCallback(async () => {
    let lastErr: unknown
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const d = await api.getStep(taskId, stepId, {limit: 200})
        setData(d)
        // 2026-08-27：live 快照 → 首屏渲染进行中状态（thinking/执行中工具/占位）
        initLive(d.live)
        // 2026-08-27 兜底：步骤 active 但快照缺失（服务重启后快照重建窗口、
        // _clear_task_live 排空期等）→ 显示「AI 正在思考...」占位，不依赖后端
        // 快照时序；快照/SSE 事件到达后由 initLive/实时事件接管
        if (!d.live && d.step?.status === 'active') {
          setStreaming(true)
          setStreamRounds([{...newRound(), thinking: '', text: ''}])
        }
        return
      } catch (err) {
        lastErr = err
        if (attempt < 4) {
          await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt))
        }
      }
    }
    setToast(lastErr instanceof Error ? lastErr.message : '消息加载失败，请刷新重试')
  }, [taskId, stepId, initLive])
  // 往前翻页：加载比当前窗口更早的 200 条并插到列表头部（seq 升序，prepend 不冲突）
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

  // §3.7.1：useTaskPolling(1000) 拉 getTask 作 meta（进度条 / 步骤状态）
  const loadMeta = useCallback(() => api.getTask(taskId).then((d) => {
    setTask(d.task)
    // 2026-08-22：后端校准——DB stats（tiktoken 精确、每轮落库）逐字段 max
    // 合并到前端实时 metrics（SSE 事件丢失/断连时后端补齐，前端实时不降级）
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

  // §3.7.3：SSE chunk 文本 append 到当前轮次文本块尾部（不覆盖）
  const handleChunk = useCallback((e: StreamChunkEvent) => {
    sampleFirstChunk()
    accrueRoundComp(e.chunk.length) // 2026-08-22：文本吐字计入进行中轮估算
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
    // 每轮 stepStart 重置时序点（重跑=新执行周期）；requests 由 streamEnd 计数
    resetStep(at)
    setLlmError(null)
    // V-19：新执行周期 → 重置轮次（round 0 由首个事件惰性创建）
    setStreamRounds([])
  }, [resetStep])

  // 2026-08-24：切换步骤清空限流重试进度条（useStepStream 重建后无清除事件，
  // 旧值残留到新步骤——用户反馈：恢复后进度条不消失，刷新页面才消失）
  useEffect(() => {
    setRetryProgress(null)
  }, [stepId])

  // streamEnd：结束流 + 清 live 卡（旧 endStream 清 _toolCards 等价物）+ 全量重拉
  // （对话已落库，恢复 DB 版本渲染）——统计结算（endRound）在重拉前：
  // outputDurationMs/requests/completion 定格为最终值（2026-08-22
  // completion 含本轮估算，从 useStepMetrics 提取）
  // 2026-08-23：loadStep 成功（DB 版本已渲染）后才清流式轮次块——此前先清
  // 后拉，loadStep 未生效时文本块永久消失（用户反馈：刷新才出现）；失败保留
  // 轮次块（md 定稿渲染兑底，不再空白）
  const handleStreamEnd = useCallback(() => {
    setStreaming(false)
    const at = Date.now()
    endRound(at)
    clearLive()
    void loadStep().then(() => setStreamRounds([]))
  }, [clearLive, loadStep, endRound])

  // §3.7.3：__DC_FULL__ → 重新 getStep 全量渲染；expandedTools（callId 集合）
  // 不清空 → 已展开工具卡状态保留。2026-08-23：同 streamEnd——loadStep 成功
  // 后才清流式块（此前先清后拉，失败即空白）
  const handleFullRerender = useCallback(() => {
    setStreaming(false)
    clearLive()
    void loadStep().then(() => setStreamRounds([]))
  }, [clearLive, loadStep])

  // 介入消息已注入落库：全量重拉（DB 版本渲染 user 消息），2026-08-23：重拉
  // 成功（消息已渲染）后才清本地待发送气泡——此前先清气泡再异步重拉，loadStep
  // 未生效时气泡与消息双双消失（用户反馈：发送后"完全无反应"）；失败保留
  // "发送中…"气泡（用户可见已发出，可刷新恢复）
  const handleUserMessage = useCallback(() => {
    void loadStep().then(() => setPendingSends([]))
  }, [loadStep])

  // 思考过程流式追加（thinkingChunk，仅展示）：上一轮已定稿（有文本）→
  // 开新轮次块，避免多轮思考全堆在一个框（V-19）
  // 2026-08-22：只保留当前请求——工具执行已清空旧轮次，思考只进当前轮次；
  // 不因 toolCallIds 存在而拆块（否则工具卡与思考分属轮次，toolExecuting
  // 继承会丢卡）
  const handleThinkingChunk = useCallback(
      (e: ThinkingChunkEvent) => {
        sampleFirstChunk()
        accrueRoundComp(e.chunk.length) // 2026-08-22：思考吐字计入进行中轮估算
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
    // refreshData（断连溢出兜底）：全量重拉；live 卡由 visibleMessages 按
    // callId 过滤防覆盖，流式轮次块保留（与旧 streamText 行为一致）
    loadStep()
  }, [loadStep])

  // §3.7.4：toolCallStart 插入 spinner 卡（归属当前轮次块）；toolCallResult 更新输出
  const handleToolStart = useCallback(
      (e: ToolCallStartEvent) => {
        // 2026-08-27（暂停/停止后快照清空 + 缓冲补发重放历史 toolCallStart）：
        // DB 已落库该 callId 的工具消息 → 不 addLiveCall、不渲染 live 卡（DB 卡
        // 保留原位——否则 visibleMessages 过滤掉已渲染卡 = 工具卡消失根因）
        const persisted = data?.conversation?.some((m) => {
          if (m.role !== 'tool') return false
          const callId = m.tool_call_id ?? (m.seq != null ? `seq-${m.seq}` : '')
          return callId === e.callId
        })
        if (persisted) return
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
      [addLiveCall, liveTools, appendRound, data],
  )

  // V-18：工具参数流式增量（LLM 生成参数时逐片到达，前端逐字累积动画）
  const handleToolParam = useCallback(
      (e: ToolCallParamEvent) => {
        liveTools.appendParam(e.callId, e.delta)
        // 2026-08-22：工具参数吐字计入进行中轮估算（输出速度分子实时）
        accrueRoundComp(e.delta.length)
      },
      [liveTools, accrueRoundComp],
  )

  // V-18：工具开始执行 → 工具卡自带 spinner（无需额外状态条）；当前轮次
  // 定稿归档（文本/思考/工具卡已渲染），下一轮开新块
  // 2026-08-22：只保留当前大模型请求——旧轮次思考/文本清空（不累积历史轮次），
  // 工具卡继承到新轮次继续显示（执行中/刚完成的卡不闪断）
  const handleToolExecuting = useCallback(() => {
    setStreaming(false)
    setStreamRounds((prev) => {
      if (!prev.length) return prev
      const last = prev[prev.length - 1]
      // 轮次非空才处理（防空轮拆分）；工具卡保持引用 liveTools 最新状态
      if (!last.thinking && !last.text && !last.toolCallIds.length) return prev
      // 2026-08-23（用户反馈：AI 调工具时文本"快速消失"）：旧轮次思考清空
      // （过程性不保留，防堆叠）、文本保留定稿（isLast=false → ReactMarkdown
      // md 渲染，DB 已落库同款展示）、工具卡原地保留，追加新空轮次——后续
      // 思考/文本进入新轮次，显示在旧内容之后（时间序）
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
        // V-18：工具执行完 → 立即恢复 streaming（显示「AI 正在思考…」等待下一轮首字）
        setStreaming(true)
        // 2026-08-21：工具结果到达 ≈ 下一轮 LLM 请求即将发出——记录 API 开始时间，
        // 使后续轮的输出速度/首字延迟统计含请求发出时刻（否则 reqStartedAt=null
        // 退化为首字起点，卡顿后吐字仍虚高）
        const req = metricRequestRef.current
        if (req) req.reqStartedAt = Date.now()
      },
      [liveTools],
  )

  // §3.7.5 错误卡（llmError → .llm-error-card）+ §3.6.12 J4：除渲染错误卡外
  // 同时弹全局 ErrorToast（避免并行另一条流错误无感知）
  // 2026-08-23：setStreamRounds([]) 移到 loadStep 成功后——此前先清后拉，
  // loadStep 未生效时错误前的文本块永久消失（同 streamEnd 缺陷模式）
  const handleLlmError = useCallback(
      (e: LlmErrorEvent) => {
        if (e.stepId === stepId) setLlmError(e)
        setToast(e.message || 'LLM 错误')
        // 后端 llmError/aborted 路径不发 streamEnd（_execute_step 捕获后直接返回）
        // → 流式轮次块（纯文本）残留显示（markdown 原样）→ 全量重拉切回 ChatMessage
        setStreaming(false)
        // llmError/aborted 路径后端不发 streamEnd → 同样定格（悬空计时修复：
        // 非 active 步骤不再因缺少 endedAt 而显示无限增长的时间）
        settleStep(Date.now())
        clearLive()
        void loadStep().then(() => setStreamRounds([]))
      },
      [stepId, clearLive, loadStep, settleStep],
  )

  // §3.7.5：「🔄 重试」→ api.resumeStep（resume 仅恢复状态，须显式 start 重启执行循环）
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

  // §3.7.6：介入栏提交（send / force_inject / stop；提交期间 busy 防重入）
  // 已完成步骤 send → 先弹确认（重置后续流程会清除后续消息记录），确认后执行
  const [confirmReset, setConfirmReset] = useState(false)
  const [pendingMsg, setPendingMsg] = useState('')
  const doIntervene = useCallback(
      async (mode: 'send' | 'force_inject' | 'stop', content: string) => {
        setBusy(true)
        // 发送/强制插入：本地立即插入待发送气泡（发送中…），userMessage 事件后转正
        const isMsg = mode !== 'stop'
        const id = ++pendingIdRef.current
        if (isMsg) setPendingSends((prev) => [...prev, {id, content}])
        try {
          await api.stepIntervene(taskId, stepId, mode, content)
          if (mode === 'stop') {
            // stop 后端不发 streamEnd → 流式纯文本残留 → 重拉清残留（切回 ChatMessage
            // 渲染）。2026-08-23：loadStep 成功后才清（同 streamEnd 缺陷模式）
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
        // 已完成步骤续做：先弹窗确认（会清除后续步骤消息记录并重置为未开始）
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
  // 弹窗确认：重置并发送（后端已完成步骤续做编排：清后续 + 回进行中 + 自动继续）
  const handleConfirmReset = useCallback(() => {
    setConfirmReset(false)
    void doIntervene('send', pendingMsg)
  }, [doIntervene, pendingMsg])
  // 弹窗列表：后续真实步骤（sort_order 更大；虚拟 monitor 步骤不展示但实际一并重置）
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

  // §3.7.6：📦压缩 → api.compressStep，成功后刷新
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
  // 2026-08-23（用户反馈）：任务暂停 → 顶栏轨道下一个待执行步骤深灰标记（暂停中断点）
  const railPausedPendingId = task?.status === 'paused'
      ? [...steps].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
          .find((s) => s.status === 'pending')?.step_id
      : undefined

  // 聊天流自动滚动：仅「步骤运行中」（流式输出或步骤 active）时跟随底部；
  // 非运行步骤（已完成/暂停）自动滚动不生效——用户自由浏览历史，不会被轮询刷新拉回
  const contentRef = useRef<HTMLDivElement>(null)
  const [autoScrollOn, setAutoScrollOn] = useState(true)
  const stepRunning = streaming || steps.find((s) => s.step_id === stepId)?.status === 'active'
  const autoScroll = useChatAutoScroll(
      contentRef,
      [
        streamRounds, // V-19：轮次块内容变化（思考/文本/工具卡）时跟随
        pendingSends,
        liveCallIds.size,
        liveTools.tools, // 工具结果到达/卡展开增高时也跟随（否则距底>阈值被误判滚离 → 自动滚动失效）
        data,
        llmError,
        retryProgress,
        streaming,
      ],
      autoScrollOn && stepRunning,
  )
  // 进入页面初始定位到底部（看最新消息）；此后非运行步骤滚动完全自由
  const initialScrolledRef = useRef(false)
  useEffect(() => {
    if (data && !initialScrolledRef.current) {
      initialScrolledRef.current = true
      const el = contentRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [data])
  // Token 展示上条（上下文占用）：当前上下文长度（context_tokens = 最近一次请求的
  // 输入 tokens，覆盖写非累加；task 1s 轮询为准，轮询未返回时回退 getStep 的 step 字段）
  const curStep = steps.find((s) => s.step_id === stepId) ?? step
  const contextTokens = curStep?.context_tokens ?? 0
  // Token 展示下条（明细）：累计消耗（总输入 / 缓存 / 总输出）
  const tokenPrompt = curStep?.token_prompt ?? 0
  const tokenCached = curStep?.token_cached ?? 0
  // 2026-08-22：输出 token 展示——实时 metrics（DB 校准 max）优先，刷新后
  // （metrics 重置 startedAt=null）回退 DB 值
  const tokenCompletion = metrics.startedAt != null ? metrics.completion : (curStep?.token_completion ?? 0)
  // 2026-08-23：总花费——按步骤 model_tier 选价格组（缺省 power），无单位
  // （设置与展示口径一致）；价格全 0 时金额为 0
  // 2026-08-24（修复缓存命中双重计价）：token_prompt 含缓存命中部分——未缓存
  // 输入（prompt-cached）按 in 单价，缓存命中按 cached 单价，不再重复计价
  const prices = (curStep?.model_tier ?? 'power') === 'light' ? lightPrices : powerPrices
  const totalCost = (Math.max(0, tokenPrompt - tokenCached) * prices.in
      + tokenCached * prices.cached + tokenCompletion * prices.out) / 1e6

  // G3：gate 待审批判定（curStep 定义后计算，TDZ 安全）
  const isGatePending =
      curStep?.status === 'active' && curStep?.human_attention === 'gate' && task?.status === 'paused'

  // §3.7.6：恢复执行（顶栏按钮）——stopped（resumeStep+重启循环）或
  // pending+任务 paused（流程暂停后步骤被重置 pending：直接重启执行循环
  // 拾取重跑，2026-08-20 pause_task 打断语义）显示；暂停由输入框「终止
  // 当前输出」承担（基于步骤 active 状态始终可用），两者不再重复
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
      // 流程暂停打断：步骤已 pending，恢复 = 重启执行循环
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

  // 尽力模式开关（2026-08-16 用户需求）：与总览页同款——gate 审批自动放行 +
  // 防放弃提醒 + 收尾复核；乐观更新 + loadMeta 刷新
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

  // 轮询响应不覆盖已渲染卡：跳过 DB 中已 live 渲染（liveCallIds）的 tool 消息
  const visibleMessages = useMemo(() => {
    if (!data) return []
    return data.conversation.filter((m) => {
      if (m.role !== 'tool') return true
      const callId = m.tool_call_id ?? (m.seq != null ? `seq-${m.seq}` : '')
      return !liveCallIds.has(callId)
    })
  }, [data, liveCallIds])

  // 顺序（用户要求）：系统提示（纯规则折叠）置顶，动态上下文 user 气泡紧随其后
  const systemMsg = visibleMessages.find((m) => m.role === 'system')
  const restMsgs = visibleMessages.filter((m) => m.role !== 'system')

  // V-19：工具卡展开规则——执行中（running）全部展开 + 最近完成的一张（done）
  // 保持展开（「正在执行的 + 前一张」），更早的自动折叠；用户手动展开不受影响
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
        {/* §3.7.2 顶栏：返回 /task/:taskId + ProgressRail（rail 版：进度条占满剩余空间并贴左）
          暂停/恢复 toggle 固定在顶栏右侧（不依赖 getStep——刷新后即使消息数据
          未返回/被阻塞，按钮也随 task 1s 轮询立即可用；状态徽章行不承载操作） */}
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
          {/* 恢复按钮：stopped 或（pending+任务暂停）显示（暂停由输入框「终止当前
            输出」承担，避免重复；与总览页 flow-actions 同位置，对话/代码 tab 在后） */}
          {canResume && (
              <button className="run-toggle-btn" onClick={handleResumeStep} disabled={busy} title="恢复当前步骤">
                <Icon name="play" size={13} gap={4}/>
                恢复
              </button>
          )}
          {/* 尽力模式开关（2026-08-16 用户需求）：gate 自动放行 + 防放弃提醒 */}
          <button
              className={`best-effort-toggle${task?.best_effort ? ' active' : ''}`}
              onClick={handleToggleBestEffort}
              title="尽力模式：用户不在线时 gate 自动放行、AI 丧气时自动提醒继续"
          >
            <span className="toggle-dot"/>
            尽力模式
          </button>
          {/* 对话/代码 segmented 按钮组（无空隙标准控件；dirty 标记 ● 同总览页） */}
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
        {/* 代码视图：FileTree + CodeEditor（与总览页同款，占满剩余空间；对话视图隐藏介入栏） */}
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
                {/* §3.7.2 标题区：step title + 徽章（tier/必做/状态） */}
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
                {/* G3d：审批操作区跟随最后一条决策包——看完就地拍板（选项按钮 + 自定义方向输入框） */}
                <div className="chat-log">
                  {/* 分页：还有更早消息时显示「加载更早」（历史折叠） */}
                  {data?.truncated && (
                      <div className="history-fold">
                        <button className="history-load-more" onClick={loadEarlier} disabled={loadingMore}>
                          {loadingMore ? '加载中...' : `加载更早消息（共 ${data.total ?? '?'} 条，已显示 ${data.messages.length} 条）`}
                        </button>
                      </div>
                  )}
                  {/* 顺序（用户要求）：系统提示（纯规则折叠）在最顶 → 动态上下文 user 气泡 → 其余消息 */}
                  {systemMsg && <ChatMessage msg={systemMsg}/>}
                  {data?.prep?.step_context && (
                      // 2026-08-22：系统注入上下文按 Markdown 渲染（标题/列表/代码块），
                      // 与 AI 消息一致——此前纯文本一坨无法阅读
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
                            {/* 决策请求包：剥离 JSON 块后完整 markdown 展示（2026-08-20：不再渲染卡片） */}
                            <ChatMessage
                                msg={{...m, content: dp.before + dp.after}}
                                onToggleTool={toggleTool}
                                expandedTools={expandedTools}
                            />
                            {/* G3d：审批操作区跟随最后一条决策包——看完就地拍板 */}
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
                                  {/* 硬编码最终选项：自定义方向输入框（不限于 AI 给出的选项） */}
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
                                          // 自定义方向：输入框内容即完整决策，直接发送；选项则先弹窗补充说明
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
                  {/* 待发送气泡：发送后立即可见（市面 AI 编程软件同款交互），注入落库后移除 */}
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
                  {/* V-19：流式轮次块（思考 → 文本 → 工具卡，每轮独立；toolExecuting 归档） */}
                  {streamRounds.map((r, ri) => {
                    const isLast = ri === streamRounds.length - 1
                    return (
                        <Fragment key={r.id}>
                          {/* 思考过程流（thinkingChunk，仅展示）：置于文本/工具卡之前 */}
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
                          {/* 流式文本块：当前正在流式的轮（last + streaming）→ 纯文本逐字展示
                    （MD 解析需完整文本，输出不完整时渲染会错位）；已定稿的轮（归档或
                    流已结束）→ ReactMarkdown 渲染（用户要求：文本块结束输出即渲染） */}
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
                          {/* 工具卡（引用 liveTools 最新状态：参数流式累积 / 结果更新）；
                    defaultExpanded=执行中 + 最近完成的一张（其余折叠） */}
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
                  {/* __DC_RETRY__N/10：重试进度条 */}
                  {retryProgress != null && (
                      <div className="retry-progress">
                        <Icon name="refresh" size={13} gap={8}/>
                        请求被限流，正在重试 ({retryProgress}/10)...
                      </div>
                  )}
                  {/* §3.7.5：错误卡（code/message/重试次数/可重试标记 + 重试） */}
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
                  {/* 审批类 gate（AI 未输出决策请求包）：消息流末尾渲染通过/拒绝（无选项可点） */}
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
                {/* 「回到底部」悬浮按钮：仅滚离底部较远时显示（sticky 贴右下角；自由滚动时回底用） */}
                {autoScroll.showJump && (
                    <button className="scroll-jump-btn" onClick={autoScroll.jumpToBottom} title="回到最新输出">
                      <Icon name="chevronDown" size={16} gap={0}/>
                    </button>
                )}
              </div>
              {/* §3.7.6 底部介入栏（压缩按钮 v2 归入介入栏按钮组；自动滚动开关在输入框旁）;
          Token 展示：上条（当前上下文长度 / 窗口上限）在输入框上方、下条（累计明细）在输入框下方 */}
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
                                // 2026-08-21：未运行不隐藏（全 -- 占位）；数据源——本会话实时
                                // metrics（statsLive）优先，否则读 DB 步骤级统计（刷新后恢复，与
                                // token 明细同表同位置）；首字延迟用 metrics 样本（有则实时平均，
                                // 无则 DB 平均）
                                <TokenMetrics startedAt={metrics.startedAt} endedAt={metrics.endedAt}
                                              activeSince={metrics.activeSince}
                                              ttftMs={metrics.ttftSamples ? metrics.ttftMs
                                                  : ((curStep?.ttft_samples ?? 0) > 0
                                                      ? ((curStep?.ttft_total_ms ?? 0) / (curStep?.ttft_samples ?? 1))
                                                      : null)}
                                    /* 2026-08-22：active 加 streaming——SSE 流已开始但轮询未回时
                                       1s tick 也启动（否则请求进行中统计不刷新） */
                                              active={curStep?.status === 'active' || streaming}
                                              runDurationMs={metrics.startedAt != null
                                                  ? metrics.runDurationMs : (curStep?.run_duration_ms ?? 0)}
                                              outputDurationMs={metrics.startedAt != null
                                                  ? metrics.outputDurationMs : (curStep?.output_duration_ms ?? 0)}
                                              requestCount={metrics.startedAt != null
                                                  ? metrics.requests : (curStep?.requests ?? 0)}
                                              completion={tokenCompletion}
                                    /* 2026-08-22：请求进行中实时——分母补差（进行中轮
                                       纯吐字时长，2026-08-24 从首字起算）、分子含进行中轮
                                       估算 token（渲染时读 ref，1s tick 刷新） */
                                              roundFirstAt={metrics.startedAt != null
                                                  ? (metricRequestRef.current?.firstTokenAt ?? null) : null}
                                              roundComp={metrics.startedAt != null ? roundCompRef.current : 0}/>
                              }
                />
              </div>
            </>
        )}
        {/* 选择确认弹窗：按所选选项继续（可附补充说明，发送给 AI） */}
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
        {/* 拒绝审批弹窗：原因必填（与总览页同交互） */}
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
        {/* 已完成步骤发送确认：告知会清除后续消息记录（含 monitor）并重置为未开始 */}
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
