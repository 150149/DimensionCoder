// ═══════════════════════════════════════════════════════════════
// useChatAutoScroll — 聊天流自动滚动（显式开关版）
// - 开关语义（用户明确要求）：
//     开（enabled=true）  → 内容更新（deps 变化）时强制滚到底部（锁定跟随）
//     关（enabled=false） → 完全不干预滚动，用户自由上滑/下滑
// - 「运行中才跟随」由调用方控制：非运行步骤（已完成/暂停）传入 enabled=false，
//   自动滚动完全不生效——用户浏览历史不会被轮询刷新拉回
// ═══════════════════════════════════════════════════════════════

import {type RefObject, useCallback, useEffect, useState} from 'react'

// 「回到底部」显示阈值：距容器底部 100px 内视为接近底部，不显示按钮
const JUMP_THRESHOLD = 100

export function useChatAutoScroll(
    containerRef: RefObject<HTMLElement | null>,
    deps: unknown[],
    enabled: boolean,
) {
  // 距底部是否较远（>阈值）→ 显示「回到底部」悬浮按钮；接近底部隐藏
  const [showJump, setShowJump] = useState(false)

  // ① 开关开启：内容更新 → 滚到底（此时必在底部 → 不显示按钮）；
  // ② 开关关闭：内容更新时同步刷新「是否远离底部」（流式内容增长不触发 scroll 事件）
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    if (enabled) {
      el.scrollTop = el.scrollHeight
      setShowJump(false)
    } else {
      setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_THRESHOLD)
    }
  }, [...deps, enabled]) // eslint-disable-line react-hooks/exhaustive-deps

  // 滚动位置跟踪：滚离底部超过阈值 → 显示按钮；接近底部 → 隐藏
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onScroll = () => {
      setShowJump(el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_THRESHOLD)
    }
    el.addEventListener('scroll', onScroll, {passive: true})
    onScroll() // 初始化
    return () => el.removeEventListener('scroll', onScroll)
  }, [containerRef])

  // 「回到底部」悬浮按钮：显式点击（无论开关状态）→ 立即滚到底部并隐藏按钮
  const jumpToBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    setShowJump(false)
  }, [containerRef])

  return {jumpToBottom, showJump}
}
