import {type RefObject, useCallback, useEffect, useRef, useState} from 'react'

const JUMP_THRESHOLD = 100

const REPIN_FRAMES = 3

export function useChatAutoScroll(
    containerRef: RefObject<HTMLElement | null>,
    deps: unknown[],
    enabled: boolean,
) {
  const [showJump, setShowJump] = useState(false)
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
    setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_THRESHOLD)
  }, [...deps, enabled]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onScroll = () => {
      setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_THRESHOLD)
    }
    el.addEventListener('scroll', onScroll, {passive: true})
    onScroll()
    return () => el.removeEventListener('scroll', onScroll)
  }, [containerRef])

  const jumpToBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    setShowJump(false)
  }, [containerRef])

  return {jumpToBottom, showJump}
}
