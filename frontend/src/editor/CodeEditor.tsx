import './monacoSetup'
import {useCallback, useEffect, useRef, useState} from 'react'
import Editor from '@monaco-editor/react'
import type {editor as MonacoEditor} from 'monaco-editor'
import {api, ApiError} from '../api/client'
import type {FsFile} from '../api/types'
import ErrorToast from '../components/ErrorToast'
import {Icon} from '../components/icons'
import {monaco} from './monacoSetup'

interface CodeEditorProps {

  path: string | null

  onDirtyChange?: (dirty: boolean) => void

  onExternalModified?: (path: string, modified: boolean) => void
}

const EXT_LANG: Record<string, string> = {
  ts: 'typescript',
  tsx: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  py: 'python',
  json: 'json',
  md: 'markdown',
  html: 'html',
  css: 'css',
}

function langFor(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return EXT_LANG[ext] ?? 'plaintext'
}

const POLL_MS = 5000

export default function CodeEditor({path, onDirtyChange, onExternalModified}: CodeEditorProps) {

  const [file, setFile] = useState<FsFile | null>(null)

  const [draft, setDraft] = useState('')
  const [fileError, setFileError] = useState('')
  const [toast, setToast] = useState<{ message: string; variant: 'error' | 'success' } | null>(null)

  const [conflict, setConflict] = useState(false)
  const baseMtimeRef = useRef<number | null>(null)
  const pathRef = useRef<string | null>(null)
  const draftRef = useRef('')
  const saveRef = useRef<() => void>(() => {
  })
  draftRef.current = draft

  const reload = useCallback(() => {
    const p = pathRef.current
    if (!p) return
    setFileError('')
    api
        .fsRead(p)
        .then((f) => {
          setFile(f)
          setDraft(f.content)
          baseMtimeRef.current = f.mtime
        })
        .catch((err) => {
          setFile(null)
          setDraft('')
          baseMtimeRef.current = null
          const message = err instanceof Error ? err.message : '文件读取失败'
          setFileError(message)

          setToast({message, variant: 'error'})
        })
  }, [])

  useEffect(() => {
    pathRef.current = path
    setConflict(false)
    setToast(null)
    if (!path) {
      setFile(null)
      setDraft('')
      setFileError('')
      baseMtimeRef.current = null
      return
    }
    reload()
  }, [path, reload])

  const dirty = file !== null && draft !== file.content
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  const save = useCallback(async () => {
    const p = pathRef.current
    if (!p) return
    try {
      await api.fsWrite(p, draftRef.current)
      setFile((f) => (f ? {...f, content: draftRef.current} : f))
      onExternalModified?.(p, false)
      setToast({message: '已保存', variant: 'success'})

      try {
        const fresh = await api.fsRead(p)
        baseMtimeRef.current = fresh.mtime
      } catch {

      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(true)
      } else {
        setToast({message: err instanceof Error ? err.message : '保存失败', variant: 'error'})
      }
    }
  }, [onExternalModified])

  saveRef.current = save

  const onMount = useCallback((ed: MonacoEditor.IStandaloneCodeEditor) => {
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
  }, [])

  useEffect(() => {
    if (!path) return
    const timer = window.setInterval(() => {
      api
          .fsRead(path)
          .then((f) => {
            if (baseMtimeRef.current !== null && f.mtime !== baseMtimeRef.current) {
              onExternalModified?.(path, true)
            }
          })
          .catch(() => {

          })
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [path, onExternalModified])

  const handleCopyMine = async () => {
    const p = pathRef.current
    if (!p) return
    try {
      await navigator.clipboard?.writeText(draftRef.current)
    } catch {

    }
    setConflict(false)
    reload()
  }

  const handleForceOverwrite = async () => {
    const p = pathRef.current
    if (!p) return
    setConflict(false)
    try {
      const fresh = await api.fsRead(p)
      await api.fsWrite(p, draftRef.current)
      baseMtimeRef.current = fresh.mtime
      setFile((f) => (f ? {...f, content: draftRef.current} : f))
      onExternalModified?.(p, false)
      setToast({message: '已保存', variant: 'success'})
    } catch (err) {
      setToast({message: err instanceof Error ? err.message : '保存失败', variant: 'error'})
    }
  }

  const handleDiscard = () => {
    setConflict(false)
    reload()
  }

  if (!path) {
    return (
        <div className="code-placeholder">选择文件以查看</div>
    )
  }

  return (
      <div className="code-wrap">
        <div className="code-toolbar">
        <span className="file-icon">
          <Icon name="file" size={13} gap={0}/>
        </span>
          {path}
        </div>
        <div className="code-body">
          {fileError ? (
              <div className="code-status error">{fileError}</div>
          ) : file === null ? (
              <div className="code-status muted">加载中...</div>
          ) : (
              <Editor
                  language={langFor(path)}
                  value={draft}
                  onChange={(v) => setDraft(v ?? '')}
                  theme="vs"
                  onMount={onMount}
                  options={{minimap: {enabled: false}, fontSize: 13, automaticLayout: true}}
              />
          )}
        </div>
        {}
        {conflict && (
            <div className="modal-overlay show" onClick={() => setConflict(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <h3>文件已被 AI/他人修改</h3>
                <div className="field">
                  <div className="field-label">服务器上的文件已被修改（保存冲突 409），请选择处理方式：</div>
                </div>
                <div className="modal-actions">
                  <button onClick={handleCopyMine}>复制我的版本</button>
                  <button className="primary" onClick={handleForceOverwrite}>
                    强制覆盖
                  </button>
                  <button onClick={handleDiscard}>放弃修改</button>
                </div>
              </div>
            </div>
        )}
        {toast && <ErrorToast message={toast.message} variant={toast.variant} onClose={() => setToast(null)}/>}
      </div>
  )
}
