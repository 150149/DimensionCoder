// ═══════════════════════════════════════════════════════════════
// CodeEditor（SWP4-D / WP4-4 §3.9）
// - 首行 import "./monacoSetup"：本地 worker + loader 覆盖（P1-6 禁 CDN）
// - FlowOverview 中 React.lazy 懒加载 + Suspense fallback="加载编辑器..."
// - 打开文件：api.fsRead → 内容 + mtime（baseMtime 来源）；>2MB → 413 提示
// - 保存：Ctrl+S（onMount editor.addCommand，monaco 从 monacoSetup 导入）→
//   api.fsWrite；409（乐观锁，文件已被 AI/外部修改）→ 三选弹窗（J2a）：
//   ①复制我的版本（clipboard + 重载服务器版）②强制覆盖（重取最新 mtime 再写）
//   ③放弃修改（直接重载）——禁止静默丢弃用户内容
// - 保存成功 ErrorToast success「已保存」；失败 error 变体
// - dirty 经 onDirtyChange 上报（FlowOverview tab 标题 ●）
// - J2b 外部修改：轮询 fsRead 的 mtime 与打开时不一致（且非本人保存）→
//   onExternalModified(path, true)（文件行显示「⚠️ 已被 AI/他人修改」）
// ═══════════════════════════════════════════════════════════════

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
  /** 当前打开文件（posix 相对路径）；null = 未打开 */
  path: string | null
  /** dirty 状态上报（FlowOverview tab 标题 ●） */
  onDirtyChange?: (dirty: boolean) => void
  /** J2b 外部修改上报（文件行「⚠️ 已被 AI/他人修改」角标） */
  onExternalModified?: (path: string, modified: boolean) => void
}

/** §3.9 扩展名映射表（固定）：ts/tsx→typescript、js/jsx→javascript、
 *  py→python、json→json、md→markdown、html→html、css→css、其他→plaintext */
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

/** J2b 轮询间隔（fsRead mtime 检查） */
const POLL_MS = 5000

export default function CodeEditor({path, onDirtyChange, onExternalModified}: CodeEditorProps) {
  // 服务器版本（打开/重载/保存成功后的权威内容）
  const [file, setFile] = useState<FsFile | null>(null)
  // 编辑器受控内容（draft；dirty = draft !== file.content）
  const [draft, setDraft] = useState('')
  const [fileError, setFileError] = useState('')
  const [toast, setToast] = useState<{ message: string; variant: 'error' | 'success' } | null>(null)
  // 409 三选弹窗（J2a：用户修改不丢失）
  const [conflict, setConflict] = useState(false)
  const baseMtimeRef = useRef<number | null>(null)
  const pathRef = useRef<string | null>(null)
  const draftRef = useRef('')
  const saveRef = useRef<() => void>(() => {
  })
  draftRef.current = draft

  /** 加载/重载服务器版（打开文件或三选后回滚/放弃） */
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
          // 大文件提示（mock 413 响应）：错误文案来自后端（文件超过 2MB 上限...）
          setToast({message, variant: 'error'})
        })
  }, [])

  // path 变化 → 打开文件
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

  // dirty 上报（FlowOverview tab 标题 ●）
  const dirty = file !== null && draft !== file.content
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  /** §3.9 保存：api.fsWrite；409 → 三选弹窗 */
  const save = useCallback(async () => {
    const p = pathRef.current
    if (!p) return
    try {
      await api.fsWrite(p, draftRef.current)
      setFile((f) => (f ? {...f, content: draftRef.current} : f))
      onExternalModified?.(p, false)
      setToast({message: '已保存', variant: 'success'})
      // 保存成功：重取服务器最新 mtime 作为新基准（本人保存后不再报外部修改）；
      // 该刷新失败不影响「已保存」提示
      try {
        const fresh = await api.fsRead(p)
        baseMtimeRef.current = fresh.mtime
      } catch {
        /* 静默：mtime 刷新失败，下一轮轮询自愈 */
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(true) // 409：三选弹窗，禁止静默丢弃用户内容
      } else {
        setToast({message: err instanceof Error ? err.message : '保存失败', variant: 'error'})
      }
    }
  }, [onExternalModified])

  saveRef.current = save

  // onMount：Ctrl+S 保存命令（monaco 从 monacoSetup 导入，同一实例）
  const onMount = useCallback((ed: MonacoEditor.IStandaloneCodeEditor) => {
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
  }, [])

  // J2b 外部修改轮询：mtime 与打开时不一致（且非本人保存）→ 上报角标
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
            /* 轮询失败静默，下一轮重试 */
          })
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [path, onExternalModified])

  // J2a ①：当前内容复制到剪贴板 + 重载服务器版
  const handleCopyMine = async () => {
    const p = pathRef.current
    if (!p) return
    try {
      await navigator.clipboard?.writeText(draftRef.current)
    } catch {
      /* 剪贴板不可用（jsdom/权限）：仍继续重载，不阻塞 */
    }
    setConflict(false)
    reload()
  }

  // J2a ②：强制覆盖——重取最新 mtime 后再写
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

  // J2a ③：放弃修改——直接重载
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
        {/* 409 三选弹窗（J2a）：用户修改不丢失 */}
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
