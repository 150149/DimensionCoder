// ═══════════════════════════════════════════════════════════════
// InterveneBar（WP4-1 §3 组件清单：props {mode:"step"|"flow", onSend,
//   onForce, onStop?, onResume?, onCompress?, busy}）
// 底部介入栏（4 个旧 HTML 逐字类：.send-btn/.force-btn/.stop-btn，B7）：
// - 交互状态机（v2 参照主流 AI 产品：ChatGPT 发送/停止同位置切换、
//   WPS 排队发送）：
//     空闲       → 主按钮=发送（send，排队注入）
//     busy+空输入 → 主按钮=终止（stop，打断）
//     busy+有输入 → 主按钮=强制发送（force，强插）+ 输入框上方待发送区
// - 待发送区 .pending-msg：展示待发文本摘要 + [排队]（send 语义）+ [×] 取消
// - 按钮图标化（title 保留文字提示）；压缩/恢复/打断保持既有类名
// - busy 时按钮禁用（流式进行中，WP4-3 §3.7 状态机细节）
// ═══════════════════════════════════════════════════════════════

import {useState} from 'react'
import {Icon} from './icons'

interface InterveneBarProps {
    mode: 'step' | 'flow'
    onSend: (content: string) => void
    onForce: (content: string) => void
    onStop?: () => void
    onResume?: () => void
    /** step 模式压缩对话（v2：从 StepDetail 右上角移入介入栏统一管理） */
    onCompress?: () => void
    /** 正在运行信号（step 模式=streaming；flow 模式=task active）；缺省回退 busy */
    running?: boolean
    /** 提交进行中（防重入，按钮禁用） */
    busy?: boolean
    /** 自动滚动开关（step 模式）：开=锁定跟随底部，关=完全自由滚动 */
    autoScroll?: boolean
    onToggleAutoScroll?: () => void
}

export default function InterveneBar({
                                         mode,
                                         onSend,
                                         onForce,
                                         onStop,
                                         onResume,
                                         onCompress,
                                         running,
                                         busy = false,
                                         autoScroll,
                                         onToggleAutoScroll,
                                     }: InterveneBarProps) {
    const [text, setText] = useState('')
    const hasText = text.trim().length > 0
    const isRunning = running ?? busy

    const submitSend = () => {
        const t = text.trim()
        if (!t) return
        onSend(t)
        setText('')
    }

    const submitForce = () => {
        const t = text.trim() || '强制介入调整'
        onForce(t)
        setText('')
    }

    // 主按钮三态（参照 ChatGPT/WPS 交互）：空闲=发送；运行中+空=终止；运行中+有输入=强制
    // 2026-08-20：运行中但未提供 onStop（页面不承载终止语义）→ 空闲态发送
    const primary =
        !isRunning || !hasText ? (isRunning && onStop ? 'stop' : 'send') : 'force'
    const primaryTitle =
        primary === 'stop'
            ? '终止当前输出'
            : primary === 'force'
                ? mode === 'step'
                    ? '立即中断+注入+继续（强制发送）'
                    : '立即打断+Monitor调整+继续（强制介入）'
                : mode === 'step'
                    ? '排队注入本步骤'
                    : '排队等流程完成后Monitor调整'

    return (
        <div className="intervene-bar">
            {/* 待发送区：运行中且有输入时显示（排队入口 + 取消） */}
            {isRunning && hasText && (
                <div className="pending-msg">
                    <span className="pending-text">待发送：{text.trim().slice(0, 40)}{text.trim().length > 40 ? '…' : ''}</span>
                    <button className="queue-btn" onClick={submitSend} title="排队发送（当前输出结束后注入）">
                        <Icon name="send" size={12} gap={0}/>
                        排队
                    </button>
                    <button className="pending-clear" onClick={() => setText('')} title="取消待发送">
                        <Icon name="close" size={12} gap={0}/>
                    </button>
                </div>
            )}
            <div className="intervene-row">
        <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={mode === 'step' ? '输入干预指令...' : '输入消息...'}
            rows={2}
        />
                <div className="btn-group">
                    {autoScroll !== undefined && onToggleAutoScroll && (
                        <button
                            className={`autoscroll-toggle${autoScroll ? ' on' : ''}`}
                            onClick={onToggleAutoScroll}
                            title={autoScroll ? '自动滚动：开（点击关闭，自由滚动）' : '自动滚动：关（点击开启，锁定跟随）'}
                        >
                            <Icon name={autoScroll ? 'lock' : 'unlock'} size={14} gap={0}/>
                        </button>
                    )}
                    {/* 2026-08-20：压缩按钮不再限 step 模式——Monitor 页（flow 模式）
            压缩当前 Monitor 对话也用（FlowOverview 未传 onCompress 不受影响） */}
                    {onCompress && (
                        <button className="compress-btn" onClick={onCompress} disabled={busy} title="压缩对话历史">
                            <Icon name="compress" size={14} gap={0}/>
                        </button>
                    )}
                    {onResume && (
                        <button className="resume-btn" onClick={onResume} disabled={busy} title="恢复执行">
                            <Icon name="play" size={14} gap={0}/>
                        </button>
                    )}
                    {/* 2026-08-20：终止按钮不再限 step 模式——Monitor 页（flow 模式）
            暂停当前 Monitor 输出也用它（FlowOverview 未传 onStop 不受影响） */}
                    {onStop && primary === 'stop' && (
                        <button className="stop-btn" onClick={onStop} disabled={busy} title="终止当前输出">
                            <Icon name="stop" size={14} gap={0}/>
                        </button>
                    )}
                    {primary === 'force' && (
                        <button className="force-btn" onClick={submitForce} disabled={busy} title={primaryTitle}>
                            <Icon name="zap" size={14} gap={0}/>
                        </button>
                    )}
                    {primary === 'send' && (
                        <button className="send-btn" onClick={submitSend} disabled={!hasText || busy} title={primaryTitle}>
                            <Icon name="send" size={14} gap={0}/>
                        </button>
                    )}
                </div>
            </div>
        </div>
    )
}
