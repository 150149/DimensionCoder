import {type RefObject, useCallback, useEffect, useRef} from 'react'

const REPIN_FRAMES = 5

export function useChatAutoScroll(
    containerRef: RefObject<HTMLElement | null>,
    deps: unknown[],
    enabled: boolean,
    setShowJump: (v: boolean) => void,
) {
  const enabledRef = useRef(enabled)

  useEffect(() => {
    const el = containerRef.current
    enabledRef.current = enabled
    if (!el) return
    if (enabled) {
      el.scrollTop = el.scrollHeight
      setShowJump(false)
      let raf = 0
      let left = REPIN_FRAMES
      const step = () => {
        if (!enabledRef.current) return
        const el2 = containerRef.current
        if (el2) el2.scrollTop = el2.scrollHeight
        left -= 1
        if (left > 0) raf = requestAnimationFrame(step)
      }
      raf = requestAnimationFrame(step)
      return () => cancelAnimationFrame(raf)
    }
  }, [...deps, enabled]) // eslint-disable-line react-hooks/exhaustive-deps

  const jumpToBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [containerRef])

  return {jumpToBottom}
}
