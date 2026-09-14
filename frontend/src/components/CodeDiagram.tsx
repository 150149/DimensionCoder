import {useCallback, useEffect, useRef, useState} from 'react'
import {api} from '../api/client'
import {Icon} from './icons'

function extractMermaidCode(content: string): string | null {
  const blocks: string[] = []
  for (const m of content.matchAll(/```(?:mermaid)?[^\n]*\n([\s\S]*?)```/g)) {
    if (m[1]?.trim()) blocks.push(m[1].trim())
  }
  return blocks.length ? blocks.join('\n') : null
}

let mermaidReady = false

async function ensureMermaid(): Promise<void> {
  if (mermaidReady) return
  const {default: mermaid} = await import('mermaid')
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    flowchart: {htmlLabels: false},
  })
  mermaidReady = true
}

let renderSeq = 0

function normalizeSvg(svgStr: string): string {
  const vb = /viewBox="0 0 ([\d.]+) ([\d.]+)"/.exec(svgStr)
  let out = svgStr.replace(/max-width:\s*[\d.]+px;?\s*/g, '')
  if (vb) out = out.replace(/width="100%"/, `width="${vb[1]}px"`)
  return out
}

const SCALE_MIN = 0.2
const SCALE_MAX = 10
const SCALE_STEP = 1.1

interface ViewState {
  x: number
  y: number
  scale: number
}

const IDENTITY: ViewState = {x: 0, y: 0, scale: 1}
const clampScale = (s: number) => Math.min(SCALE_MAX, Math.max(SCALE_MIN, s))

export default function CodeDiagram({taskId}: { taskId: string }) {
  const [svg, setSvg] = useState('')
  const [state, setState] = useState<'idle' | 'ready' | 'error'>('idle')
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<ViewState>(IDENTITY)
  const dragRef = useRef<{ px: number; py: number; vx: number; vy: number } | null>(null)

  useEffect(() => {
    let alive = true
    let hasCode = false
    setSvg('')
    setState('idle')
    ;(async () => {
      try {
        const r = await api.getCodeDiagram(taskId)
        const code = extractMermaidCode(r.content ?? '')
        if (!alive || !code) return
        hasCode = true
        await ensureMermaid()
        const mermaid = (await import('mermaid')).default
        const {svg: rendered} = await mermaid.render(`dc-diagram-${++renderSeq}`, code)
        if (!alive) return
        setSvg(normalizeSvg(rendered))
        setState('ready')
      } catch {
        if (alive && hasCode) setState('error')
      }
    })()
    return () => {
      alive = false
    }
  }, [taskId])

  const close = useCallback(() => {
    setOpen(false)
    setView(IDENTITY)
    dragRef.current = null
  }, [])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close])

  const zoomBy = useCallback((factor: number) => {
    setView((v) => ({...v, scale: clampScale(v.scale * factor)}))
  }, [])

  const onWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    zoomBy(e.deltaY < 0 ? SCALE_STEP : 1 / SCALE_STEP)
  }, [zoomBy])

  const onDragStart = useCallback((px: number, py: number) => {
    dragRef.current = {px, py, vx: view.x, vy: view.y}
  }, [view.x, view.y])

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    onDragStart(e.clientX, e.clientY)
  }, [onDragStart])

  const onTouchStart = useCallback((e: React.TouchEvent) => {
    const t = e.touches[0]
    if (t) onDragStart(t.clientX, t.clientY)
  }, [onDragStart])

  const onDragMove = useCallback((px: number, py: number) => {
    const d = dragRef.current
    if (!d) return
    setView((v) => ({...v, x: d.vx + (px - d.px), y: d.vy + (py - d.py)}))
  }, [])

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    onDragMove(e.clientX, e.clientY)
  }, [onDragMove])

  const onTouchMove = useCallback((e: React.TouchEvent) => {
    const t = e.touches[0]
    if (t) onDragMove(t.clientX, t.clientY)
  }, [onDragMove])

  const endDrag = useCallback(() => {
    dragRef.current = null
  }, [])

  if (state === 'idle') return null
  if (state === 'error') {
    return (
        <div className="diagram-card">
          <div className="diagram-error">
            代码流程图渲染失败（图内容可能尚未生成合法 Mermaid 语法），可稍后刷新重试
          </div>
        </div>
    )
  }

  return (
      <>
        <div className="diagram-card diagram-thumb" onClick={() => setOpen(true)}
             title="点击放大查看（滚轮缩放、拖拽移动）">
          <div className="diagram-head">
            <Icon name="compass" size={13} gap={6}/>
            代码流程图（全局路线权威图）
            <span className="diagram-expand-hint">点击放大</span>
          </div>
          <div className="diagram-svg" dangerouslySetInnerHTML={{__html: svg}}/>
        </div>
        {open && (
            <div className="diagram-lightbox" onClick={close}>
              <div className="lightbox-toolbar" onClick={(e) => e.stopPropagation()}>
                <span className="lb-title">
                  <Icon name="compass" size={13} gap={6}/>
                  代码流程图
                </span>
                <span className="lb-actions">
                  <button className="lb-btn lb-zoom-out" onClick={() => zoomBy(1 / SCALE_STEP)}
                          title="缩小"><Icon name="compress" size={14} gap={0}/></button>
                  <span className="lb-scale">{Math.round(view.scale * 100)}%</span>
                  <button className="lb-btn lb-zoom-in" onClick={() => zoomBy(SCALE_STEP)}
                          title="放大"><Icon name="search" size={14} gap={0}/></button>
                  <button className="lb-btn lb-reset" onClick={() => setView(IDENTITY)}
                          title="重置视图"><Icon name="refresh" size={14} gap={0}/></button>
                  <button className="lb-btn lb-close" onClick={close} title="关闭 (Esc)">
                    <Icon name="close" size={14} gap={0}/>
                  </button>
                </span>
              </div>
              <div className="diagram-canvas" onClick={close}
                   onWheel={onWheel}
                   onMouseDown={onMouseDown}
                   onMouseMove={onMouseMove}
                   onMouseUp={endDrag}
                   onMouseLeave={endDrag}
                   onTouchStart={onTouchStart}
                   onTouchMove={onTouchMove}
                   onTouchEnd={endDrag}>
                <div className="diagram-canvas-inner"
                     style={{transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`}}
                     onClick={(e) => e.stopPropagation()}
                     dangerouslySetInnerHTML={{__html: svg}}/>
              </div>
            </div>
        )}
      </>
  )
}
