import {useState} from 'react'
import {Icon} from './icons'

interface InterveneBarProps {
    mode: 'step' | 'flow'
    onSend: (content: string) => void
    onForce: (content: string) => void
    onStop?: () => void
    onResume?: () => void

    onCompress?: () => void

    running?: boolean

    busy?: boolean

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
            {}
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
                    {}
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
                    {}
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
