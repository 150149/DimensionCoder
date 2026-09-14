import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {FsEntry} from '../api/types'
import {Icon} from '../components/icons'
import {extClass} from './FileTree'

interface FileBrowserProps {
    taskId: string
    onOpenFile: (path: string) => void
    markedPaths?: string[]
    externalModifiedPaths?: string[]
    onOpenQuickOpen?: () => void
}

function joinPath(parent: string, name: string): string {
    return parent ? `${parent}/${name}` : name
}

function fmtSize(size?: number): string {
    if (size == null) return ''
    if (size < 1024) return `${size}B`
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}KB`
    return `${(size / 1024 / 1024).toFixed(1)}MB`
}

export default function FileBrowser({
                                        taskId, onOpenFile, markedPaths = [],
                                        externalModifiedPaths = [], onOpenQuickOpen
                                    }: FileBrowserProps) {
    const [navStack, setNavStack] = useState<string[]>([])
    const [entries, setEntries] = useState<FsEntry[] | null>(null)
    const [workspace, setWorkspace] = useState('')
    const [error, setError] = useState('')

    const curDir = navStack.length ? navStack[navStack.length - 1] : ''

    useEffect(() => {
        let alive = true
        setEntries(null)
        setError('')
        api
            .fsTree(curDir, {taskId})
            .then((tree) => {
                if (!alive) return
                setEntries(tree.entries || [])
                if (tree.workspace) setWorkspace(tree.workspace)
            })
            .catch((err) => {
                if (alive) setError(err instanceof Error ? err.message : '目录加载失败')
            })
        return () => {
            alive = false
        }
    }, [curDir, taskId])

    const jumpTo = useCallback((depth: number) => {
        setNavStack((prev) => prev.slice(0, depth))
    }, [])

    return (
        <div className="file-browser">
            <div className="fb-head">
                {navStack.length > 0 && (
                    <button className="fb-back" title="返回" aria-label="返回上级目录"
                            onClick={() => jumpTo(navStack.length - 1)}>
                        <Icon name="arrowLeft" size={16} gap={0}/>
                    </button>
                )}
                <div className="fb-crumbs">
                    <button className="fb-crumb" onClick={() => jumpTo(0)}>
                        {workspace || '工作区'}
                    </button>
                    {navStack.map((dir, i) => {
                        const seg = dir.split('/')[dir.split('/').length - 1]
                        return (
                            <span key={dir} className="fb-crumb-seg">
                <span className="fb-crumb-sep">›</span>
                                {i === navStack.length - 1 ? (
                                    <span className="fb-crumb current">{seg}</span>
                                ) : (
                                    <button className="fb-crumb" onClick={() => jumpTo(i + 1)}>{seg}</button>
                                )}
              </span>
                        )
                    })}
                </div>
                {onOpenQuickOpen && (
                    <button className="fb-search" title="搜索文件" aria-label="搜索文件"
                            onClick={onOpenQuickOpen}>
                        <Icon name="search" size={15} gap={0}/>
                    </button>
                )}
            </div>
            {error && <div className="fb-msg error">{error}</div>}
            {!error && entries === null && <div className="fb-msg muted">加载中...</div>}
            {entries !== null && entries.length === 0 && (
                <div className="fb-msg muted">此目录为空</div>
            )}
            <div className="fb-list">
                {entries?.map((e) => {
                    const fullPath = joinPath(curDir, e.name)
                    const marked = markedPaths.includes(fullPath)
                    const externalModified = externalModifiedPaths.includes(fullPath)
                    return (
                        <button
                            key={fullPath}
                            className="fb-row"
                            onClick={() => (e.type === 'dir' ? setNavStack((s) => [...s, fullPath]) : onOpenFile(fullPath))}
                        >
                  <span className={`ft-icon ${e.type === 'dir' ? 'folder' : extClass(e.name)}`}>
                    <Icon name={e.type === 'dir' ? 'folder' : 'file'} size={16} gap={0}/>
                  </span>
                            <span className="fb-name">{e.name}</span>
                            {marked && <span className="fb-badge" title="AI 修改">🔧</span>}
                            {externalModified && <span className="fb-badge warn" title="已被 AI/他人修改">⚠️</span>}
                            {e.type === 'dir' ? (
                                <span className="fb-chevron"><Icon name="chevronRight" size={14} gap={0}/></span>
                            ) : (
                                <span className="fb-size">{fmtSize(e.size)}</span>
                            )}
                        </button>
                    )
                })}
            </div>
        </div>
    )
}
