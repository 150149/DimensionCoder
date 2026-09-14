import {lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState} from 'react'
import {api} from '../api/client'
import useMediaQuery from '../hooks/useMediaQuery'
import FileTree from './FileTree'
import FileBrowser from './FileBrowser'

const CodeEditor = lazy(() => import('./CodeEditor'))

interface CodeTabProps {
  taskId: string
  markedPaths?: string[]
  externalModifiedPaths?: string[]
  onDirtyChange?: (dirty: boolean) => void
  onExternalModified?: (path: string, modified: boolean) => void
}

function fuzzyScore(pattern: string, text: string): number | null {
  const p = pattern.toLowerCase()
  const t = text.toLowerCase()
  let ti = 0
  let score = 0
  for (const ch of p) {
    const idx = t.indexOf(ch, ti)
    if (idx < 0) return null
    score += idx - ti
    ti = idx + 1
  }
  return score
}

const QUICK_OPEN_MAX = 30

export default function CodeTab({
                                  taskId, markedPaths = [], externalModifiedPaths = [],
                                  onDirtyChange, onExternalModified
                                }: CodeTabProps) {
  const mobile = useMediaQuery('(max-width: 768px)')
  const [path, setPath] = useState<string | null>(null)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [workspace, setWorkspace] = useState('')
  const [quickOpen, setQuickOpen] = useState(false)
  const [qoQuery, setQoQuery] = useState('')
  const [qoEntries, setQoEntries] = useState<{ name: string; type: string }[] | null>(null)
  const qoInputRef = useRef<HTMLInputElement | null>(null)

  const openFile = useCallback((p: string) => {
    setPath(p)
    setMobileOpen(true)
  }, [])

  useEffect(() => {
    let alive = true
    api.fsTree('', {taskId})
        .then((tree) => {
          if (alive && tree.workspace) setWorkspace(tree.workspace)
        })
        .catch(() => {
        })
    return () => {
      alive = false
    }
  }, [taskId])

  useEffect(() => {
    if (mobile) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'p') {
        e.preventDefault()
        setQuickOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mobile])

  useEffect(() => {
    if (!quickOpen) return
    setQoQuery('')
    if (qoEntries === null) {
      api
          .fsTree('', {recursive: true, taskId})
          .then((tree) => {
            setQoEntries((tree.entries || []).filter((e) => e.type === 'file')
                .map((e) => ({name: e.name, type: e.type})))
          })
          .catch(() => {
            setQoEntries([])
          })
    }
    requestAnimationFrame(() => qoInputRef.current?.focus())
  }, [quickOpen, taskId, qoEntries])

  const qoResults = useMemo(() => {
    const q = qoQuery.trim()
    if (!q) return (qoEntries ?? []).slice(0, QUICK_OPEN_MAX).map((e) => e.name)
    const scored: { name: string; score: number }[] = []
    for (const e of qoEntries ?? []) {
      const s = fuzzyScore(q, e.name)
      if (s !== null) scored.push({name: e.name, score: s})
    }
    scored.sort((a, b) => a.score - b.score)
    return scored.slice(0, QUICK_OPEN_MAX).map((x) => x.name)
  }, [qoQuery, qoEntries])

  const closeQuickOpen = useCallback(() => setQuickOpen(false), [])

  return (
      <div className="code-tab">
        {mobile ? (
            path && mobileOpen ? (
                <Suspense fallback="加载编辑器...">
                  <CodeEditor
                      path={path}
                      taskId={taskId}
                      workspace={workspace}
                      mobile
                      onBack={() => setMobileOpen(false)}
                      onDirtyChange={onDirtyChange}
                      onExternalModified={onExternalModified}
                  />
                </Suspense>
            ) : (
                <FileBrowser
                    taskId={taskId}
                    onOpenFile={openFile}
                    markedPaths={markedPaths}
                    externalModifiedPaths={externalModifiedPaths}
                    onOpenQuickOpen={() => setQuickOpen(true)}
                />
            )
        ) : (
            <div className="flow-code-split">
              <FileTree
                  onOpenFile={setPath}
                  markedPaths={markedPaths}
                  externalModifiedPaths={externalModifiedPaths}
                  taskId={taskId}
                  activePath={path}
              />
              <Suspense fallback="加载编辑器...">
                <CodeEditor
                    path={path}
                    taskId={taskId}
                    workspace={workspace}
                    onDirtyChange={onDirtyChange}
                    onExternalModified={onExternalModified}
                />
              </Suspense>
            </div>
        )}
        {quickOpen && (
            <div className="quick-open" onClick={closeQuickOpen}>
              <div className="quick-open-panel" onClick={(e) => e.stopPropagation()}>
                <input
                    ref={qoInputRef}
                    className="qo-input"
                    placeholder="输入文件名快速打开..."
                    value={qoQuery}
                    onChange={(e) => setQoQuery(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') closeQuickOpen()
                      if (e.key === 'Enter' && qoResults.length > 0) {
                        openFile(qoResults[0])
                        closeQuickOpen()
                      }
                    }}
                />
                {qoEntries === null ? (
                    <div className="qo-empty">加载中...</div>
                ) : qoResults.length === 0 ? (
                    <div className="qo-empty">无匹配文件</div>
                ) : (
                    <div className="qo-list">
                      {qoResults.map((name) => (
                          <button
                              key={name}
                              className="qo-item"
                              onClick={() => {
                                openFile(name)
                                closeQuickOpen()
                              }}
                          >
                            {name}
                          </button>
                      ))}
                    </div>
                )}
              </div>
            </div>
        )}
      </div>
  )
}
