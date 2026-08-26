import type {ReactNode} from 'react'

export interface ProgressStep {
  step_id: string
  title: string
  status: string

  human_attention?: string
}

interface ProgressRailProps {
  steps: ProgressStep[]
  currentStepId?: string
  onDotClick?: (stepId: string) => void

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

  if (status === 'active' && humanAttention === 'gate') return 'pt-dot pt-dot-gate'
  const st = mapStatus(status)
  if (st === 'done') return 'pt-dot pt-dot-done'
  if (st === 'active') return 'pt-dot pt-dot-active'
  if (st === 'skipped') return 'pt-dot pt-dot-skipped'
  if (st === 'stopped') return 'pt-dot pt-dot-stopped'

  if (st === 'pending' && pausedPendingId != null && stepId === pausedPendingId) return 'pt-dot pt-dot-paused'
  return 'pt-dot pt-dot-pending'
}

export default function ProgressRail({steps, currentStepId, onDotClick, pausedPendingId}: ProgressRailProps) {

  let foldAll = 0
  while (foldAll < steps.length && steps[foldAll].status === 'completed') foldAll++
  const needFold = Math.max(0, steps.length - (MAX_DOTS - 1))
  const foldCount = Math.min(needFold, foldAll)
  const folded = steps.length > MAX_DOTS && foldCount > 0
  const renderSteps = folded ? steps.slice(foldCount) : steps

  const nodes: ReactNode[] = []
  if (folded) {

    nodes.push(
        <div key="__fold__" className="pt-step" data-fold={foldCount}>
          <div className="pt-dot pt-dot-done"/>
          <div className="pt-label">{foldCount}+</div>
        </div>,
    )

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

const MAX_DOTS = 16
