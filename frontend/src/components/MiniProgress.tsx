// ═══════════════════════════════════════════════════════════════
// MiniProgress（WP4-1 §3 组件清单：props {steps: {status, human_attention}[]}）
// mini-seg 分段条（sidebar.html L20-25 逐字逻辑）：
//   segStatus：gate（active+gate 待审批）→gate, completed→done, active→active,
//   stopped→stopped（红），else pending
// 2026-08-23（用户需求）：gate 优先判定（此前仅看 status=active 落蓝色）；
// stopped 独立红色（此前误用 gate 橙色）
// ═══════════════════════════════════════════════════════════════

interface MiniProgressProps {
    steps: { status: string; human_attention?: string; step_id?: string }[]
    /** 2026-08-23（用户反馈）：任务暂停时下一个待执行的 pending 步骤 id——
     *  侧栏分段深灰标记「暂停中断点」（与总览卡片「暂停中」一致） */
    pausedPendingId?: string
}

function segStatus(status: string, humanAttention?: string, pausedPendingId?: string, stepId?: string): string {
    if (status === 'active' && humanAttention === 'gate') return 'gate'
    if (status === 'completed') return 'done'
    if (status === 'active') return 'active'
    if (status === 'stopped') return 'stopped'
    // 2026-08-23（用户反馈）：任务暂停 → 下一个待执行步骤深灰分段（暂停中断点可见）
    if (status === 'pending' && pausedPendingId != null && stepId === pausedPendingId) return 'paused'
    return 'pending'
}

export default function MiniProgress({steps, pausedPendingId}: MiniProgressProps) {
    // 2026-08-25（用户需求）：与 ProgressRail 同规则动态折叠——显示上限 16 段，
    // 只折开头连续 completed，合并为一段 seg-done（title=N+ hover 提示）；≤16 步不折
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

/** 2026-08-25（修订）：分段条显示上限（含合并段）——超过才折叠，折叠后 ≤16 */
const MAX_DOTS = 16
