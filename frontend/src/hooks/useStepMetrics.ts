// ═══════════════════════════════════════════════════════════════
// useStepMetrics（2026-08-22 提取重构，StepDetail/MonitorDetail 共用）
// 运行统计（输出速度/首字延迟/运行时长/请求数）实时化：
// - SSE 事件驱动实时采样/结算：stepStart 重置时序点、chunk/thinkingChunk/
//   toolParam 采样 TTFT 与进行中轮估算 token、streamEnd 结算（时长/请求数）
// - 后端校准：轮询拿到 DB stats（tiktoken 精确、每轮落库）后 mergeBackend
//   逐字段 max 合并（累计值单调）——SSE 事件丢失/断连时后端补齐
// completion 口径：由 mergeBackend 提供（DB 权威）——前端估算（chars/2 近似
// cl100k）仅作请求进行中实时分子（roundComp，用户要求：思考/文本/工具参数
// 吐字均算输出速度），不并入累计（避免与 DB 值双计）；浏览器无 tiktoken，
// 精确值以 DB 校准为准
// ═══════════════════════════════════════════════════════════════

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
  /** 已结算轮输出 token 累计（前端估算，DB 校准 max 合并） */
  completion: number
}

/** mergeBackend 可接受的数据源（StepDef / StepStats 结构兼容，字段全可选） */
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
  // 当前 LLM 流时序点：stepStart 建、streamEnd 结算（每轮 LLM 流一个边界，
  // 工具轮之间也有 streamEnd）；outputDurationMs 累计「API 调用开始 → 流结束」
  // 总耗时（含首字等待 TTFT——卡顿后一次性吐字时按"首字→结束"会虚高，
  // 用户反馈 2026-08-21 改为 API 总耗时）；requests = streamEnd 次数（= 实际
  // LLM 请求数）；TTFT 采样：reqStartedAt（第一轮=stepStart；工具轮后=工具
  // 结果到达，近似请求发出）
  const metricRequestRef = useRef<RoundTiming | null>(null)
  // 进行中 LLM 轮已输出估算 token（文本/思考/工具参数逐片 accrueRoundComp
  // 累积，ref 不触发渲染；渲染时由 TokenMetrics 1s tick 读最新）
  const roundCompRef = useRef(0)

  // 首个输出 chunk（文本或思考）采样 TTFT——thinking 模型工具轮无文本，
  // 思考首块也算模型开始输出（与后端 reasoning 计时一致，否则纯思考+工具
  // 轮实时统计全 --）
  const sampleFirstChunk = useCallback(() => {
    const req = metricRequestRef.current
    if (!req) return
    const now = Date.now()
    if (req.firstTokenAt == null) {
      // 首字：TTFT 采样（仅第一轮精确——reqStartedAt=stepStart）
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

  // 进行中轮估算 token 累积：Math.ceil(chars/2) 近似 cl100k——后端 tiktoken
  // 精确、前端浏览器无 tiktoken，近似即可；streamEnd 后 DB 校准 max 兜底。
  // 思考/文本/工具参数吐字均计入（用户要求：思考吐字、工具参数吐字算输出速度）
  const accrueRoundComp = useCallback((chars: number) => {
    roundCompRef.current += Math.ceil(chars / 2)
  }, [])

  // stepStart：重置时序点 + startedAt/activeSince（不重置累计值——重跑=新
  // 执行周期，但累计值属步骤生命周期，仅校准起点）+ roundCompRef 清零
  const resetStep = useCallback((at: number) => {
    metricRequestRef.current = {stepStartedAt: at, reqStartedAt: at, firstTokenAt: null, lastChunkAt: null}
    roundCompRef.current = 0
    setMetrics((m) => ({...m, startedAt: m.startedAt ?? at, endedAt: null, activeSince: at}))
  }, [])

  // streamEnd：结算本轮——快照 ref（2026-08-21 修复：setMetrics updater 是
  // 异步执行的（React 批处理），若在 updater 内读 ref，此时 ref 已被下方同步
  // 清空 → 首字/时长丢失（outputDurationMs 恒 0、速度显示 --）——先取快照再结算
  const endRound = useCallback((at: number) => {
    const req = metricRequestRef.current
    const firstAt = req?.firstTokenAt ?? null
    const lastAt = req?.lastChunkAt ?? null
    const stepStartAt = req?.stepStartedAt ?? at
    setMetrics((m) => {
      if (req == null) return {...m, endedAt: at}
      // 2026-08-24（用户反馈：输出速度虚低）：本轮输出时长改纯吐字 = 最后 chunk -
      // 首字——排除首字等待 TTFT/限流等待（此前 API 调用开始→流结束，工具轮多的
      // 步骤每轮 TTFT 累积导致速度比预期慢很多）；首字延迟已有独立指标（TTFT）
      const out = firstAt != null && lastAt != null
          ? m.outputDurationMs + (lastAt - firstAt)
          : m.outputDurationMs
      // 运行时长定格：本轮 streamEnd - 步骤 start（active 时 TokenMetrics 实时补差）
      return {
        ...m,
        endedAt: at,
        activeSince: null,
        runDurationMs: at - stepStartAt,
        outputDurationMs: out,
        requests: m.requests + 1, // 每轮 LLM 流结束 = 一次大模型请求
        // completion 不在此累计：由 mergeBackend 校准（DB tiktoken 精确）提供
        // ——前端估算仅作请求进行中实时分子（roundComp），并入累计会与 DB
        // 值双计（合并后再结算 = 同一批轮次算两次，2026-08-22）
      }
    })
    // ref 保留（工具轮后还有下一轮 LLM 流），清掉本轮时序点——下一轮
    // 首字不再采样 TTFT（无 stepStart，reqStartedAt=null）
    if (req) {
      req.reqStartedAt = null
      req.firstTokenAt = null
      req.lastChunkAt = null
    }
    roundCompRef.current = 0
  }, [])

  // llmError/aborted 路径后端不发 streamEnd → 仅定格（悬空计时修复：非
  // active 步骤不再因缺少 endedAt 而显示无限增长的时间）；不结算输出时长/
  // 请求数（请求失败未完成）
  const settleStep = useCallback((at: number) => {
    const req = metricRequestRef.current
    const stepStartAt = req?.stepStartedAt ?? at
    setMetrics((m) => ({...m, endedAt: at, activeSince: null, runDurationMs: at - stepStartAt}))
  }, [])

  // 后端校准：DB stats（每轮 LLM 流结束落库）逐字段 max 合并——累计值单调，
  // SSE 事件丢失/断连时后端补齐；ttftMs 用合并后累计重算
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
