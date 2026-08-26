import {useEffect, useRef} from 'react'

interface ErrorToastProps {
  message: string
  onClose: () => void
  variant?: 'error' | 'success'
}

export default function ErrorToast({message, onClose, variant = 'error'}: ErrorToastProps) {

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
