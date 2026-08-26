import {useCallback, useRef, useState} from 'react'

export interface StepMetricsState {
  startedAt: number | null
  endedAt: number | null
  activeSince: number | null
  runDurationMs: number
  outputDurationMs: number
  ttftMs: number | null
  ttftTotalMs: number
  ttftSamples: number
  requests: number

  completion: number
}

export interface StepStatsLike {
  requests?: number | null
  ttft_total_ms?: number | null
  ttft_samples?: number | null
  output_duration_ms?: number | null
  run_duration_ms?: number | null
  token_completion?: number | null
}

interface RoundTiming {
  stepStartedAt: number
  reqStartedAt: number | null
  firstTokenAt: number | null
  lastChunkAt: number | null
}

const EMPTY: StepMetricsState = {
  startedAt: null,
  endedAt: null,
  activeSince: null,
  runDurationMs: 0,
  outputDurationMs: 0,
  ttftMs: null,
  ttftTotalMs: 0,
  ttftSamples: 0,
  requests: 0,
  completion: 0,
}

export function useStepMetrics() {
  const [metrics, setMetrics] = useState<StepMetricsState>(EMPTY)

  const metricRequestRef = useRef<RoundTiming | null>(null)

  const roundCompRef = useRef(0)

  const sampleFirstChunk = useCallback(() => {
    const req = metricRequestRef.current
    if (!req) return
    const now = Date.now()
    if (req.firstTokenAt == null) {

      req.firstTokenAt = now
      const reqStartedAt = req.reqStartedAt
      if (reqStartedAt != null) {
        setMetrics((m) => ({
          ...m,
          ttftMs: (m.ttftTotalMs + now - reqStartedAt) / (m.ttftSamples + 1),
          ttftTotalMs: m.ttftTotalMs + now - reqStartedAt,
          ttftSamples: m.ttftSamples + 1,
        }))
      }
    }
    req.lastChunkAt = now
  }, [])

  const accrueRoundComp = useCallback((chars: number) => {
    roundCompRef.current += Math.ceil(chars / 2)
  }, [])

  const resetStep = useCallback((at: number) => {
    metricRequestRef.current = {stepStartedAt: at, reqStartedAt: at, firstTokenAt: null, lastChunkAt: null}
    roundCompRef.current = 0
    setMetrics((m) => ({...m, startedAt: m.startedAt ?? at, endedAt: null, activeSince: at}))
  }, [])

  const endRound = useCallback((at: number) => {
    const req = metricRequestRef.current
    const firstAt = req?.firstTokenAt ?? null
    const lastAt = req?.lastChunkAt ?? null
    const stepStartAt = req?.stepStartedAt ?? at
    setMetrics((m) => {
      if (req == null) return {...m, endedAt: at}

      const out = firstAt != null && lastAt != null
          ? m.outputDurationMs + (lastAt - firstAt)
          : m.outputDurationMs

      return {
        ...m,
        endedAt: at,
        activeSince: null,
        runDurationMs: at - stepStartAt,
        outputDurationMs: out,
        requests: m.requests + 1,

      }
    })

    if (req) {
      req.reqStartedAt = null
      req.firstTokenAt = null
      req.lastChunkAt = null
    }
    roundCompRef.current = 0
  }, [])

  const settleStep = useCallback((at: number) => {
    const req = metricRequestRef.current
    const stepStartAt = req?.stepStartedAt ?? at
    setMetrics((m) => ({...m, endedAt: at, activeSince: null, runDurationMs: at - stepStartAt}))
  }, [])

  const mergeBackend = useCallback((st: StepStatsLike | null | undefined) => {
    if (!st) return
    const requests = st.requests ?? 0
    if (requests <= 0) return
    setMetrics((m) => {
      const ttftTotalMs = Math.max(m.ttftTotalMs, st.ttft_total_ms ?? 0)
      const ttftSamples = Math.max(m.ttftSamples, st.ttft_samples ?? 0)
      return {
        ...m,
        requests: Math.max(m.requests, requests),
        outputDurationMs: Math.max(m.outputDurationMs, st.output_duration_ms ?? 0),
        runDurationMs: Math.max(m.runDurationMs, st.run_duration_ms ?? 0),
        completion: Math.max(m.completion, st.token_completion ?? 0),
        ttftTotalMs,
        ttftSamples,
        ttftMs: ttftSamples > 0 ? ttftTotalMs / ttftSamples : m.ttftMs,
      }
    })
  }, [])

  return {
    metrics, metricRequestRef, roundCompRef, sampleFirstChunk, accrueRoundComp,
    resetStep, endRound, settleStep, mergeBackend
  }
}
