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

    confirmText?: string

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
