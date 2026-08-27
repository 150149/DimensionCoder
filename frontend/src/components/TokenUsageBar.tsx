// ═══════════════════════════════════════════════════════════════
// TokenUsageBar（Token 展示）——两个纯展示组件，零业务依赖
// - ContextMeter：上条「上下文占用」（当前上下文长度 / 窗口总容量 + 细进度条 + 百分比）
//   → 渲染于输入框上方；prompt = 最近一次请求的输入 tokens（context_tokens，
//   覆盖写非累加），窗口总容量 = 模型真实上下文窗口（默认 1M，非压缩触发线）
// - TokenDetails：下条「token 明细」（未缓存输入 / 缓存输入+占比 / 累计输出）
//   → 渲染于输入框下方
// 参照 Cursor/Claude Code 惯例：11px 小字号、tabular-nums 等宽数字、
// 4px 细进度条；缓存命中=省钱语义用绿色
// ═══════════════════════════════════════════════════════════════

import {memo, ReactNode, useEffect, useState} from 'react'

interface ContextMeterProps {
  /** 模型上下文窗口总容量（config contextWindow，默认 1M） */
  windowSize: number
  /** 当前上下文长度（最近一次请求的输入 tokens，非累计） */
  prompt: number
}

interface TokenMetricsProps {
  startedAt: number | null
  endedAt: number | null
  active: boolean
  activeSince: number | null
  runDurationMs: number
  outputDurationMs: number
  ttftMs: number | null
  requestCount: number
  completion: number
  /** 2026-08-22：进行中 LLM 轮请求开始时间（reqStartedAt，null 则无进行中轮）——
   *  输出时长实时补差：输出速度在 API 请求进行中即可见（1s tick 更新），
   *  streamEnd 结算后 outputDurationMs 定格、roundFirstAt 归 null
   *  2026-08-24（输出速度纯吐字口径）：改为进行中轮首字时间（firstTokenAt）——
   *  首字前等待（TTFT/限流）不计入实时分母，只补纯吐字 */
  roundFirstAt?: number | null
  /** 2026-08-22：进行中轮已输出估算 token（前端字符近似）——与累计 completion
   *  相加作分子，请求中速度分子实时；streamEnd 后并入 metrics.completion、归 0 */
  roundComp?: number
}

function formatDuration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds))
  return `${Math.floor(safe / 60)}分${String(safe % 60).padStart(2, '0')}秒`
}

/** 运行统计：未运行/无数据统一短横线占位（tus-dim），避免长文本撑爆行宽 */
export const TokenMetrics = memo(function TokenMetrics({
                                                         startedAt,
                                                         endedAt,
                                                         active,
                                                         activeSince,
                                                         runDurationMs,
                                                         outputDurationMs,
                                                         ttftMs,
                                                         requestCount,
                                                         completion,
                                                         roundFirstAt,
                                                         roundComp
                                                       }: TokenMetricsProps) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active || endedAt != null) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active, endedAt])
  const hasRun = startedAt != null || runDurationMs > 0
  // 运行时长：runDurationMs（最后 streamEnd 定格值）+ active 时实时增量
  const elapsed = (runDurationMs + (active && endedAt == null ? now - (activeSince ?? now) : 0)) / 1000
  // 输出速度：累计输出 / 输出时长（纯吐字口径 2026-08-24：首字→当前/最后 chunk，
  // 排除首字等待 TTFT/限流/工具等待——此前 API 调用开始→流结束，工具轮多的步骤
  // 每轮 TTFT 累积导致速度虚低；首字延迟另有独立指标）
  // 2026-08-22：请求进行中实时——分母含进行中轮已吐字时长（roundFirstAt 补差）、
  // 分子含进行中轮估算 token（roundComp：思考/文本/工具参数吐字均算，与后端
  // 估算口径一致；精度由 streamEnd 结算 + DB 校准兜底）
  const liveOut = outputDurationMs + (roundFirstAt != null ? now - roundFirstAt : 0)
  const totalComp = completion + (roundComp ?? 0)
  const speed = liveOut > 0 && totalComp > 0 ? totalComp / (liveOut / 1000) : null
  const dash = <span className="tus-dim">--</span>
  return <span className="tus-metrics" aria-label="步骤运行统计">
    <span className="tus-metric">输出速度 <b className="tus-val">{speed == null ? dash : `${speed.toFixed(1)} token/s`}</b></span>
    <span className="tus-metric-sep">|</span>
    <span className="tus-metric">首字延迟 <b className="tus-val">{ttftMs == null ? dash : `${(ttftMs / 1000).toFixed(3)}秒`}</b></span>
    <span className="tus-metric-sep">|</span>
    <span className="tus-metric">运行时长 <b className="tus-val">{hasRun ? formatDuration(elapsed) : dash}</b></span>
    <span className="tus-metric-sep">|</span>
    <span className="tus-metric">请求数 <b className="tus-val">{requestCount}</b></span>
  </span>
})

