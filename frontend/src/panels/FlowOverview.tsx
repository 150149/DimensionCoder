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

const CodeEditor = lazy(() => import('../editor/CodeEditor'))

interface FlowOverviewProps {
  taskId?: string
}

function isUnstarted(task: TaskSummary): boolean {
  if (task.status !== 'active') return false
  const steps = task.steps || []
  if (steps.length === 0) return false
  return steps.every((s) => s.status === 'pending')
}

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

function cardClass(s: StepDef, taskStatus: string, firstPendingId?: string): string {
  if (s.status === 'active' && s.human_attention === 'gate' && taskStatus === 'paused') return 'flow-card card-gate'
  if (s.status === 'active') return 'flow-card card-active'
  if (s.status === 'stopped') return 'flow-card card-stopped'
  if (s.status === 'skipped') return 'flow-card card-skipped'

  if (taskStatus === 'paused' && s.status === 'pending' && s.step_id === firstPendingId) return 'flow-card card-paused'
  return 'flow-card'
}

function fcStatus(s: StepDef): string {
  if (s.status === 'completed') return 'done'
  if (s.status === 'active' && s.human_attention === 'gate') return 'gate'
  if (s.status === 'active') return 'active'
  if (s.status === 'stopped') return 'stopped'
  if (s.status === 'skipped') return 'skipped'
  return 'pending'
}

function fcBadge(s: StepDef, taskStatus: string, firstPendingId?: string): string {
  if (s.status === 'completed') return '完成'
  if (s.status === 'active' && s.human_attention === 'gate') return 'Gate 待审批'
  if (s.status === 'active') return '正在执行'

  if (s.status === 'stopped') return '已暂停'
  if (s.status === 'skipped') return '已跳过'

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

  const [sentToast, setSentToast] = useState('')

  const [view, setView] = useState<'flow' | 'code'>('flow')
  const [openedPath, setOpenedPath] = useState<string | null>(null)
  const [editorDirty, setEditorDirty] = useState(false)

  const [aiMarkedFiles, setAiMarkedFiles] = useState<string[]>([])
  const [externalModified, setExternalModified] = useState<string[]>([])

  const [monitorSteps, setMonitorSteps] = useState<Record<string, { status: string; order: number; anchor?: string }>>({})

  const [stepTokens, setStepTokens] = useState<Record<string, StepTokens>>({})
  const [stepStats, setStepStats] = useState<Record<string, StepStats>>({})
  const [lightPrices, setLightPrices] = useState({in: 0, cached: 0, out: 0})
  const [powerPrices, setPowerPrices] = useState({in: 0, cached: 0, out: 0})
  const loadTask = useCallback(() => {
    if (!taskId) return Promise.resolve()
    return api.getTask(taskId).then((data) => {
      setTask(data.task)

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

  const stepCount = task ? task.steps.length : -1
  useEffect(() => {
    if (!task || stepCount > 0) return
    api
        .fsTree('')
        .then((tree) => setFsEmpty(!tree.entries || tree.entries.length === 0))
        .catch(() => setFsEmpty(false))

  }, [taskId, stepCount])

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

  const handleExternalModified = useCallback((path: string, modified: boolean) => {
    setExternalModified((prev) =>
        modified ? Array.from(new Set([...prev, path])) : prev.filter((p) => p !== path),
    )
  }, [])

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
    if (!reason) return
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

      setSentToast('消息已进入队列，将在流程节点处理时注入')
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败，请稍后重试')
    }
  }

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

  const sortedSteps = [...steps].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))

  const firstPendingId = sortedSteps.find((s) => s.status === 'pending')?.step_id
  const nodes = buildFlowNodes(sortedSteps)

  const nodeLo = (n: (typeof nodes)[number]) =>
      n.kind === 'single' ? (n.step.sort_order ?? 0) : Math.min(...n.steps.map((s) => s.sort_order ?? 0))
  const nodeHi = (n: (typeof nodes)[number]) =>
      n.kind === 'single' ? (n.step.sort_order ?? 0) : Math.max(...n.steps.map((s) => s.sort_order ?? 0))

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

              && segRanges.some(([l, h]) => displayOrder(sid, m) >= l && displayOrder(sid, m) < h))
          .map(([sid, m]) => displayOrder(sid, m)),
  )

  const phaseFallback = Object.entries(monitorSteps)
      .filter(([sid, m]) => sid.startsWith('monitor-') && sid !== 'monitor-init'
          && !coveredOrders.has(displayOrder(sid, m)))
  const unstarted = isUnstarted(task)
  const hasStopped = steps.some((s) => s.status === 'stopped')
  const gateActive = steps.some((s) => s.status === 'active' && s.human_attention === 'gate')
  const hasActive = steps.some((s) => s.status === 'active')

  const reviewing = task.status === 'paused' && hasActive && !gateActive

  const gateWaitingId = task.status === 'paused'
      ? steps.find((s) => s.human_attention === 'gate'
          && (s.status === 'active' || s.status === 'stopped'))?.step_id
      : undefined

  const reportDone = task.status === 'completed'

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
          {}
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
          {}
        </div>
    )
  }

  return (
      <>
        <div className="top-bar">
          {}
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
              {}
              {unstarted && (
                  <button className="btn btn-accent" onClick={handleStart}>
                    <Icon name="play" size={13}/>
                    启动任务
                  </button>
              )}
              {}
              {!unstarted && task.status === 'active' && (
                  <button className="btn" onClick={handlePause}>
                    <Icon name="pause" size={13}/>
                    暂停
                  </button>
              )}
              {}
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
              {}
              {hasStopped && (
                  <button className="btn btn-accent" onClick={handleResumeAll}>
                    <Icon name="play" size={13}/>
                    恢复全部
                  </button>
              )}
              {}
              <button
                  className={`btn best-effort-toggle${task?.best_effort ? ' active' : ''}`}
                  onClick={handleToggleBestEffort}
                  title="尽力模式：用户不在线时 gate 自动放行、AI 丧气时自动提醒继续"
              >
                <span className="toggle-dot"/>
                尽力模式
              </button>
              {}
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

                          const monitorBtnsIn = (lo: number, hi: number) =>
                              Object.entries(monitorSteps)
                                  .filter(([sid, m]) => sid.startsWith('monitor-')

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

                          const line = ni > 0 ? (
                              <div className={`flow-link${prevDone ? ' flow-link-done' : ''}`}>
                                <span className="flow-link-seg"/>
                                {prev && monitorBtnsIn(nodeHi(prev), nodeLo(node))}
                                <span className="flow-link-seg"/>
                              </div>
                          ) : null

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

                          return (
                              <div className={`flow-node${node.kind === 'parallel' ? ' flow-node-parallel' : ''}`} key={ni}>
                                {}
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
                      {}
                      <div className="flow-phase flow-phase-final">
                        <div className="flow-branch">
                          <div className="flow-node">
                            <div className={`flow-link${reportDone ? ' flow-link-done' : ''}`}>
                              <span className="flow-link-seg"/>
                              {}
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
              {}
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
        {}
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
