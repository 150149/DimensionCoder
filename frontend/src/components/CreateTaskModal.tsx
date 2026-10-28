import {useEffect, useState} from 'react'
import {api} from '../api/client'
import {Icon} from './icons'

const HISTORY_KEY = 'dc_custom_workspaces'
const HISTORY_MAX = 10

function readHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

function writeHistory(path: string): void {
  const next = [path, ...readHistory().filter((x) => x !== path)].slice(0, HISTORY_MAX)
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(next))
  } catch {
  }
}

const norm = (p: string) => p.replace(/\\/g, '/')

function parentOf(p: string): string {
  const t = norm(p).replace(/\/+$/, '')
  if (/^[A-Za-z]:$/.test(t)) return ''
  const i = t.lastIndexOf('/')
  if (i < 0) return ''
  const up = t.slice(0, i)
  return /^[A-Za-z]:$/.test(up) ? `${up}/` : up
}

interface CreateTaskModalProps {
  open: boolean
  onConfirm: (workspaceDir?: string, taskMode?: 'flow' | 'single') => void
  onCancel: () => void
}

export default function CreateTaskModal({open, onConfirm, onCancel}: CreateTaskModalProps) {
  const [mode, setMode] = useState<'auto' | 'custom'>('auto')
  const [taskMode, setTaskMode] = useState<'flow' | 'single'>('flow')
  const [path, setPath] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [browsing, setBrowsing] = useState(false)
  const [browsePath, setBrowsePath] = useState('')
  const [entries, setEntries] = useState<{ name: string; type: string }[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) {
      setMode('auto')
      setTaskMode('flow')
      setPath('')
      setHistory(readHistory())
      setBrowsing(false)
      setBrowsePath('')
    }
  }, [open])

  const loadBrowse = async (p: string) => {
    setBusy(true)
    try {
      const data = await api.fsBrowse(p)
      setBrowsePath(data.path)
      setEntries(data.entries || [])
    } catch {
      setEntries([])
    } finally {
      setBusy(false)
    }
  }

  const enterDir = (name: string) => {
    const base = norm(browsePath).replace(/\/+$/, '')
    const next = /^[A-Za-z]:$/.test(name) ? `${name}/` : `${base}/${name}`
    void loadBrowse(next)
  }

  const confirm = () => {
    const trimmed = path.trim()
    if (mode === 'custom' && trimmed) writeHistory(trimmed)
    onConfirm(mode === 'custom' && trimmed ? trimmed : undefined, taskMode)
  }

  if (!open) return null

  const segs: { label: string; to: string }[] = []
  if (browsePath) {
    const parts = norm(browsePath).replace(/\/+$/, '').split('/').filter(Boolean)
    let acc = ''
    parts.forEach((label, i) => {
      acc = i === 0 ? `${label}/` : `${acc.replace(/\/+$/, '')}/${label}`
      segs.push({label, to: acc})
    })
  }

  const dirs = entries.filter((e) => e.type === 'dir')

  if (browsing) {
    return (
        <div className="modal-overlay show" onClick={onCancel}>
          <div className="modal modal-browse" onClick={(e) => e.stopPropagation()}>
            <h3>
              <button className="btn btn-icon" type="button" title="返回"
                      onClick={() => setBrowsing(false)}>
                <Icon name="arrowLeft" size={13} gap={0}/>
              </button>
              选择工作目录
            </h3>
            <div className="ws-crumb-bar">
              <button className="btn btn-icon" type="button" title="上一级"
                      disabled={!browsePath || busy}
                      onClick={() => void loadBrowse(parentOf(browsePath))}>
                <Icon name="arrowUp" size={13} gap={0}/>
              </button>
              <div className="ws-crumb">
                <button className={`ws-crumb-seg${!browsePath ? ' current' : ''}`} type="button"
                        onClick={() => void loadBrowse('')}>
                  磁盘
                </button>
                {segs.map((s) => (
                    <span key={s.to} className="ws-crumb-sep">
                      /
                      <button className={`ws-crumb-seg${s.to === browsePath ? ' current' : ''}`}
                              type="button" onClick={() => void loadBrowse(s.to)}>
                        {s.label}
                      </button>
                    </span>
                ))}
              </div>
            </div>
            <div className="ws-dir-list">
              {busy && <div className="empty-state">加载中…</div>}
              {!busy && dirs.length === 0 && <div className="empty-state">（无子目录）</div>}
              {dirs.map((e) => (
                  <button key={e.name} className="ws-dir-item" type="button"
                          onClick={() => enterDir(e.name)}>
                    <Icon name="folder" size={14} gap={8}/>
                    {e.name}
                  </button>
              ))}
            </div>
            <div className="modal-actions">
              <button type="button" onClick={() => setBrowsing(false)}>
                返回
              </button>
              <button className="primary" type="button"
                      disabled={!browsePath || busy}
                      onClick={() => {
                        setPath(norm(browsePath))
                        setBrowsing(false)
                      }}>
                选择此目录
              </button>
            </div>
          </div>
        </div>
    )
  }

  return (
      <div className="modal-overlay show" onClick={onCancel}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <h3>创建任务</h3>
          <div className="field">
            <div className="field-label">流程类型</div>
            <div className="opt-cards" role="radiogroup" aria-label="流程类型">
              <button type="button" role="radio" aria-checked={taskMode === 'flow'}
                      className={`opt-card${taskMode === 'flow' ? ' selected' : ''}`}
                      onClick={() => setTaskMode('flow')}>
                <span className="opt-card-title">多步骤流程</span>
                <span className="opt-card-desc">AI 编排步骤执行，适合复杂任务</span>
              </button>
              <button type="button" role="radio" aria-checked={taskMode === 'single'}
                      className={`opt-card${taskMode === 'single' ? ' selected' : ''}`}
                      onClick={() => setTaskMode('single')}>
                <span className="opt-card-title">单步骤对话</span>
                <span className="opt-card-desc">直接对话干活，可随时升级为流程</span>
              </button>
            </div>
          </div>
          <div className="field">
            <div className="field-label">工作目录</div>
            <div className="opt-cards" role="radiogroup" aria-label="工作目录">
              <button type="button" role="radio" aria-checked={mode === 'auto'}
                      className={`opt-card${mode === 'auto' ? ' selected' : ''}`}
                      onClick={() => setMode('auto')}>
                <span className="opt-card-title">自动分配</span>
                <span className="opt-card-desc">系统创建 workspace/&lt;任务ID&gt;/ 目录</span>
              </button>
              <button type="button" role="radio" aria-checked={mode === 'custom'}
                      className={`opt-card${mode === 'custom' ? ' selected' : ''}`}
                      onClick={() => setMode('custom')}>
                <span className="opt-card-title">自定义目录</span>
                <span className="opt-card-desc">浏览或输入本机已有项目路径</span>
              </button>
            </div>
            {mode === 'custom' && (
                <>
                  <div className="ws-path-row">
                    <input className="text-input ws-path-input"
                           placeholder="如 E:\code\my-project"
                           value={path}
                           onChange={(e) => setPath(e.target.value)}/>
                    <button className="btn" type="button" onClick={() => {
                      setBrowsing(true)
                      void loadBrowse(path.trim() ? norm(path.trim()).replace(/\/+$/, '') : '')
                    }}>
                      <Icon name="folder" size={13} gap={5}/>
                      浏览…
                    </button>
                  </div>
                  {history.length > 0 && (
                      <div className="ws-recent">
                        <span className="ws-recent-label">最近使用</span>
                        {history.map((h) => (
                            <button key={h} className={`ws-chip${path === h ? ' selected' : ''}`}
                                    type="button" title={h}
                                    onClick={() => setPath(h)}>
                              {h}
                            </button>
                        ))}
                      </div>
                  )}
                </>
            )}
          </div>
          <div className="modal-actions">
            <button type="button" onClick={onCancel}>取消</button>
            <button className="primary" type="button"
                    disabled={mode === 'custom' && !path.trim()}
                    onClick={confirm}>
              确认创建
            </button>
          </div>
        </div>
      </div>
  )
}
