import {useCallback, useRef, useState} from 'react'
import type {ToolCardState} from '../api/types'

export interface LiveTools {
  tools: Map<string, ToolCardState>
  start: (callId: string, name: string, input: unknown) => void

  appendParam: (callId: string, delta: string) => void
  result: (callId: string, output: string) => void
  markRendered: (callId: string) => void
}

export function useLiveTools(): LiveTools {
  const [tools, setTools] = useState<Map<string, ToolCardState>>(() => new Map())

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
