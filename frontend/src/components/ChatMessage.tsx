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
