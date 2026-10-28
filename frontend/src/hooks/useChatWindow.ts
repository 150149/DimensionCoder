import {type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState,} from 'react'

const NEAR_TOP = 300
const NEAR_TOP_RESET = NEAR_TOP * 2

export const JUMP_THRESHOLD = 100

export interface UseChatWindowOptions {
  scrollRef: RefObject<HTMLElement | null>
  count: number
  oldestKey?: string | number
  newestKey?: string | number
  resetKey?: string | number
  onReachTop?: () => void
  truncated?: boolean
  cap?: number
  step?: number
}

export function useChatWindow({
                                scrollRef,
                                count,
                                oldestKey,
                                newestKey,
                                resetKey,
                                onReachTop,
                                truncated,
                                cap = 800,
                                step = 400,
                              }: UseChatWindowOptions) {
  const [visible, setVisible] = useState(cap)
  const from = Math.max(0, count - visible)
  const atHead = from === 0 && !truncated

  const [showJump, setShowJump] = useState(false)
  const showJumpRef = useRef(false)

  const expand = useCallback(() => setVisible((v) => v + step), [step])

  const metrics = useRef({h: 0, top: 0, from, oldestKey, newestKey, resetKey})
  const firedRef = useRef(false)
  const stateRef = useRef({from, onReachTop, step})
  stateRef.current = {from, onReachTop, step}

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const top = el.scrollTop
    metrics.current.top = top

    if (top < NEAR_TOP) {
      if (!firedRef.current) {
        firedRef.current = true
        const s = stateRef.current
        if (s.from > 0) setVisible((v) => v + s.step)
        else s.onReachTop?.()
      }
    } else {
      if (top > NEAR_TOP_RESET) firedRef.current = false
    }

    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    const shouldShow = dist > JUMP_THRESHOLD
    if (shouldShow !== showJumpRef.current) {
      showJumpRef.current = shouldShow
      setShowJump(shouldShow)
    }
  }, [scrollRef])

  const attachedRef = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    const el = scrollRef.current ?? null
    if (el === attachedRef.current) return
    attachedRef.current?.removeEventListener('scroll', onScroll)
    attachedRef.current = el
    el?.addEventListener('scroll', onScroll, {passive: true})
  })
  useEffect(() => () => {
    attachedRef.current?.removeEventListener('scroll', onScroll)
    attachedRef.current = null
  }, [onScroll])

  const [prevLoaded, setPrevLoaded] = useState({count, oldestKey, newestKey, resetKey})
  if (prevLoaded.resetKey !== resetKey) {
    setPrevLoaded({count, oldestKey, newestKey, resetKey})
    setVisible(cap)
  } else if (
      prevLoaded.count !== count || prevLoaded.oldestKey !== oldestKey ||
      prevLoaded.newestKey !== newestKey
  ) {
    const grew = count - prevLoaded.count
    const headPrepend =
        grew > 0 && prevLoaded.oldestKey !== oldestKey && prevLoaded.newestKey === newestKey
    setPrevLoaded({count, oldestKey, newestKey, resetKey})
    if (headPrepend) setVisible((v) => v + grew)
  }

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const p = metrics.current
    const replaced = resetKey !== p.resetKey
    const headPrepend = oldestKey !== p.oldestKey && newestKey === p.newestKey
    const headGrew = !replaced && (from < p.from || headPrepend)
    const nh = el.scrollHeight
    if (headGrew && nh > p.h && p.top < NEAR_TOP) {
      el.scrollTop = p.top + (nh - p.h)
      if (el.scrollTop > NEAR_TOP_RESET) firedRef.current = false
    }
    metrics.current = {h: el.scrollHeight, top: el.scrollTop, from, oldestKey, newestKey, resetKey}
  })

  const jumpToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    showJumpRef.current = false
    setShowJump(false)
  }, [scrollRef])

  return {from, atHead, expand, showJump, setShowJump, jumpToBottom}
}
