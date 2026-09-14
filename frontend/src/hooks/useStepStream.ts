import {useEffect, useRef} from 'react'
import {createStepStream, type StepStreamHandlers} from '../api/sse'

export function useStepStream(
    taskId: string,
    stepId: string,
    handlers: StepStreamHandlers,
    matchStepId?: (eventStepId: string) => boolean,
    getLastMsgSeq?: () => number,
): void {
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers
  const matchRef = useRef(matchStepId)
  matchRef.current = matchStepId
  const getLastMsgSeqRef = useRef(getLastMsgSeq)
  getLastMsgSeqRef.current = getLastMsgSeq

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
      onRetryProgress: (e) => handlersRef.current.onRetryProgress?.(e),
      onRetryClear: () => handlersRef.current.onRetryClear?.(),
      onRoundAborted: (e) => handlersRef.current.onRoundAborted?.(e),
      onMessagesConfirmed: (e) => handlersRef.current.onMessagesConfirmed?.(e),
      onTaskUpdated: (e) => handlersRef.current.onTaskUpdated?.(e),
      onStepStatus: (e) => handlersRef.current.onStepStatus?.(e),
      onStepTokensUpdated: (e) => handlersRef.current.onStepTokensUpdated?.(e),
      onStepStatsUpdated: (e) => handlersRef.current.onStepStatsUpdated?.(e),
      onGateApprovalReady: (e) => handlersRef.current.onGateApprovalReady?.(e),
      onMessagesReset: (e) => handlersRef.current.onMessagesReset?.(e),
      onReconnect: () => handlersRef.current.onReconnect?.(),
    }
    const matchArg = matchRef.current ? (id: string) => matchRef.current!(id) : undefined

    const stream = createStepStream(taskId, bridge, matchArg)
    const getLms = getLastMsgSeqRef.current
    stream.connect(undefined, getLms ? {stepId, getLastMsgSeq: getLms} : undefined)
    stream.setActiveStep(stepId)
    return () => {
      stream.close()
    }
  }, [taskId, stepId])
}
