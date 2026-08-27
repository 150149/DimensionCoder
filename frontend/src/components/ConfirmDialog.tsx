// ═══════════════════════════════════════════════════════════════
// ConfirmDialog（WP4-1 §3 组件清单：props {open, title, rows, onConfirm,
//   onCancel, confirmText?}）
// 确认对话框（sidebar.html modal 逐字类），三处复用：
// - Sidebar custom 创建确认（默认按钮文案「确认创建」）
// - FlowOverview 删除任务（confirmText="确认删除"）/ 强制介入（"确认介入"）
// ═══════════════════════════════════════════════════════════════

export interface ConfirmRow {
    step_id: string
    title: string
    required: boolean
    human_attention?: string
}

interface ConfirmDialogProps {
    open: boolean
    title: string
    rows: ConfirmRow[]
    onConfirm: () => void
    onCancel: () => void
    /** 确认按钮文案（默认「确认创建」，删除/介入场景各自传入） */
    confirmText?: string
    /** 可选说明文案（如重置流程的后果提示），渲染在标题下方 */
    description?: string
}

export default function ConfirmDialog({open, title, rows, onConfirm, onCancel, confirmText = '确认创建', description}: ConfirmDialogProps) {
    if (!open) return null
    return (
        <div className="modal-overlay show" onClick={onCancel}>
            <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h3>{title}</h3>
                {description && <div className="modal-desc">{description}</div>}
                {rows.length > 0 && (
                    <div className="task-list" style={{maxHeight: 260}}>
                        {rows.map((r) => (
                            <div key={r.step_id} className="task-card" style={{cursor: 'default'}}>
                                <div className="title">{r.title}</div>
                                <div className="meta">
                                    <span>{r.step_id}</span>
                                    <span>
                    {r.required ? '必做' : '可选'}
                                        {r.human_attention === 'gate' ? ' · Gate 待审批' : ''}
                  </span>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
                <div className="modal-actions">
                    <button onClick={onCancel}>取消</button>
                    <button className="primary" onClick={onConfirm}>
                        {confirmText}
                    </button>
                </div>
            </div>
        </div>
    )
}
