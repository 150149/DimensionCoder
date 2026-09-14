import {useMemo, useRef} from 'react'
import type {Message} from '../api/types'
import type {StoreMsg} from './useMessageStore'

export function useConfirmedViews(allMsgs: StoreMsg[]): Message[] {
    const cacheRef = useRef(new WeakMap<StoreMsg, Message>())
    return useMemo(() => {
        const cache = cacheRef.current
        return allMsgs
            .filter((m) => m.state === 'confirmed')
            .map((m) => {
                const hit = cache.get(m)
                if (hit) return hit
                const view = {
                    seq: m.seq,
                    role: m.role,
                    content: m.content,
                    toolName: m.toolName,
                    tool_call_id: m.entityId ?? (m.seq != null ? `seq-${m.seq}` : undefined),
                    input: m.input,
                    output: m.output,
                } as unknown as Message
                cache.set(m, view)
                return view
            })
    }, [allMsgs])
}
