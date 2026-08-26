import {type RefObject, useCallback, useEffect, useState} from 'react'

const JUMP_THRESHOLD = 100

export function useChatAutoScroll(
    containerRef: RefObject<HTMLElement | null>,
    deps: unknown[],
    enabled: boolean,
) {

  const [showJump, setShowJump] = useState(false)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (enabled) {
      el.scrollTop = el.scrollHeight
      setShowJump(false)
    } else {
      setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_THRESHOLD)
    }
  }, [...deps, enabled])

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
