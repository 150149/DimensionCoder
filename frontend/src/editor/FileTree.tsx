import type {CSSProperties, KeyboardEvent} from 'react'
import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {FsEntry} from '../api/types'
import {Icon} from '../components/icons'

interface FileTreeProps {
    onOpenFile: (path: string) => void
    markedPaths?: string[]
    externalModifiedPaths?: string[]
    taskId?: string
    activePath?: string | null
    filter?: string
    onRootInfo?: (info: { root?: string; workspace?: string }) => void
}

function joinPath(parent: string, name: string): string {
    return parent ? `${parent}/${name}` : name
}

const rowPad = (depth: number): CSSProperties => ({paddingLeft: 10 + depth * 14})

export function extClass(name: string): string {
    const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
    if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'bmp'].includes(ext)) return 'ext-img'
    if (['exe', 'dll', 'so', 'bin', 'pyc', 'zip', '7z', 'tar', 'gz', 'pdf'].includes(ext)) return 'ext-bin'
    const map: Record<string, string> = {
        py: 'ext-py', ts: 'ext-ts', tsx: 'ext-ts', js: 'ext-js', jsx: 'ext-js',
        md: 'ext-md', json: 'ext-json', css: 'ext-css', html: 'ext-html',
    }
    return map[ext] ?? 'ext-bin'
}

export default function FileTree({
                                     onOpenFile, markedPaths = [], externalModifiedPaths = [],
                                     taskId, activePath, filter, onRootInfo
                                 }: FileTreeProps) {
    const [root, setRoot] = useState<FsEntry[] | null>(null)
    const [children, setChildren] = useState<Record<string, FsEntry[]>>({})
    const [expanded, setExpanded] = useState<Set<string>>(new Set())
    const [loadedPaths, setLoadedPaths] = useState<Set<string>>(new Set())
    const [error, setError] = useState('')
    const [wsInfo, setWsInfo] = useState<{ root?: string; workspace?: string }>({})

    const [reloadKey, setReloadKey] = useState(0)
    useEffect(() => {
        let alive = true
        setRoot(null)
        setChildren({})
        setExpanded(new Set())
        setLoadedPaths(new Set())
        setError('')
        api
            .fsTree('', {taskId})
            .then((tree) => {
                if (!alive) return
                setRoot(tree.entries || [])
                setLoadedPaths(new Set((tree.entries || []).map((e) => e.name)))
                const info = {root: tree.root, workspace: tree.workspace}
                setWsInfo(info)
                onRootInfo?.(info)
            })
            .catch((err) => {
                if (alive) setError(err instanceof Error ? err.message : '文件树加载失败')
            })
        return () => {
            alive = false
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [taskId, reloadKey])

    const toggleDir = useCallback(
        (dirPath: string) => {
            setExpanded((prev) => {
                const next = new Set(prev)
                if (next.has(dirPath)) {
                    next.delete(dirPath)
                } else {
                    next.add(dirPath)
                    if (!(dirPath in children)) {
                        api
                            .fsTree(dirPath, {taskId})
                            .then((tree) => {
                                const entries = tree.entries || []
                                setChildren((c) => ({...c, [dirPath]: entries}))
                                setLoadedPaths((p) => {
                                    const np = new Set(p)
                                    for (const e of entries) np.add(joinPath(dirPath, e.name))
                                    return np
                                })
                            })
                            .catch((err) => setError(err instanceof Error ? err.message : '目录加载失败'))
                    }
                }
                return next
            })
        },
        [children, taskId],
    )

    const matchFilter = useCallback(
        (name: string) => {
            const f = (filter ?? '').trim().toLowerCase()
            return !f || name.toLowerCase().includes(f)
        },
        [filter],
    )

    const rowKey = (handler: () => void) => (e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            handler()
        }
    }

    const renderEntry = (entry: FsEntry, parentPath: string, depth: number) => {
        const fullPath = joinPath(parentPath, entry.name)
        if (entry.type === 'dir') {
            const isOpen = expanded.has(fullPath)
            const loaded = fullPath in children
            return (
                <div key={fullPath}>
                    <div
                        className="ft-row"
                        style={rowPad(depth)}
                        onClick={() => toggleDir(fullPath)}
                        onKeyDown={rowKey(() => toggleDir(fullPath))}
                        role="treeitem"
                        aria-expanded={isOpen}
                        tabIndex={0}
                        title={fullPath}
                    >
                        {Array.from({length: depth}, (_, i) => <span key={i} className="ft-guide"/>)}
                        <span className={`ft-arrow${isOpen ? ' open' : ''}`}>
              <Icon name="chevronRight" size={11} gap={0}/>
            </span>
                        <span className="ft-icon folder">
              <Icon name="folder" size={14} gap={0}/>
            </span>
                        <span>{entry.name}</span>
                    </div>
                    {isOpen && (
                        <div>
                            {loaded && children[fullPath].length === 0 && (
                                <div className="ft-empty" style={rowPad(depth + 1)}>
                                    （空）
                                </div>
                            )}
                            {loaded && children[fullPath].map((c) => renderEntry(c, fullPath, depth + 1))}
                        </div>
                    )}
                </div>
            )
        }
        const marked = loadedPaths.has(fullPath) && markedPaths.includes(fullPath)
        const externalModified = externalModifiedPaths.includes(fullPath)
        return (
            <div key={fullPath}>
                <div
                    className={`ft-row${activePath === fullPath ? ' active' : ''}`}
                    style={rowPad(depth)}
                    onClick={() => onOpenFile(fullPath)}
                    onKeyDown={rowKey(() => onOpenFile(fullPath))}
                    role="treeitem"
                    aria-selected={activePath === fullPath}
                    tabIndex={0}
                    title={fullPath}
                >
                    {Array.from({length: depth}, (_, i) => <span key={i} className="ft-guide"/>)}
                    <span className="ft-arrow"/>
                    <span className={`ft-icon ${extClass(entry.name)}`}>
            <Icon name="file" size={14} gap={0}/>
          </span>
                    <span>{entry.name}</span>
                    {marked && (
                        <span className="ft-mark" title="AI 修改">
              <Icon name="wrench" size={12} gap={0}/>
            </span>
                    )}
                    {externalModified && (
                        <span className="ft-external" title="外部已修改">
              <Icon name="alert" size={11} gap={3}/>
              已被 AI/他人修改
            </span>
                    )}
                </div>
            </div>
        )
    }

    const filterActive = !!(filter ?? '').trim()
    const visibleRoot = (root ?? []).filter((e) => matchFilter(e.name))

    return (
        <div className="file-tree" role="tree" aria-label="工作区文件树">
            <div className="ft-head">
          <span className="ft-ws" title={wsInfo.root || '项目根'}>
            {wsInfo.workspace || '项目根'}
          </span>
                <button className="ft-refresh" title="刷新" aria-label="刷新文件树"
                        onClick={() => setReloadKey((k) => k + 1)}>
                    <Icon name="refresh" size={13} gap={0}/>
                </button>
            </div>
            {error && <div className="ft-msg error">{error}</div>}
            {!error && root === null && <div className="ft-msg muted">加载中...</div>}
            {root !== null && visibleRoot.length === 0 && (
                <div className="ft-msg muted">
                    {filterActive ? '无匹配文件' : `工作区为空（${wsInfo.workspace || '项目根'}）`}
                </div>
            )}
            {root !== null && visibleRoot.map((e) => renderEntry(e, '', 0))}
        </div>
    )
}
