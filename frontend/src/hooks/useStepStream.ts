// ═══════════════════════════════════════════════════════════════
// useStepStream（WP4-2 §3.11）
// 签名：useStepStream(taskId, stepId, handlers, matchStepId?)
// 行为：按 §3.4 处理 SSE（M10：事件按 matchStepId 过滤，默认 stepId 相等；
//       MonitorDetail 传 M4 双向匹配）；pendingEvents 缓冲与回放；组件卸载
//       关闭 EventSource
// 实现：handlers/matchStepId 经 ref + bridge 转发，父组件每次渲染内联创建
//       新回调也不会导致 SSE 重连或回调过期；effect 仅随 taskId/stepId 重建
// ═══════════════════════════════════════════════════════════════

import {useEffect, useRef} from 'react'
import {createStepStream, type StepStreamHandlers} from '../api/sse'

export function useStepStream(
    taskId: string,
    stepId: string,
    handlers: StepStreamHandlers,
    matchStepId?: (eventStepId: string) => boolean,
): void {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers
  const matchRef = useRef(matchStepId)
  matchRef.current = matchStepId

  useEffect(() => {
    // 转发桥：stream 生命周期内始终读取最新回调（避免回调过期）
    const bridge: StepStreamHandlers = {
      onStepStart: (e) => handlersRef.current.onStepStart?.(e),
      onChunk: (e) => handlersRef.current.onChunk?.(e),
      onStreamEnd: (e) => handlersRef.current.onStreamEnd?.(e),
      onToolStart: (e) => handlersRef.current.onToolStart?.(e),
      onToolParam: (e) => handlersRef.current.onToolParam?.(e),
      onToolExecuting: (e) => handlersRef.current.onToolExecuting?.(e),
      onToolResult: (e) => handlersRef.current.onToolResult?.(e),
      onUserMessage: (e) => handlersRef.current.onUserMessage?.(e),
      onThinkingChunk: (e) => handlersRef.current.onThinkingChunk?.(e),
      onLlmError: (e) => handlersRef.current.onLlmError?.(e),
      onRefresh: (e) => handlersRef.current.onRefresh?.(e),
      onFullRerender: (e) => handlersRef.current.onFullRerender?.(e),
      onRetry: (e, n) => handlersRef.current.onRetry?.(e, n),
      onRetryClear: () => handlersRef.current.onRetryClear?.(),
    }
    const matchArg = matchRef.current ? (id: string) => matchRef.current!(id) : undefined

    const stream = createStepStream(taskId, bridge, matchArg)
    stream.connect()
    stream.setActiveStep(stepId)
    return () => {
      stream.close()
    }
  }, [taskId, stepId])
}
