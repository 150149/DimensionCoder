// ═══════════════════════════════════════════════════════════════
// ChatMessage（WP4-1 §3 组件清单：props {msg, onToggleTool?, expandedTools?}）
// 单条消息渲染（按 role 分派 5 类，WP4-3 §3.7）：
//   system → .msg-system 可折叠（折叠头部「系统提示 (N 字)」）
//   user   → .msg-user / .user-bubble（文本转义后渲染，禁止注入 HTML）
//   assistant → .ai-block（ReactMarkdown + remark-gfm，代码块浅灰底等宽）
//   tool   → .ai-tool-inline .tool-panel（委托 ToolCard，callId = tool_call_id ?? seq；
//   .ai-tool-inline 包装与 SSE live 卡一致——内联工具卡无边框无圆角，旧版 stepDetail/monitorDetail 同款）
//   thinking → .think-box 灰色斜体（保留性渲染，D8）
// Markdown 渲染只用 react-markdown + remark-gfm（D3 修订）
// ═══════════════════════════════════════════════════════════════

import {memo, useState} from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type {Message} from '../api/types'
import ToolCard from './ToolCard'
import {Icon} from './icons'

interface ChatMessageProps {
    msg: Message
    onToggleTool?: (callId: string) => void
    expandedTools?: Set<string>
}

/**
 * 流式性能：消息列表可能数千条（step-5b 实况 4600+），每次 streamChunk 触发
 * 父组件重渲染时，未 memo 的 ChatMessage 会全量重执行（含 ReactMarkdown 解析）→
 * UI 输出速度被拖到"一个字一个字"。memo 后 props（msg 引用 / useCallback 回调 /
 * Set 引用）在流式期间稳定 → 只重渲染流式块本身。
 */
export default memo(function ChatMessage({msg, onToggleTool, expandedTools}: ChatMessageProps) {
    switch (msg.role) {
        case 'system':
            return <SystemMessage content={msg.content}/>
        case 'user':
            return (
                <div className="msg-user">
                    <div className="user-bubble">{msg.content}</div>
                </div>
            )
        case 'assistant':
            return (
                <div className="ai-block">
                    <div className="ai-text">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content || ''}</ReactMarkdown>
                    </div>
                </div>
            )
        case 'tool': {
            const callId = msg.tool_call_id ?? (msg.seq != null ? `seq-${msg.seq}` : '')
            const expanded = expandedTools ? expandedTools.has(callId) : undefined
            return (
                <div className="ai-tool-inline">
                    <ToolCard
                        callId={callId}
                        toolName={msg.toolName ?? msg.tool_name ?? ''}
                        input={msg.input}
                        output={msg.output}
                        status="done"
                        defaultExpanded={expanded}
                        onToggle={onToggleTool ? () => onToggleTool(callId) : undefined}
                    />
                </div>
            )
        }
        case 'thinking':
            return (
                <div className="ai-thinking-inline">
                    <div className="think-box">
                        <div className="think-body" style={{display: 'block'}}>
                            <div className="ai-text">{msg.content}</div>
                        </div>
                    </div>
                </div>
            )
        default:
            return (
                <div className="ai-block">
                    <div className="ai-text">{msg.content}</div>
                </div>
            )
    }
})

function SystemMessage({content}: { content: string }) {
    // 默认收起：系统提示词是不变的规则（Task 8 分离结构），需要时再展开查看
    const [collapsed, setCollapsed] = useState(true)
    return (
        <div className={`msg-system${collapsed ? ' collapsed' : ''}`}>
            <div className="sys-header" onClick={() => setCollapsed((v) => !v)}>
        <span className="arrow">
          <Icon name="chevronDown" size={11} gap={0}/>
        </span>
                <span>系统提示 ({content.length} 字)</span>
            </div>
            <div className="sys-body">{content}</div>
        </div>
    )
}
