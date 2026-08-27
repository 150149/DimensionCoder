// ═══════════════════════════════════════════════════════════════
// FileTree（SWP4-D / WP4-4 §3.9）
// - 懒展开渲染：展开目录时请求该层 api.fsTree(path) 单层（问题 6）
// - 点击目录展开/收起；点击文件 → onOpenFile(path)（posix 相对路径）
// - FSTREE_HIDDEN（node_modules/.git/dist/.dc_tmp）由后端过滤，前端不渲染
// - 🔧 徽标（M6/J2b）：markedPaths 中已存在于「已加载树节点」的文件
//   （api.fsTree 单层懒加载结果不含未展开深层目录，存在性以已加载集合为准；
//   规格 recursive 全树参数因 api.fsTree 签名缺口（SWP4-A）无法传递，见
//   verify/SWP4-D_判据执行说明.md 上报项）
// - ⚠️ 外部已修改角标（J2b）：externalModifiedPaths 中的文件行显示
//   「⚠️ 已被 AI/他人修改」
// ═══════════════════════════════════════════════════════════════

import type {CSSProperties} from 'react'
import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {FsEntry} from '../api/types'
import {Icon} from '../components/icons'

interface FileTreeProps {
    onOpenFile: (path: string) => void
    /** AI 修改徽标候选路径（FlowOverview 从 artifacts 提取，最多 5 个；存在性以已加载节点为准） */
    markedPaths?: string[]
    /** 外部已修改角标路径（J2b） */
    externalModifiedPaths?: string[]
}

/** posix 相对路径拼接（根层 name 即文件名；子层 parent + '/' + name） */
function joinPath(parent: string, name: string): string {
    return parent ? `${parent}/${name}` : name
}

/** 行缩进：动态 depth 经 CSS 变量注入（v2：样式迁移 class，仅缩进保留动态值） */
const rowPad = (depth: number): CSSProperties => ({paddingLeft: 10 + depth * 14})

export default function FileTree({onOpenFile, markedPaths = [], externalModifiedPaths = []}: FileTreeProps) {
    // 根层（api.fsTree('') 单层）
    const [root, setRoot] = useState<FsEntry[] | null>(null)
    // 目录路径 → 该层 entries（懒加载缓存；null = 未请求）
    const [children, setChildren] = useState<Record<string, FsEntry[]>>({})
    const [expanded, setExpanded] = useState<Set<string>>(new Set())
    // 已加载节点路径集合（存在性检查：根层 + 已展开层）
    const [loadedPaths, setLoadedPaths] = useState<Set<string>>(new Set())
    const [error, setError] = useState('')

    // 根层加载
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

    // 展开目录：未加载 → 请求该层（api.fsTree(path) 单层）
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

    /** 递归渲染节点（目录可展开；文件可打开） */
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