interface TokenDetailsProps {
  /** 累计输入 token（总输入，含缓存命中部分；未缓存输入 = prompt - cached） */
  prompt: number
  /** 累计缓存命中输入 token */
  cached: number
  /** 累计输出 token */
  completion: number
  /** 2026-08-23：总消耗金额（上层按步骤 model_tier 选价格组算好传入；
   *  不显示单位——设置口径与展示一致；undefined 则显示 --） */
  cost?: number
  /** 行尾附加内容（2026-08-21：运行统计栏，前置 | 分隔；不传则不渲染） */
  trailing?: ReactNode
}

/** 消耗金额格式化：<1 → 4 位小数去尾零；≥1 → 2 位；0 → 0.0000；无数据 → -- */
function fmtCost(cost: number | undefined): string {
  if (cost == null || !Number.isFinite(cost)) return '--'
  if (cost === 0) return '0.0000'
  return cost < 1 ? String(Number(cost.toFixed(4))) : cost.toFixed(2)
}

/** 大数格式化（整数版，上条用）：≥1M → 1.2M；≥1K → 128K；否则原样 */
function fmtInt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`
  return String(n)
}

/** 大数格式化（1 位小数版，下条用）：≥1M → 1.2M；≥1K → 128.4K；否则原样 */
function fmt1(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

/** 占用进度条三档色：<80% 紫（accent）；80-95% 橙；>95% 红 */
function meterColor(ratio: number): string {
  if (ratio > 0.95) return '#ef4444'
  if (ratio >= 0.8) return '#f59e0b'
  return 'var(--accent)'
}

/** 上条：上下文窗口占用（输入框上方） */
export const ContextMeter = memo(function ContextMeter({windowSize, prompt}: ContextMeterProps) {
  const hasData = prompt > 0
  const ratio = hasData ? Math.min(prompt / windowSize, 1) : 0
  const pct = Math.round(ratio * 100)
  const title = hasData
      ? `上下文 ${prompt.toLocaleString()} / ${windowSize.toLocaleString()} tokens（已用/总容量）`
      : `上下文窗口总容量 ${windowSize.toLocaleString()} tokens`

  return (
      <div className="ctx-row" title={title}>
        <span className="tus-label">上下文</span>
        <span className="tus-num">
        {hasData ? `${fmtInt(prompt)} / ${fmtInt(windowSize)}` : `-- / ${fmtInt(windowSize)}`}
      </span>
        <span className="ctx-meter" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <span className="ctx-meter-fill" style={{width: `${pct}%`, background: meterColor(ratio)}}/>
      </span>
        <span className="tus-num">{pct}%</span>
      </div>
  )
})

/** 下条：token 明细（输入框下方）；trailing 统计栏同行渲染（| 分隔） */
export const TokenDetails = memo(function TokenDetails({prompt, cached, completion, cost, trailing}: TokenDetailsProps) {
  const hasData = prompt > 0
  // 2026-08-24：未缓存输入 = 总输入 - 缓存命中（金额按未缓存输入计价，展示口径一致）
  const uncached = Math.max(0, prompt - cached)
  const cachePct = hasData ? (cached / prompt) * 100 : 0
  const title = hasData
      ? `未缓存输入 ${uncached.toLocaleString()} · 缓存输入 ${cached.toLocaleString()} (${cachePct.toFixed(1)}%) · 输出 ${completion.toLocaleString()} · 消耗金额 ${fmtCost(cost)}`
      : undefined

  return (
      <div className="tus-row" title={title}>
        {hasData ? (
            <>
          <span className="tus-item">
            未缓存输入 <b className="tus-val">{fmt1(uncached)}</b>
          </span>
              <span className="tus-sep">·</span>
              <span className="tus-item">
            缓存输入 <b className="tus-val tus-cached">{fmt1(cached)}</b>{' '}
                <span className="tus-cache-pct">({cachePct.toFixed(0)}%)</span>
          </span>
              <span className="tus-sep">·</span>
              <span className="tus-item">
            输出 <b className="tus-val">{fmt1(completion)}</b>
          </span>
              {/* 2026-08-23：输出 token 后追加总消耗金额（无单位——设置与展示口径一致） */}
              <span className="tus-sep">·</span>
              <span className="tus-item">
            消耗金额 <b className="tus-val">{fmtCost(cost)}</b>
          </span>
            </>
        ) : (
            <>
          <span className="tus-item">
            未缓存输入 <b className="tus-val tus-dim">--</b>
          </span>
              <span className="tus-sep">·</span>
              <span className="tus-item">
            缓存输入 <b className="tus-val tus-dim">--</b>
          </span>
              <span className="tus-sep">·</span>
              <span className="tus-item">
            输出 <b className="tus-val tus-dim">--</b>
          </span>
              <span className="tus-sep">·</span>
              <span className="tus-item">
            消耗金额 <b className="tus-val tus-dim">--</b>
          </span>
            </>
        )}
        {trailing != null && (
            <>
              <span className="tus-metric-sep">|</span>
              {trailing}
            </>
        )}
      </div>
  )
})
