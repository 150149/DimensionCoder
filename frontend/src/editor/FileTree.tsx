import type {CSSProperties} from 'react'
import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {FsEntry} from '../api/types'
import {Icon} from '../components/icons'

interface FileTreeProps {
    onOpenFile: (path: string) => void

    markedPaths?: string[]

    externalModifiedPaths?: string[]
}

function joinPath(parent: string, name: string): string {
    return parent ? `${parent}/${name}` : name
}

const rowPad = (depth: number): CSSProperties => ({paddingLeft: 10 + depth * 14})

export default function FileTree({onOpenFile, markedPaths = [], externalModifiedPaths = []}: FileTreeProps) {

    const [root, setRoot] = useState<FsEntry[] | null>(null)

    const [children, setChildren] = useState<Record<string, FsEntry[]>>({})
    const [expanded, setExpanded] = useState<Set<string>>(new Set())

    const [loadedPaths, setLoadedPaths] = useState<Set<string>>(new Set())
    const [error, setError] = useState('')

    useEffect(() => {
        let alive = true
        setRoot(null)
        setChildren({})
        setExpanded(new Set())
        setLoadedPaths(new Set())
        setError('')
        api
            .fsTree('')
            .then((tree) => {
                if (!alive) return
                setRoot(tree.entries || [])
                setLoadedPaths(new Set((tree.entries || []).map((e) => e.name)))
            })
            .catch((err) => {
                if (alive) setError(err instanceof Error ? err.message : '文件树加载失败')
            })
        return () => {
            alive = false
        }
    }, [])

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
                            .fsTree(dirPath)
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
        [children],
    )

    const renderEntry = (entry: FsEntry, parentPath: string, depth: number) => {
        const fullPath = joinPath(parentPath, entry.name)
        if (entry.type === 'dir') {
            const isOpen = expanded.has(fullPath)
            const loaded = fullPath in children
            return (
                <div key={fullPath}>
                    <div className="ft-row" style={rowPad(depth)} onClick={() => toggleDir(fullPath)} title={fullPath}>
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
                <div className="ft-row" style={rowPad(depth)} onClick={() => onOpenFile(fullPath)} title={fullPath}>
                    <span className="ft-arrow"/>
                    <span className="ft-icon file">
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

    return (
        <div className="file-tree">
            {error && <div className="ft-msg error">{error}</div>}
            {!error && root === null && <div className="ft-msg muted">加载中...</div>}
            {root !== null && root.length === 0 && (
                <div className="ft-msg muted">工作区为空：请在设置页配置项目目录</div>
            )}
            {root !== null && root.map((e) => renderEntry(e, '', 0))}
        </div>
    )
}
