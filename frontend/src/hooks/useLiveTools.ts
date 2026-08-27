// ═══════════════════════════════════════════════════════════════
// useLiveTools（WP4-2 §3.11）
// 签名：useLiveTools(): {tools: Map<string, ToolCardState>,
//        start(callId, name, input), result(callId, output),
//        markRendered(callId)}
// 行为：toolCallStart 插入 running 卡，toolCallResult 更新输出；
//       markRendered 标记已渲染 callId（轮询响应不覆盖已渲染卡，
//       `_liveContentRendered` 等价物，WP4-3 §3.7 第 4 条）
// ═══════════════════════════════════════════════════════════════

import {useCallback, useRef, useState} from 'react'
import type {ToolCardState} from '../api/types'

export interface LiveTools {
  tools: Map<string, ToolCardState>
  start: (callId: string, name: string, input: unknown) => void
  /** V-18：工具参数流式增量追加（input 为字符串时拼接；非字符串忽略） */
  appendParam: (callId: string, delta: string) => void
  result: (callId: string, output: string) => void
  markRendered: (callId: string) => void
}

export function useLiveTools(): LiveTools {
  const [tools, setTools] = useState<Map<string, ToolCardState>>(() => new Map())
  // 已渲染标记集合（`_liveContentRendered` 等价物）：轮询/全量重拉时据此
  // 不覆盖已渲染的工具卡；markRendered 由消费方在渲染后调用
  const renderedRef = useRef<Set<string>>(new Set())

  const start = useCallback((callId: string, name: string, input: unknown) => {
    setTools((prev) => {
      const next = new Map(prev)
      next.set(callId, {toolName: name, input, status: 'running'})
      return next
    })
  }, [])

  const result = useCallback((callId: string, output: string) => {
    setTools((prev) => {
      const cur = prev.get(callId)
      if (!cur) return prev
      const next = new Map(prev)
      next.set(callId, {...cur, output, status: 'done'})
      return next
    })
  }, [])

  const appendParam = useCallback((callId: string, delta: string) => {
    setTools((prev) => {
      const cur = prev.get(callId)
      if (!cur) return prev
      const next = new Map(prev)
      // 流式路径 input 初始为空串 → 逐片拼接；兜底路径 input 已是完整 JSON（字符串）
      // 且不再收到 param 事件，不会二次拼接
      const base = typeof cur.input === 'string' ? cur.input : ''
      next.set(callId, {...cur, input: base + delta})
      return next
    })
  }, [])

  const markRendered = useCallback((callId: string) => {
    renderedRef.current.add(callId)
  }, [])

  return {tools, start, appendParam, result, markRendered}
}
