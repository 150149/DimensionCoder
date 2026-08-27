// ═══════════════════════════════════════════════════════════════
// useTaskPolling（WP4-2 §3.11）
// 签名：useTaskPolling(intervalMs, fetcher, deps?)
// 行为：定时器 + 组件卸载清理 + 依赖变化重置；fetch 失败静默（不弹错，
//       下一轮重试）
// ═══════════════════════════════════════════════════════════════

import {useEffect, useRef} from 'react'

export function useTaskPolling(
    intervalMs: number,
    fetcher: () => Promise<unknown>,
    deps?: unknown[],
): void {
  // fetcher 用 ref 保持最新引用，避免 effect 因回调重建而重置定时器
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const depsKey = JSON.stringify(deps ?? [])
  const depsKeyRef = useRef(depsKey)
  const depsChanged = depsKeyRef.current !== depsKey

  useEffect(() => {
    depsKeyRef.current = depsKey

    const tick = () => {
      // fetch 失败静默（不弹错，下一轮重试）
      fetcherRef.current().catch(() => {
        /* 静默 */
      })
    }
    tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, depsKey, depsChanged])
}
