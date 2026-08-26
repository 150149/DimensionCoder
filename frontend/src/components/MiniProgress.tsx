interface MiniProgressProps {
    steps: { status: string; human_attention?: string; step_id?: string }[]

    pausedPendingId?: string
}

function segStatus(status: string, humanAttention?: string, pausedPendingId?: string, stepId?: string): string {
    if (status === 'active' && humanAttention === 'gate') return 'gate'
    if (status === 'completed') return 'done'
    if (status === 'active') return 'active'
    if (status === 'stopped') return 'stopped'

    if (status === 'pending' && pausedPendingId != null && stepId === pausedPendingId) return 'paused'
    return 'pending'
}

export default function MiniProgress({steps, pausedPendingId}: MiniProgressProps) {

    let foldAll = 0
    while (foldAll < steps.length && steps[foldAll].status === 'completed') foldAll++
    const needFold = Math.max(0, steps.length - (MAX_DOTS - 1))
    const foldCount = Math.min(needFold, foldAll)
    const folded = steps.length > MAX_DOTS && foldCount > 0
    const renderSteps = folded ? steps.slice(foldCount) : steps
    return (
        <div className="mini-bar">
            {folded && (
                <>
                    <div className="mini-seg seg-done" title={`${foldCount}+`}/>
                    <span className="mini-fold-badge">+{foldCount}</span>
                </>
            )}
            {renderSteps.map((s, i) => (
                <div key={folded ? i + 1 : i}
                     className={`mini-seg seg-${segStatus(s.status, s.human_attention, pausedPendingId, s.step_id)}`}/>
            ))}
        </div>
    )
}

const MAX_DOTS = 16
