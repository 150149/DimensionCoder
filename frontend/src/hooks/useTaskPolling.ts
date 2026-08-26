import {useEffect, useRef} from 'react'

export function useTaskPolling(
    intervalMs: number,
    fetcher: () => Promise<unknown>,
    deps?: unknown[],
): void {

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const depsKey = JSON.stringify(deps ?? [])
  const depsKeyRef = useRef(depsKey)
  const depsChanged = depsKeyRef.current !== depsKey

  useEffect(() => {
    depsKeyRef.current = depsKey

    const tick = () => {

      fetcherRef.current().catch(() => {

      })
    }
    tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => window.clearInterval(timer)

  }, [intervalMs, depsKey, depsChanged])
}
