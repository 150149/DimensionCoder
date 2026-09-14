import {useMemo, useRef} from 'react'
import type {StoreMsg} from './useMessageStore'

export interface ToolExpanded {
  set: Set<string>
  sig: string
}

export function useToolExpanded(allMsgs: StoreMsg[], expandedTools: Set<string>): ToolExpanded {
  const cacheRef = useRef<ToolExpanded | null>(null)
  return useMemo(() => {
    const expanded = new Set<string>()
    let lastDoneId: string | null = null
    for (const m of allMsgs) {
      const isTool = m.state === 'confirmed' ? m.role === 'tool' : m.kind === 'tool'
      if (!isTool) continue
      const cid = m.state === 'confirmed'
          ? (m.entityId ?? (m.seq != null ? `seq-${m.seq}` : m.key))
          : (m.entityId ?? m.key)
      if (m.state === 'confirmed' || m.status === 'done') lastDoneId = cid
      else expanded.add(cid)
    }
    if (lastDoneId) expanded.add(lastDoneId)
    for (const id of expandedTools) expanded.add(id)
    const sig = [...expanded].sort().join('\u0000')
    const cached = cacheRef.current
    if (cached && cached.sig === sig) return cached
    const next: ToolExpanded = {set: expanded, sig}
    cacheRef.current = next
    return next
  }, [allMsgs, expandedTools])
}
