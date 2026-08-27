// ═══════════════════════════════════════════════════════════════
// ProgressRail（WP4-1 §3 组件清单：props {steps, currentStepId?, onDotClick?}）
// 进度轨道（pt-dot/pt-rail，来源 flowOverview.html L16-26 逐字逻辑）：
// - 状态映射（flowOverview mapStatus）：pending/active/completed→done/
//   skipped/stopped；ptDotClass：done/active/skipped/stopped→gate/pending
// - 步骤间 .pt-rail（前一步 done 时 .pt-rail-done）
// - 2026-08-23（用户需求）：去除阶段分组（phaseGroups/.pt-phase）——轨道连续渲染
// ═══════════════════════════════════════════════════════════════

import type {ReactNode} from 'react'

export interface ProgressStep {
  step_id: string
  title: string
  status: string
  // 2026-08-23（用户需求）：gate 待审批判定（与 FlowOverview 卡片同口径）
  human_attention?: string
}

interface ProgressRailProps {
  steps: ProgressStep[]
  currentStepId?: string
  onDotClick?: (stepId: string) => void
  /** 2026-08-23（用户反馈）：任务暂停时下一个待执行的 pending 步骤 id——
   *  顶栏轨道深灰标记「暂停中断点」（与总览卡片「暂停中」一致） */
  pausedPendingId?: string
}

function mapStatus(status: string): string {
  if (status === 'completed') return 'done'
  if (status === 'active') return 'active'
  if (status === 'skipped') return 'skipped'
  if (status === 'stopped') return 'stopped'
  return 'pending'
}

function ptDotClass(status: string, humanAttention?: string, pausedPendingId?: string, stepId?: string): string {
  // 2026-08-23（用户需求）：gate 待审批（active+gate）优先判定 → 黄/橙
  // （卡片 card-gate 同色——此前仅看 status=active 落蓝色）；stopped →
  // pt-dot-stopped 红（修复误用 pt-dot-gate 类名的错位）
  if (status === 'active' && humanAttention === 'gate') return 'pt-dot pt-dot-gate'
  const st = mapStatus(status)
  if (st === 'done') return 'pt-dot pt-dot-done'
  if (st === 'active') return 'pt-dot pt-dot-active'
  if (st === 'skipped') return 'pt-dot pt-dot-skipped'
  if (st === 'stopped') return 'pt-dot pt-dot-stopped'
  // 2026-08-23（用户反馈）：任务暂停 → 下一个待执行步骤深灰圆点（暂停中断点可见）
  if (st === 'pending' && pausedPendingId != null && stepId === pausedPendingId) return 'pt-dot pt-dot-paused'
  return 'pt-dot pt-dot-pending'
}

export default function ProgressRail({steps, currentStepId, onDotClick, pausedPendingId}: ProgressRailProps) {
  // 2026-08-25（用户需求修订）：动态折叠——显示上限 16 个圆点（1 折叠节点 + 15），
  // 折叠数量按需动态计算；只折开头连续 completed，非 completed（pending/active/
  // stopped/skipped）不折叠；≤16 步不折叠
  let foldAll = 0
  while (foldAll < steps.length && steps[foldAll].status === 'completed') foldAll++
  const needFold = Math.max(0, steps.length - (MAX_DOTS - 1))
  const foldCount = Math.min(needFold, foldAll)
  const folded = steps.length > MAX_DOTS && foldCount > 0
  const renderSteps = folded ? steps.slice(foldCount) : steps

  const nodes: ReactNode[] = []
  if (folded) {
    // 折叠节点：绿点 + N+（纯静态，无点击交互）
    nodes.push(
        <div key="__fold__" className="pt-step" data-fold={foldCount}>
          <div className="pt-dot pt-dot-done"/>
          <div className="pt-label">{foldCount}+</div>
        </div>,
    )
    // 折叠段最后一步 completed → 折叠节点与后续之间 rail 为 done
    nodes.push(<div key="rail-fold" className="pt-rail pt-rail-done"/>)
  }
  renderSteps.forEach((step, i) => {
    const prev = renderSteps[i - 1]
    if (prev && !(folded && i === 0)) {
      const prevDone = mapStatus(prev.status) === 'done'
      nodes.push(<div key={`rail-${step.step_id}`} className={`pt-rail${prevDone ? ' pt-rail-done' : ''}`}/>)
    }
    const st = mapStatus(step.status)
    nodes.push(
        <div key={step.step_id} className="pt-step" data-step={step.step_id} onClick={() => onDotClick?.(step.step_id)}>
          <div className={ptDotClass(step.status, step.human_attention, pausedPendingId, step.step_id)}/>
          <div className={`pt-label${step.step_id === currentStepId || st === 'active' ? ' active' : ''}`}>{step.title}</div>
        </div>,
    )
  })

  return <div className="progress-track">{nodes}</div>
}

/** 2026-08-25（修订）：步骤条显示上限（含折叠节点）——超过才折叠，折叠后 ≤16 */
const MAX_DOTS = 16
