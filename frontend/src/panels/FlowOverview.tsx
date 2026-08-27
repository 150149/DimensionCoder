// ═══════════════════════════════════════════════════════════════
// FlowOverview（SWP4-B / WP4-3 §3.6）
// - Props：{taskId?: string}；useTaskPolling(1000) 拉 api.getTask(taskId)
// - 顶部 ProgressRail（Phase 分组：dev-full-flow step-1..4「分析与方案」/
//   step-5..8「实施」/其余「验证与审查」；其他预设按 sort_order 每 4 步
//   一组）；dot 点击跳步骤详情
// - 步骤卡片：🟢/🟣 tier 标签、必做/可选、active 卡「⏳ 正在执行」脉冲
//   标记（J4①）；Gate 卡（human_attention=gate && task.status==paused &&
//   step.status==active，V-03①/J8 三者齐备）显示「审批通过→」「拒绝✗」
//   （拒绝弹窗原因必填）；stopped 卡「▶恢复执行」→ resumeStep
// - P0-5 未启动检测：页面顶部「▶ 启动任务」；P2-14 删除确认流（ConfirmDialog
//   → deleteTask → 跳 /）；P2-15/M7 重审徽标（paused + active 且无 gate
//   active）；J3 任务级暂停/继续/「▶ 恢复全部」（遍历 stopped 逐个 resumeStep）
// - §3.6.8 底部 InterveneBar mode="flow"：💬发送（pending 排队）+ 🛑强制介入
//   （immediate，先 confirm「将打断当前流程」）
// - §3.6.9 不建立 SSE 连接（1s 轮询 + refreshData 由后端驱动）
// - §3.6.11 空工作区引导（fsTree('') 空 entries）
// - 无任务（taskId 空）→ 空态提示引导去 Sidebar 新建
// ═══════════════════════════════════════════════════════════════

