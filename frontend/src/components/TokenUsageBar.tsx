import {memo, ReactNode, useEffect, useState} from 'react'

interface ContextMeterProps {

    windowSize: number

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

    roundFirstAt?: number | null

    roundComp?: number
}

function formatDuration(seconds: number): string {
    const safe = Math.max(0, Math.floor(seconds))
    return `${Math.floor(safe / 60)}分${String(safe % 60).padStart(2, '0')}秒`
}

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

    const elapsed = (runDurationMs + (active && endedAt == null ? now - (activeSince ?? now) : 0)) / 1000

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

    prompt: number

    cached: number

    completion: number

    cost?: number

    trailing?: ReactNode
}

function fmtCost(cost: number | undefined): string {
    if (cost == null || !Number.isFinite(cost)) return '--'
    if (cost === 0) return '0.0000'
    return cost < 1 ? String(Number(cost.toFixed(4))) : cost.toFixed(2)
}

function fmtInt(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${Math.round(n / 1_000)}K`
    return String(n)
}

function fmt1(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
}

function meterColor(ratio: number): string {
    if (ratio > 0.95) return '#ef4444'
    if (ratio >= 0.8) return '#f59e0b'
    return 'var(--accent)'
}

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

export const TokenDetails = memo(function TokenDetails({prompt, cached, completion, cost, trailing}: TokenDetailsProps) {
    const hasData = prompt > 0

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
                    {}
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
