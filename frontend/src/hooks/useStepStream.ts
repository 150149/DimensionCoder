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
