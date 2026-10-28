import {memo} from 'react'
import type {DailyTokenItem} from '../api/types'

interface DailyTokenChartProps {
    items: DailyTokenItem[]
}

function fmt(n: number): string {
    return Math.max(0, Math.round(n)).toLocaleString()
}

function mmdd(date: string): string {
    return date.slice(5)
}

function cnDate(date: string): string {
    const [y, m, d] = date.split('-').map(Number)
    return y ? `${m}月${d}日` : date
}

export const DailyTokenChart = memo(function DailyTokenChart({items}: DailyTokenChartProps) {
    const totalSum = items.reduce((s, i) => s + i.total, 0)
    if (!items.length || totalSum <= 0) {
        return <div className="empty-state">暂无 token 记录</div>
    }
    const max = Math.max(...items.map((i) => i.total), 1)
    const peak = items.reduce((a, b) => (b.total > a.total ? b : a), items[0])
    const avg = Math.round(totalSum / items.length)
    const axisIdx = [...new Set([0, Math.floor((items.length - 1) / 2), items.length - 1])]

    return (
        <div className="dtc-wrap">
            <div className="dtc-summary">
                近 {items.length} 天合计 <b className="dtc-strong">{fmt(totalSum)}</b>
                {' '}· 日均 <b className="dtc-strong">{fmt(avg)}</b>
                {' '}· 峰值 <b className="dtc-strong">{cnDate(peak.date)}</b> {fmt(peak.total)}
            </div>
            <div className="dtc-legend" data-testid="dtc-legend">
                <span className="dtc-legend-item"><i className="dtc-swatch dtc-swatch-input"/>未缓存输入</span>
                <span className="dtc-legend-item"><i className="dtc-swatch dtc-swatch-cached"/>缓存输入</span>
                <span className="dtc-legend-item"><i className="dtc-swatch dtc-swatch-output"/>输出</span>
            </div>
            <div className="dtc-chart">
                <div className="dtc-bars" role="img"
                     aria-label={`每日 token 消耗堆叠条形图，共 ${items.length} 天`}>
                    {items.map((i) => {
                        const uncached = Math.max(0, i.prompt - i.cached)
                        const cachePct = i.prompt > 0 ? (i.cached / i.prompt) * 100 : 0
                        const heightPct = (i.total / max) * 100
                        const seg = (n: number) => (i.total > 0 ? (n / i.total) * 100 : 0)
                        return (
                            <div key={i.date} className="dtc-col">
                                <div className="dtc-bar" data-testid="dtc-bar"
                                     data-date={i.date} data-total={i.total}
                                     style={{height: `${heightPct}%`}}
                                     title={`${i.date}｜未缓存输入 ${fmt(uncached)} · 缓存输入 ${fmt(i.cached)}`
                                         + ` (${cachePct.toFixed(1)}%) · 输出 ${fmt(i.completion)}`
                                         + ` · 合计 ${fmt(i.total)}`}>
                                    <div className="dtc-seg dtc-seg-output" style={{height: `${seg(i.completion)}%`}}/>
                                    <div className="dtc-seg dtc-seg-cached" style={{height: `${seg(i.cached)}%`}}/>
                                    <div className="dtc-seg dtc-seg-input" style={{height: `${seg(uncached)}%`}}/>
                                </div>
                            </div>
                        )
                    })}
                </div>
                <div className="dtc-axis">
                    {axisIdx.map((idx) => (
                        <span key={items[idx].date} className="dtc-tick">{mmdd(items[idx].date)}</span>
                    ))}
                </div>
            </div>
        </div>
    )
})
