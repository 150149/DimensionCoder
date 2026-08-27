// ═══════════════════════════════════════════════════════════════
// ErrorToast（WP4-1 §3 组件清单 / P2-16/E2 新增）
// props: {message, onClose, variant?:"error"|"success"}
// 全局错误提示（API 非关键错误统一走它；llmError 仍是步骤内错误卡，
// 互不替代）。CSS 固定（E2）：.error-toast（position:fixed; top:12px;
// left:50%; transform:translateX(-50%); background:#dc2626（success 变体
// #16a34a）; color:#fff; padding:8px 16px; border-radius:8px; font-size:13px;
// z-index:1000; box-shadow:0 2px 8px rgba(0,0,0,.2)）；3s 自动消失。
// ═══════════════════════════════════════════════════════════════

import {useEffect, useRef} from 'react'

interface ErrorToastProps {
  message: string
  onClose: () => void
  variant?: 'error' | 'success'
}

export default function ErrorToast({message, onClose, variant = 'error'}: ErrorToastProps) {
  // 父组件高频重渲染（1s 轮询）会让内联 onClose 引用变化——若 timer 依赖 onClose，
  // 会被反复重置导致永不触发（toast 不消失）。用 ref 持有最新回调：timer 只随
  // message 启动（新消息重新计时），父组件重渲染不影响倒计时。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  useEffect(() => {
    const timer = window.setTimeout(() => onCloseRef.current(), 3000)
    return () => window.clearTimeout(timer)
  }, [message])

  return (
      <div className={`error-toast${variant === 'success' ? ' success' : ''}`} onClick={onClose}>
        {message}
      </div>
  )
}