import {lazy, Suspense, useCallback, useEffect, useMemo, useState} from 'react'
import {Navigate, useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {StepDef, StepStats, StepTokens, TaskSummary} from '../api/types'
import ConfirmDialog from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import ProgressRail from '../components/ProgressRail'
import {TokenDetails, TokenMetrics} from '../components/TokenUsageBar'
import {Icon} from '../components/icons'
import {useTaskPolling} from '../hooks/useTaskPolling'
import FileTree from '../editor/FileTree'
// §3.9：CodeEditor 懒加载（Monaco 不入主 bundle；dist/assets 独立 monaco chunk，P1-6）
const CodeEditor = lazy(() => import('../editor/CodeEditor'))

interface FlowOverviewProps {
  taskId?: string
}

/** P0-5：已创建未启动判定（与 Sidebar 卡片同步） */
function isUnstarted(task: TaskSummary): boolean {
  if (task.status !== 'active') return false
  const steps = task.steps || []
  if (steps.length === 0) return false
  return steps.every((s) => s.status === 'pending')
}

/** §3.9 M6：completed 步骤 artifacts（artifact_type=process/summary 的 preview 内容）
 *  中匹配 /[\w./-]+\.\w+/ 的路径，去重最多 5 个；存在性（已存在于全树）由
 *  FileTree 的已加载节点集合校验 */
const AI_FILE_PATH_RE = /[\w./-]+\.\w+/g
const AI_MARK_MAX = 5

function extractAiFiles(artifacts: unknown): string[] {
  if (!artifacts || typeof artifacts !== 'object') return []
  const found: string[] = []
  const seen = new Set<string>()
  for (const [key, val] of Object.entries(artifacts as Record<string, { preview?: string }>)) {
    if (!(key.endsWith('/process') || key.endsWith('/summary'))) continue
    const preview = typeof val?.preview === 'string' ? val.preview : ''
    const matches = preview.match(AI_FILE_PATH_RE) ?? []
    for (const m of matches) {
      if (seen.has(m)) continue
      seen.add(m)
      found.push(m)
      if (found.length >= AI_MARK_MAX) return found
    }
  }
  return found
}

// 2026-08-23（用户需求）：去除「阶段」概念——不再按 sort_order 每 4 步/step 前缀
// 分组渲染，流程总览为单一线性流程图（节点连接线顺序不变），monitor 眼睛归属
// 按全局线段区间（等价原「各组线段总和 + 最后组终点线」）

/** 并行分组：parallel_with 互引/指向的连续步骤合并为一组（流程图并排渲染） */
function buildFlowNodes(
    groupSteps: StepDef[],
): Array<{ kind: 'single'; step: StepDef } | { kind: 'parallel'; steps: StepDef[] }> {
  const nodes: Array<{ kind: 'single'; step: StepDef } | { kind: 'parallel'; steps: StepDef[] }> = []
  let i = 0
  while (i < groupSteps.length) {
    const cur = groupSteps[i]
    const next = groupSteps[i + 1]
    const linked = (a: StepDef, b: StepDef) =>
        a.parallel_with?.includes(b.step_id) || b.parallel_with?.includes(a.step_id)
    if (next && linked(cur, next)) {
      const group = [cur, next]
      let j = i + 2
      while (j < groupSteps.length) {
        const cand = groupSteps[j]
        if (group.some((g) => linked(g, cand))) {
          group.push(cand)
          j++
        } else break
      }
      nodes.push({kind: 'parallel', steps: group})
      i = j
    } else {
      nodes.push({kind: 'single', step: cur})
      i++
    }
  }
  return nodes
}

/** §3.6.3 卡片状态类（Gate 条件三者齐备，V-03①/J8） */
function cardClass(s: StepDef, taskStatus: string, firstPendingId?: string): string {
  if (s.status === 'active' && s.human_attention === 'gate' && taskStatus === 'paused') return 'flow-card card-gate'
  if (s.status === 'active') return 'flow-card card-active'
  if (s.status === 'stopped') return 'flow-card card-stopped'
  if (s.status === 'skipped') return 'flow-card card-skipped'
  // 2026-08-23（用户反馈修正）：任务暂停时仅「下一个待执行的 pending 步骤」
  // 灰左条弱化——被中断步骤已 stopped「已暂停」，其余 pending 保持普通待执行
  if (taskStatus === 'paused' && s.status === 'pending' && s.step_id === firstPendingId) return 'flow-card card-paused'
  return 'flow-card'
}

/** §3.6.3 状态圆点类（flowOverview.html fcStatusClass 逐字） */
function fcStatus(s: StepDef): string {
  if (s.status === 'completed') return 'done'
  if (s.status === 'active' && s.human_attention === 'gate') return 'gate'
  if (s.status === 'active') return 'active'
  if (s.status === 'stopped') return 'stopped'
  if (s.status === 'skipped') return 'skipped'
  return 'pending'
}

/** §3.6.3 徽标文本（J4①：active 卡「正在执行」脉冲标记） */
function fcBadge(s: StepDef, taskStatus: string, firstPendingId?: string): string {
  if (s.status === 'completed') return '完成'
  if (s.status === 'active' && s.human_attention === 'gate') return 'Gate 待审批'
  if (s.status === 'active') return '正在执行'
  // 2026-08-23（用户确认）：停止=暂停——「已暂停」灰色（非红非橙）；失败识别由
  // 详情页 llmError 红卡承担；与任务卡 st-paused 同色系
  if (s.status === 'stopped') return '已暂停'
  if (s.status === 'skipped') return '已跳过'
  // 2026-08-23（用户反馈修正）：仅「下一个待执行的 pending 步骤」标「暂停中」，
  // 其余 pending 保持「待执行」
  if (taskStatus === 'paused' && s.status === 'pending' && s.step_id === firstPendingId) return '暂停中'
  return '待执行'
}

function badgeClass(s: StepDef): string {
  if (s.status === 'completed') return 'fc-badge badge-done'
  if (s.status === 'active' && s.human_attention === 'gate') return 'fc-badge badge-gate'
  if (s.status === 'active') return 'fc-badge badge-active'
  if (s.status === 'stopped') return 'fc-badge badge-stopped'
  if (s.status === 'skipped') return 'fc-badge badge-skipped'
  return 'fc-badge badge-pending'
}

export default function FlowOverview({taskId}: FlowOverviewProps) {
  const navigate = useNavigate()
  const [task, setTask] = useState<TaskSummary | null>(null)
  const [error, setError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmForce, setConfirmForce] = useState(false)
  const [pendingForceText, setPendingForceText] = useState('')
  const [rejectStep, setRejectStep] = useState<StepDef | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [fsEmpty, setFsEmpty] = useState(false)
  // 发送成功反馈（flow pending 消息已排队，成功 toast）
  const [sentToast, setSentToast] = useState('')

  // §3.9：FlowOverview 顶栏「📂 代码」tab（state 切换，非路由）；左侧
  // FileTree 右侧 CodeEditor；dirty 标记 tab 标题 ●（A1 修订：仅 tab 接入）
  const [view, setView] = useState<'flow' | 'code'>('flow')
  const [openedPath, setOpenedPath] = useState<string | null>(null)
  const [editorDirty, setEditorDirty] = useState(false)
  // M6/J2b：AI 修改高亮候选路径 + 外部修改角标路径
  const [aiMarkedFiles, setAiMarkedFiles] = useState<string[]>([])
  const [externalModified, setExternalModified] = useState<string[]>([])

  // §3.6.1：useTaskPolling(1000) 拉 getTask；失败静默（hook 规格），下一轮重试
  // 2026-08-21 实体化：monitor 步骤（type=monitor/review/report）被 getTask 过滤，
  // 流程图眼睛图标的多实例归属依赖 monitor-conversations 端点的状态+sort_order
  // 2026-08-23：anchor（触发步骤 id）——monitor sort_order 被后续插入步骤挤压
  // 漂移（DB 实证 10092ff1 monitor-6 13→19），眼睛按锚点步骤归属而非自身 order
  const [monitorSteps, setMonitorSteps] = useState<Record<string, { status: string; order: number; anchor?: string }>>({})
  // 2026-08-23（用户需求）：流程总览输入框下展示全流程 Token 明细/金额/运行统计
  // ——真实步骤在 task.steps（getTask），monitor/review/report 步骤在端点 29
  // 的 step_tokens/step_stats（monitor 步骤 tier 固定 power，系统创建时写入）
  const [stepTokens, setStepTokens] = useState<Record<string, StepTokens>>({})
  const [stepStats, setStepStats] = useState<Record<string, StepStats>>({})
  const [lightPrices, setLightPrices] = useState({in: 0, cached: 0, out: 0})
  const [powerPrices, setPowerPrices] = useState({in: 0, cached: 0, out: 0})
  const loadTask = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return api.getTask(taskId).then((data) => {
      setTask(data.task)
      // §3.9 M6：completed 步骤 artifacts 提取 🔧 徽标候选（最多 5 个）
      setAiMarkedFiles(extractAiFiles(data.artifacts))
      return api.getMonitorConversations(taskId)
    }).then((d) => {
      if (!d) return
      setStepTokens(d.step_tokens ?? {})
      setStepStats(d.step_stats ?? {})
      const order = d.monitor_order ?? {}
      const anchors = d.monitor_anchors ?? {}
      const merged: Record<string, { status: string; order: number; anchor?: string }> = {}
      for (const [sid, st] of Object.entries(d.monitor_steps ?? {})) {
        merged[sid] = {status: st, order: order[sid] ?? 0, anchor: anchors[sid]}
      }
      setMonitorSteps(merged)
    }).catch(() => {
    })
  }, [taskId])
  useTaskPolling(1000, loadTask, [loadTask])

  // §3.6.11 空工作区引导（J6）：任务 steps 为空时查 fsTree('')
  const stepCount = task ? task.steps.length : -1
  useEffect(() => {
    if (!task || stepCount > 0) return
    api
        .fsTree('')
        .then((tree) => setFsEmpty(!tree.entries || tree.entries.length === 0))
        .catch(() => setFsEmpty(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, stepCount])

  // 2026-08-23（用户需求）：light/power 六项价格（每 1M token 单价，缺省 0）——
  // 任务级金额汇总按步骤 model_tier 选价格组；拉取失败保持 0（金额显示 0）
  useEffect(() => {
    let alive = true
    api
        .getConfig()
        .then((cfg) => {
          if (!alive) return
          setLightPrices({in: cfg.lightInputPrice ?? 0, cached: cfg.lightCachedPrice ?? 0, out: cfg.lightOutputPrice ?? 0})
          setPowerPrices({in: cfg.powerInputPrice ?? 0, cached: cfg.powerCachedPrice ?? 0, out: cfg.powerOutputPrice ?? 0})
        })
        .catch(() => {
        })
    return () => {
      alive = false
    }
  }, [])

  // 2026-08-23（用户需求）：流程级汇总——真实步骤（task.steps 累计列）+ monitor/
  // review/report 步骤（端点 29 step_tokens/step_stats），随 1s 轮询刷新；
  // 金额：真实步骤按 model_tier 选价格组（缺省 power），monitor 步骤固定 power
  const totals = useMemo(() => {
    const t = {
      prompt: 0, cached: 0, completion: 0, requests: 0,
      ttftTotal: 0, ttftSamples: 0, outputDurationMs: 0, runDurationMs: 0
    }
    for (const s of task?.steps ?? []) {
      t.prompt += s.token_prompt ?? 0
      t.cached += s.token_cached ?? 0
      t.completion += s.token_completion ?? 0
      t.requests += s.requests ?? 0
      t.ttftTotal += s.ttft_total_ms ?? 0
      t.ttftSamples += s.ttft_samples ?? 0
      t.outputDurationMs += s.output_duration_ms ?? 0
      t.runDurationMs += s.run_duration_ms ?? 0
    }
    for (const st of Object.values(stepTokens)) {
      t.prompt += st.token_prompt ?? 0
      t.cached += st.token_cached ?? 0
      t.completion += st.token_completion ?? 0
    }
    for (const st of Object.values(stepStats)) {
      t.requests += st.requests ?? 0
      t.ttftTotal += st.ttft_total_ms ?? 0
      t.ttftSamples += st.ttft_samples ?? 0
      t.outputDurationMs += st.output_duration_ms ?? 0
      t.runDurationMs += st.run_duration_ms ?? 0
    }
    return t
  }, [task, stepTokens, stepStats])
  const totalCost = useMemo(() => {
    // 2026-08-24（修复缓存命中双重计价）：token_prompt 含缓存命中——未缓存输入
    // （prompt-cached）按 in 单价，缓存命中按 cached 单价，不再重复计价
    let cost = 0
    for (const s of task?.steps ?? []) {
      const p = (s.model_tier ?? 'power') === 'light' ? lightPrices : powerPrices
      cost += Math.max(0, (s.token_prompt ?? 0) - (s.token_cached ?? 0)) * p.in
          + (s.token_cached ?? 0) * p.cached + (s.token_completion ?? 0) * p.out
    }
    for (const st of Object.values(stepTokens)) {
      cost += Math.max(0, (st.token_prompt ?? 0) - (st.token_cached ?? 0)) * powerPrices.in
          + (st.token_cached ?? 0) * powerPrices.cached
          + (st.token_completion ?? 0) * powerPrices.out
    }
    return cost / 1e6
  }, [task, stepTokens, lightPrices, powerPrices])
  const totalsTtftMs = totals.ttftSamples > 0 ? totals.ttftTotal / totals.ttftSamples : null

  // J2b 外部修改角标路径增删（CodeEditor 轮询上报）
  const handleExternalModified = useCallback((path: string, modified: boolean) => {
    setExternalModified((prev) =>
        modified ? Array.from(new Set([...prev, path])) : prev.filter((p) => p !== path),
    )
  }, [])

  // 2026-08-22：steps 为空（编排中）不再显示静态等待页——自动重定向到
  // monitor-init 现成详情页（工具卡/思考/提示词/Token 全量展示，见 MonitorDetail）
  const [starting, setStarting] = useState(false)

  const handleStart = async () => {
    if (!taskId || starting) return
    setStarting(true)
    try {
      await api.startTask(taskId)
      loadTask()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动失败，请稍后重试')
    } finally {
      setStarting(false)
    }
  }

  const handlePause = async () => {
    if (!taskId) return
    try {
      await api.pauseTask(taskId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '暂停失败，请稍后重试')
    }
  }

  // 尽力模式开关（2026-08-16 用户需求）：开启后 gate 审批自动放行（用户暂时不在线
  // 自动走决策路径）+ 防放弃提醒 + 收尾复核；乐观更新 + loadTask 刷新
  const handleToggleBestEffort = async () => {
    if (!taskId || !task) return
    const next = !task.best_effort
    setTask({...task, best_effort: next})
    try {
      await api.setBestEffort(taskId, next)
      loadTask()
    } catch (err) {
      setTask({...task, best_effort: !next})
      setError(err instanceof Error ? err.message : '切换尽力模式失败，请稍后重试')
    }
  }

  const handleResume = async () => {
    if (!taskId || !task) return
    try {
      // 2026-08-23（用户反馈 99248a9f）：继续 = 先恢复所有非 gate 的 stopped 步骤
      // （get_next_steps 已阻塞——不显式恢复会跳过中断点跑后续待执行）+ 重启循环
      for (const s of task.steps) {
        if (s.status === 'stopped' && s.human_attention !== 'gate') {
          await api.resumeStep(taskId, s.step_id)
        }
      }
      await api.startTask(taskId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '继续失败，请稍后重试')
    }
  }

  const handleResumeAll = async () => {
    if (!taskId || !task) return
    try {
      for (const s of task.steps) {
        // 2026-08-21（评审确认）：跳过 stopped 的 gate 步骤——决策包已提交
        // = 等审批语义，恢复全部重跑会把已提交决策包作废；其余 stopped 全恢复
        if (s.status === 'stopped' && s.human_attention !== 'gate') {
          await api.resumeStep(taskId, s.step_id)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复失败，请稍后重试')
    }
  }

  const handleResumeStep = async (stepId: string) => {
    if (!taskId) return
    try {
      await api.resumeStep(taskId, stepId)
      // v2：resume 仅恢复状态（契约），须显式 start 重启执行循环——否则
      // llmError/打断后循环已退出，恢复后任务卡住不继续
      await api.startTask(taskId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复失败，请稍后重试')
    }
  }

  const handleApprove = async (stepId: string) => {
    if (!taskId) return
    try {
      await api.approveGate(taskId, stepId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '审批失败，请稍后重试')
    }
  }

  const handleRejectConfirm = async () => {
    if (!taskId || !rejectStep) return
    const reason = rejectReason.trim()
    if (!reason) return // 拒绝原因必填
    try {
      await api.rejectGate(taskId, rejectStep.step_id, reason)
      setRejectStep(null)
      setRejectReason('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '拒绝失败，请稍后重试')
    }
  }

  const handleDelete = async () => {
    setConfirmDelete(false)
    if (!taskId) return
    try {
      await api.deleteTask(taskId)
      navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败，请稍后重试')
    }
  }

  const handleSend = async (content: string) => {
    if (!taskId) return
    try {
      await api.flowIntervene(taskId, 'pending', content)
      // 成功反馈：pending 消息排队注入，流程节点处理后才进入对话（本地确认可见）
      setSentToast('消息已进入队列，将在流程节点处理时注入')
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败，请稍后重试')
    }
  }

  // §3.6.8 强制介入：先 confirm「将打断当前流程」再提交 immediate
  const handleForce = (content: string) => {
    setPendingForceText(content)
    setConfirmForce(true)
  }

  const doForce = async () => {
    setConfirmForce(false)
    if (!taskId) return
    try {
      await api.flowIntervene(taskId, 'immediate', pendingForceText)
    } catch (err) {
      setError(err instanceof Error ? err.message : '强制介入失败，请稍后重试')
    }
  }

  // 无任务：空态提示 + 引导去 Sidebar 新建
  if (!taskId) {
    return (
        <div className="flow-area">
          <div className="empty-state">
            暂无任务
            <br/>
            请从左侧 Sidebar 新建任务
          </div>
        </div>
    )
  }

  if (!task) {
    return (
        <div className="flow-area">
          <div className="empty-state">加载中...</div>
        </div>
    )
  }

  const steps = task.steps || []
  // 2026-08-23（用户需求）：去除阶段分组——单一线性流程图，节点按 sort_order 排序
  const sortedSteps = [...steps].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
  // 2026-08-23（用户反馈修正）：任务暂停时仅「下一个待执行的 pending 步骤」标
  // 暂停中/灰左条——被中断步骤已 stopped「已暂停」，后续步骤仍是「待执行」
  const firstPendingId = sortedSteps.find((s) => s.status === 'pending')?.step_id
  const nodes = buildFlowNodes(sortedSteps)
  // 线段区间/已覆盖集合全局计算（2026-08-23 DB 实证 60b8e589：组内计算会让 fallback
  // 看不到已覆盖 monitor 导致重复挂载）——节点间线段 + 末节点 completed 的终点线区间
  const nodeLo = (n: (typeof nodes)[number]) =>
      n.kind === 'single' ? (n.step.sort_order ?? 0) : Math.min(...n.steps.map((s) => s.sort_order ?? 0))
  const nodeHi = (n: (typeof nodes)[number]) =>
      n.kind === 'single' ? (n.step.sort_order ?? 0) : Math.max(...n.steps.map((s) => s.sort_order ?? 0))
  // 2026-08-23（DB 实证 10092ff1 monitor-6 13→19）：眼睛归属 order——有锚点
  // （触发步骤 id）时用锚点步骤当前 sort_order + 0.5（锚点步骤不被后续插入
  // 挤压漂移，落在其后的线段区间）；无锚点/锚点步骤不存在回退 monitor 自身
  // sort_order（含 fallback 兜底，兼容历史数据）
  // 2026-08-24（根因修复）：介入实例（monitor-intervene-*）锚点语义为「锚点
  // 步骤之前」（介入挂当前运行步骤前排队）——用 - 0.5，与审查 + 0.5 对称
  const displayOrder = (sid: string, m: { status: string; order: number; anchor?: string }): number => {
    if (m.anchor) {
      const anchorStep = steps.find((s) => s.step_id === m.anchor)
      if (anchorStep) {
        const o = anchorStep.sort_order ?? m.order
        return sid.startsWith('monitor-intervene-') ? o - 0.5 : o + 0.5
      }
    }
    return m.order
  }
  const segRanges: Array<[number, number]> = []
  nodes.forEach((n, ni) => {
    if (ni === 0) return
    segRanges.push([nodeHi(nodes[ni - 1]), nodeLo(n)])
  })
  const lastNode = nodes[nodes.length - 1]
  const lastNodeDone =
      lastNode &&
      (lastNode.kind === 'single'
          ? lastNode.step.status === 'completed'
          : lastNode.steps.every((s) => s.status === 'completed'))
  if (lastNodeDone) {
    segRanges.push([nodeHi(lastNode), Number.MAX_SAFE_INTEGER])
  }
  const coveredOrders = new Set(
      Object.entries(monitorSteps)
          .filter(([sid, m]) => sid.startsWith('monitor-')
              && m.order != null
              // 2026-08-24（用户反馈 10092ff1）：与 monitorBtnsIn 同区间 [lo, hi)
              && segRanges.some(([l, h]) => displayOrder(sid, m) >= l && displayOrder(sid, m) < h))
          .map(([sid, m]) => displayOrder(sid, m)),
  )
  // 2026-08-24（用户反馈 10092ff1）：未覆盖 monitor（order 漂移无锚点，如 monitor-7/8
  // 14/16 超出 branch 区间）统一挂「产出报告前的线」——不再挂最后卡片下方
  // （视觉堆叠难看）；与 review 按钮同线，流程尾部审查集中可见
  const phaseFallback = Object.entries(monitorSteps)
      .filter(([sid, m]) => sid.startsWith('monitor-') && sid !== 'monitor-init'
          && !coveredOrders.has(displayOrder(sid, m)))
  const unstarted = isUnstarted(task)
  const hasStopped = steps.some((s) => s.status === 'stopped')
  const gateActive = steps.some((s) => s.status === 'active' && s.human_attention === 'gate')
  const hasActive = steps.some((s) => s.status === 'active')
  // P2-15/M7：paused + 存在 active 步骤 + 不存在 gate active 步骤（排除 Gate 等待审批）
  const reviewing = task.status === 'paused' && hasActive && !gateActive
  // 2026-08-23（用户反馈 99248a9f）：gate 审批等待——paused + gate 步骤 active（等审批）
  // 或 stopped（决策包已提交）；pending 的 gate 未执行过不算等待 → 「继续」恢复执行
  const gateWaitingId = task.status === 'paused'
      ? steps.find((s) => s.human_attention === 'gate'
          && (s.status === 'active' || s.status === 'stopped'))?.step_id
      : undefined
  // 产出报告可用性：任务完成后 final review 已生成（前端合成节点状态）
  const reportDone = task.status === 'completed'

  /** 流程图节点卡片（Gate 审批 / 恢复执行 / Monitor 胶囊按钮） */
  const renderStepCard = (s: StepDef) => {
    const isGate = s.status === 'active' && s.human_attention === 'gate' && task.status === 'paused'
    return (
        <div
            key={s.step_id}
            className={cardClass(s, task.status, firstPendingId)}
            onClick={() => navigate(`/task/${taskId}/step/${s.step_id}`)}
        >
          <div className="fc-row">
            <div className={`fc-status fc-s-${fcStatus(s)}`}/>
            <span className="fc-name">{s.title}</span>
            <span className={badgeClass(s)}>{fcBadge(s, task.status, firstPendingId)}</span>
          </div>
          <div className="fc-meta">
            <span className={`model-dot ${s.model_tier === 'light' ? 'model-dot-light' : 'model-dot-power'}`}/>
            {s.model_tier === 'light' ? '轻量' : '强力'}
            <span>{s.required ? '必做' : '可选'}</span>
          </div>
          {/* V-03①/J8：Gate 卡按钮（条件三者齐备）；J5：选项类（AI 已输出决策包，后端
            has_decision_pkg）→「去决策」进详情页选择；审批类 → 通过/拒绝 */}
          {isGate &&
              (s.has_decision_pkg ? (
                  <div className="fc-row">
                    <button
                        className="gate-btn"
                        onClick={(e) => {
                          e.stopPropagation()
                          navigate(`/task/${taskId}/step/${s.step_id}`)
                        }}
                    >
                      去决策 <Icon name="chevronRight" size={12} gap={4}/>
                    </button>
                  </div>
              ) : (
                  <div className="fc-row">
                    <button
                        className="gate-btn"
                        onClick={(e) => {
                          e.stopPropagation()
                          handleApprove(s.step_id)
                        }}
                    >
                      审批通过 <Icon name="check" size={13} gap={4}/>
                    </button>
                    <button
                        className="reject-btn"
                        onClick={(e) => {
                          e.stopPropagation()
                          setRejectStep(s)
                          setRejectReason('')
                        }}
                    >
                      拒绝 <Icon name="close" size={12} gap={4}/>
                    </button>
                  </div>
              ))}
          {!isGate && s.status === 'stopped' && (
              <button
                  className="resume-btn"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleResumeStep(s.step_id)
                  }}
              >
                <Icon name="play" size={12} gap={4}/>
                恢复执行
              </button>
          )}
          {/* §3.6.7：completed 步骤的 Monitor 入口已移至卡片上方连接线中点圆钮（v2） */}
        </div>
    )
  }

  return (
      <>
        <div className="top-bar">
          {/* 单行顶栏：标题+元信息（左）｜ 操作按钮 + 流程/代码 tab（右） */}
          <div className="tb-title-group">
          <span className="task-header">
            {task.title || 'Untitled'}
            {reviewing && (
                <span className="monitor-badge">
                <Icon name="settings" size={12} gap={0}/>
                重审中
              </span>
            )}
          </span>
            <span className="task-meta">
            类型：{task.type} · 状态：{task.status}
          </span>
          </div>
          <div className="tb-tools">
            <div className="flow-actions">
              {/* P0-5：已创建未启动 → 顶部「启动任务」 */}
              {unstarted && (
                  <button className="btn btn-accent" onClick={handleStart}>
                    <Icon name="play" size={13}/>
                    启动任务
                  </button>
              )}
              {/* J3：任务执行中（active 且非未启动）→ 暂停 */}
              {!unstarted && task.status === 'active' && (
                  <button className="btn" onClick={handlePause}>
                    <Icon name="pause" size={13}/>
                    暂停
                  </button>
              )}
              {/* J3：task paused → 继续（端点 31 已允许 paused）；gate 审批等待 → 「去审批」
                  直接跳 gate 步骤详情（2026-08-23：此前点继续后端静默 return 无反馈） */}
              {task.status === 'paused' && gateWaitingId && (
                  <button
                      className="btn btn-accent"
                      onClick={() => navigate(`/task/${taskId}/step/${gateWaitingId}`)}
                  >
                    <Icon name="check" size={13}/>
                    去审批
                  </button>
              )}
              {task.status === 'paused' && !gateWaitingId && (
                  <button className="btn btn-accent" onClick={handleResume}>
                    <Icon name="play" size={13}/>
                    继续
                  </button>
              )}
              {/* J3：存在 stopped 步骤 → 恢复全部（逐个 resumeStep） */}
              {hasStopped && (
                  <button className="btn btn-accent" onClick={handleResumeAll}>
                    <Icon name="play" size={13}/>
                    恢复全部
                  </button>
              )}
              {/* 尽力模式开关（2026-08-16 用户需求）：gate 自动放行 + 防放弃提醒 */}
              <button
                  className={`btn best-effort-toggle${task?.best_effort ? ' active' : ''}`}
                  onClick={handleToggleBestEffort}
                  title="尽力模式：用户不在线时 gate 自动放行、AI 丧气时自动提醒继续"
              >
                <span className="toggle-dot"/>
                尽力模式
              </button>
              {/* P2-14：删除任务（确认流） */}
              <button className="btn btn-danger" onClick={() => setConfirmDelete(true)}>
                <Icon name="trash" size={13}/>
                删除
              </button>
            </div>
            <div className="flow-tabs">
              <button className={`tab-btn${view === 'flow' ? ' active' : ''}`} onClick={() => setView('flow')}>
                <Icon name="list" size={13} gap={5}/>
                流程
              </button>
              <button className={`tab-btn${view === 'code' ? ' active' : ''}`} onClick={() => setView('code')}>
                <Icon name="folder" size={13} gap={5}/>
                代码
                {editorDirty && <span className="tab-dirty"/>}
              </button>
            </div>
          </div>
        </div>
        {view === 'code' && (
            <div className="flow-code-split">
              <FileTree
                  onOpenFile={setOpenedPath}
                  markedPaths={aiMarkedFiles}
                  externalModifiedPaths={externalModified}
              />
              <Suspense fallback="加载编辑器...">
                <CodeEditor
                    path={openedPath}
                    onDirtyChange={setEditorDirty}
                    onExternalModified={handleExternalModified}
                />
              </Suspense>
            </div>
        )}
        {view === 'flow' && (
            <div className="flow-view">
              <div className="progress-wrap">
                <ProgressRail
                    steps={steps}
                    pausedPendingId={task.status === 'paused' ? firstPendingId : undefined}
                    onDotClick={(stepId) => navigate(`/task/${taskId}/step/${stepId}`)}
                />
              </div>
              <div className="flow-area">
                {steps.length === 0 ? (
                    fsEmpty ? (
                        <div className="empty-state">
                          工作区为空：请在设置页配置项目目录，或将代码放入默认工作区（设置页 projectRoot）
                        </div>
                    ) : (
                        // 2026-08-22：编排中 → monitor-init 详情页（完整过程展示）
                        <Navigate to={`/task/${taskId}/monitor/monitor-init`} replace/>
                    )
                ) : (
                    <>
                      <div className="flow-branch">
                        {nodes.map((node, ni) => {
                          const prev = nodes[ni - 1]
                          const prevDone =
                              prev &&
                              (prev.kind === 'single'
                                  ? prev.step.status === 'completed'
                                  : prev.steps.every((s) => s.status === 'completed'))
                          const isLast = ni === nodes.length - 1
                          // 实体化（2026-08-21）：区间内 monitor 步骤（type=monitor，
                          // monitor-N / monitor-intervene-N）→ 一条连接线可多个眼睛
                          // 图标横向排开（去绑定普通化：不再排除介入实例——按自身
                          // sort_order 挂线段）；review/report 独立类型由
                          // flow-phase-final 区块展示。
                          const monitorBtnsIn = (lo: number, hi: number) =>
                              Object.entries(monitorSteps)
                                  .filter(([sid, m]) => sid.startsWith('monitor-')
                                      // 2026-08-24（用户反馈 10092ff1）：区间 [lo, hi)——
                                      // monitor 的 sort_order 与其触发步骤同值（monitor-1=3
                                      // == step-2），(lo,hi] 会把 order==hi 归入前一段（眼睛
                                      // 挂触发步骤之前的线）；[lo,hi) 语义：挂在 order 数值
                                      // 所在步骤之后（与锚点 +0.5 一致）
                                      && displayOrder(sid, m) >= lo && displayOrder(sid, m) < hi)
                                  .sort((a, b) => displayOrder(a[0], a[1]) - displayOrder(b[0], b[1]))
                                  .map(([sid, m]) => (
                                      <button
                                          key={sid}
                                          className={`flow-link-btn${m.status === 'active' ? ' active' : ''}`}
                                          title={m.status === 'active' ? 'Monitor 思考进行中' : '查看 Monitor 思考过程'}
                                          onClick={(e) => {
                                            e.stopPropagation()
                                            navigate(`/task/${taskId}/monitor/${sid}`)
                                          }}
                                      >
                            <span className="link-icon">
                              <Icon name="eye" size={12} gap={0}/>
                            </span>
                                      </button>
                                  ))
                          // 节点间连接线（ni>0）：圆钮归属区间 (prev 最大 order, node 最小 order]
                          const line = ni > 0 ? (
                              <div className={`flow-link${prevDone ? ' flow-link-done' : ''}`}>
                                <span className="flow-link-seg"/>
                                {prev && monitorBtnsIn(nodeHi(prev), nodeLo(node))}
                                <span className="flow-link-seg"/>
                              </div>
                          ) : null
                          // 终点线：末节点全部 completed 时渲染（其后的 monitor 步骤入口）
                          const lastDone =
                              isLast &&
                              (node.kind === 'single'
                                  ? node.step.status === 'completed'
                                  : node.steps.every((s) => s.status === 'completed'))
                          const endLine = lastDone ? (
                              <div className="flow-link flow-link-done">
                                <span className="flow-link-seg"/>
                                {monitorBtnsIn(nodeHi(node), Number.MAX_SAFE_INTEGER)}
                                <span className="flow-link-seg"/>
                              </div>
                          ) : null
                          // Monitor 节点兜底已移除（2026-08-24）：未覆盖实例统一挂
                          // 产出报告前的线（phaseFallback），不再挂最后 completed 节点
                          // 下方（用户反馈视觉堆叠难看）
                          return (
                              <div className={`flow-node${node.kind === 'parallel' ? ' flow-node-parallel' : ''}`} key={ni}>
                                {/* 起点线 + 编排圆钮：流程第一条连线——创建时的编排对话入口
                            （实体化：monitor-init 真实步骤） */}
                                {ni === 0 && (
                                    <div className="flow-link flow-link-plan">
                                      <span className="flow-link-seg"/>
                                      <button
                                          className={`flow-link-btn${monitorSteps['monitor-init']?.status === 'active' ? ' active' : ''}`}
                                          title="查看流程编排对话"
                                          onClick={(e) => {
                                            e.stopPropagation()
                                            navigate(`/task/${taskId}/monitor/monitor-init`)
                                          }}
                                      >
                              <span className="link-icon">
                                <Icon name="compass" size={12} gap={0}/>
                              </span>
                                      </button>
                                      <span className="flow-link-seg"/>
                                    </div>
                                )}
                                {line}
                                {node.kind === 'parallel' ? (
                                    <div className="flow-parallel-row">
                                      {node.steps.map((s) => renderStepCard(s))}
                                    </div>
                                ) : (
                                    renderStepCard(node.step)
                                )}
                                {endLine}
                              </div>
                          )
                        })}
                      </div>
                      {/* 产出报告节点：卡片→产出报告页 report；连接线圆钮→最终审查页 review
              （实体化：review/report 独立类型真实步骤） */}
                      <div className="flow-phase flow-phase-final">
                        <div className="flow-branch">
                          <div className="flow-node">
                            <div className={`flow-link${reportDone ? ' flow-link-done' : ''}`}>
                              <span className="flow-link-seg"/>
                              {/* 2026-08-27（用户反馈 5b2519ef）：final review 圆钮必须
                                  在最前——介入/漂移 monitor 的眼睛（phaseFallback）后置，
                                  否则眼睛遮挡 review 入口（用户误点进入 monitor 详情） */}
                              {reportDone && (
                                  <button
                                      className="flow-link-btn"
                                      title="查看最终审查"
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        navigate(`/task/${taskId}/monitor/review`)
                                      }}
                                  >
                      <span className="link-icon">
                        <Icon name="file" size={12} gap={0}/>
                      </span>
                                  </button>
                              )}
                              {/* 2026-08-24：未覆盖 monitor（order 漂移无锚点）统一挂此线
                                  ——不再挂最后卡片下方；与 review 按钮同线 */}
                              {phaseFallback.map(([sid, m]) => (
                                  <button
                                      key={sid}
                                      className={`flow-link-btn${m.status === 'active' ? ' active' : ''}`}
                                      title={m.status === 'active' ? 'Monitor 思考进行中' : '查看 Monitor 思考过程'}
                                      onClick={(e) => {
                                        e.stopPropagation()
                                        navigate(`/task/${taskId}/monitor/${sid}`)
                                      }}
                                  >
                      <span className="link-icon">
                        <Icon name="eye" size={12} gap={0}/>
                      </span>
                                  </button>
                              ))}
                              <span className="flow-link-seg"/>
                            </div>
                            <div
                                className={`flow-card${reportDone ? '' : ' card-pending'}`}
                                onClick={() => navigate(`/task/${taskId}/monitor/report`)}
                            >
                              <div className="fc-row">
                                <div className={`fc-status fc-s-${reportDone ? 'done' : 'pending'}`}/>
                                <span className="fc-name">产出报告</span>
                                <span className={`fc-badge badge-${reportDone ? 'done' : 'pending'}`}>
                      {reportDone ? '已完成' : '待生成'}
                    </span>
                              </div>
                              <div className="fc-meta">流程整体产出汇总与最终答案</div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </>
                )}
              </div>
              {/* §3.6.8：介入栏（发送 pending 排队 / 强制介入 immediate 先确认；
                  运行中=任务执行中 或 gate 审批等待（paused + 有 active 步骤——
                  2026-08-21 用户反馈：paused(gate) 时流程仍在运行但没有强制插入
                  按钮，只能排队发送而排队在审批等待期不生效，流程"不动"）
                  2026-08-23（用户需求）：下方追加全流程 Token 明细/金额/运行统计
                  ——任务级汇总（真实步骤 + monitor 步骤），随 1s 轮询刷新 */}
              <div className="step-footer">
                <InterveneBar mode="flow" onSend={handleSend} onForce={handleForce}
                              running={task.status === 'active' ||
                                  (task.status === 'paused' && (task.steps ?? []).some((s) => s.status === 'active'))}/>
                <TokenDetails prompt={totals.prompt} cached={totals.cached} completion={totals.completion}
                              cost={totalCost}
                              trailing={<TokenMetrics startedAt={null} endedAt={null} active={false} activeSince={null}
                                                      runDurationMs={totals.runDurationMs} outputDurationMs={totals.outputDurationMs}
                                                      ttftMs={totalsTtftMs} requestCount={totals.requests}
                                                      completion={totals.completion}/>}/>
              </div>
            </div>
        )}
        <ConfirmDialog
            open={confirmDelete}
            title="删除任务"
            rows={[{step_id: task.id, title: '将删除任务及全部对话，不可恢复', required: false}]}
            onConfirm={handleDelete}
            onCancel={() => setConfirmDelete(false)}
            confirmText="确认删除"
        />
        <ConfirmDialog
            open={confirmForce}
            title="强制介入"
            rows={[{step_id: 'force', title: '将打断当前流程', required: false}]}
            onConfirm={doForce}
            onCancel={() => setConfirmForce(false)}
            confirmText="确认介入"
        />
        {/* 拒绝审批弹窗：原因必填 */}
        {rejectStep && (
            <div
                className="modal-overlay show"
                onClick={() => {
                  setRejectStep(null)
                  setRejectReason('')
                }}
            >
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
                  <button
                      onClick={() => {
                        setRejectStep(null)
                        setRejectReason('')
                      }}
                  >
                    取消
                  </button>
                  <button className="primary" disabled={!rejectReason.trim()} onClick={handleRejectConfirm}>
                    确认拒绝
                  </button>
                </div>
              </div>
            </div>
        )}
        {error && <ErrorToast message={error} onClose={() => setError('')}/>}
        {sentToast && <ErrorToast variant="success" message={sentToast} onClose={() => setSentToast('')}/>}
      </>
  )
}
