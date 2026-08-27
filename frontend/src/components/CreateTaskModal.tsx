// ═══════════════════════════════════════════════════════════════
// 创建任务配置弹窗（2026-08-26 用户需求：创建流程可选工作目录）：
// - 仅配置项（描述仍在侧边栏输入）；工作目录 = 自动选择（默认）或输入路径
//   （路径输入 + 目录浏览视图「选择文件夹」 + localStorage 历史记录下拉）
// - 结构可扩展：后续其他创建配置项加进同一弹窗
// ═══════════════════════════════════════════════════════════════

import {useEffect, useState} from 'react'
import {api} from '../api/client'

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
    // 存储满/禁用：静默（不影响创建）
  }
}

/** Windows 路径统一正斜杠（盘符/目录拼接稳定，后端 abspath 可处理） */
const norm = (p: string) => p.replace(/\\/g, '/')

interface CreateTaskModalProps {
  open: boolean
  onConfirm: (workspaceDir?: string) => void
  onCancel: () => void
}

export default function CreateTaskModal({open, onConfirm, onCancel}: CreateTaskModalProps) {
  const [mode, setMode] = useState<'auto' | 'custom'>('auto')
  const [path, setPath] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [browsing, setBrowsing] = useState(false)
  const [browsePath, setBrowsePath] = useState('')
  const [entries, setEntries] = useState<{ name: string; type: string }[]>([])
  const [busy, setBusy] = useState(false)

  // 打开时重置状态 + 加载历史
  useEffect(() => {
    if (open) {
      setMode('auto')
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
    // 盘符（C:）→ C:/；普通目录 → 当前路径拼接（转正斜杠）
    const base = norm(browsePath).replace(/\/+$/, '')
    const next = /^[A-Za-z]:$/.test(name) ? `${name}/` : `${base}/${name}`
    void loadBrowse(next)
  }

  const confirm = () => {
    const trimmed = path.trim()
    if (mode === 'custom' && trimmed) writeHistory(trimmed)
    onConfirm(mode === 'custom' && trimmed ? trimmed : undefined)
  }

  if (!open) return null
  return (
      <div className="modal-overlay show" onClick={onCancel}>
        <div className="modal" onClick={(e) => e.stopPropagation()}>
          <h3>创建任务</h3>
          <div className="field">
            <div className="field-label">工作目录</div>
            {!browsing ? (
                <>
                  <label className="ws-radio">
                    <input type="radio" name="ws-mode" checked={mode === 'auto'}
                           onChange={() => setMode('auto')}/>
                    自动选择（系统分配 workspace/&lt;任务ID&gt;/）
                  </label>
                  <label className="ws-radio">
                    <input type="radio" name="ws-mode" checked={mode === 'custom'}
                           onChange={() => setMode('custom')}/>
                    输入路径
                  </label>
                  {mode === 'custom' && (
                      <div className="ws-custom-row">
                        <input className="text-input ws-path-input"
                               placeholder="如 E:\code\my-project"
                               value={path}
                               onChange={(e) => setPath(e.target.value)}/>
                        <button className="btn" type="button" onClick={() => {
                          setBrowsing(true)
                          void loadBrowse('')
                        }}>
                          选择文件夹
                        </button>
                      </div>
                  )}
                  {mode === 'custom' && history.length > 0 && (
                      <select className="ws-history-select" value=""
                              onChange={(e) => {
                                if (e.target.value) setPath(e.target.value)
                              }}>
                        <option value="">最近使用…</option>
                        {history.map((h) => <option key={h} value={h}>{h}</option>)}
                      </select>
                  )}
                </>
            ) : (
                <div className="ws-browse">
                  <div className="ws-browse-path">
                    <button className="btn" type="button" disabled={!browsePath}
                            onClick={() => void loadBrowse('')}>
                      上一级
                    </button>
                    <span className="ws-browse-current">{browsePath || '（选择磁盘）'}</span>
                  </div>
                  <div className="ws-browse-list">
                    {entries.length === 0 && <div className="empty-state">（空目录）</div>}
                    {entries.map((e) => e.type === 'dir' ? (
                        <button key={e.name} className="ws-browse-dir" type="button"
                                onClick={() => enterDir(e.name)}>
                          {e.name}
                        </button>
                    ) : (
                        <div key={e.name} className="ws-browse-file">{e.name}</div>
                    ))}
                  </div>
                  <div className="modal-actions">
                    <button className="btn" type="button" onClick={() => setBrowsing(false)}>
                      取消
                    </button>
                    <button className="btn btn-primary" type="button"
                            disabled={!browsePath || busy}
                            onClick={() => {
                              setPath(norm(browsePath))
                              setBrowsing(false)
                            }}>
                      选择此目录
                    </button>
                  </div>
                </div>
            )}
          </div>
          <div className="modal-actions">
            <button className="btn" type="button" onClick={onCancel}>取消</button>
            <button className="btn btn-primary" type="button"
                    disabled={mode === 'custom' && !path.trim()}
                    onClick={confirm}>
              确认创建
            </button>
          </div>
        </div>
      </div>
  )
}
