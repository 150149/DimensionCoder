import {useCallback, useMemo, useRef, useState} from 'react'
import type {ConfirmItem} from '../api/types'

export interface StoreMsg {
  key: string
  seq?: number
  entityId?: string
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
  input?: unknown
  output?: string
  status?: 'streaming' | 'executing' | 'done' | 'error'
  kind?: 'text' | 'thinking' | 'tool'
  state: 'pending' | 'confirmed'
  roundUid?: string
  arrivedAt: number
}

export interface PendingPatch {
  entityId: string
  role: StoreMsg['role']
  append?: string
  content?: string
  toolName?: string
  input?: unknown
  output?: string
  status?: StoreMsg['status']
  kind?: StoreMsg['kind']
  inputAppend?: string
  roundUid?: string
}

let _arrived = 0

function nextArrived(): number {
  _arrived += 1
  return _arrived
}

function itemKey(entityId?: string | null, seq?: number): string {
  return entityId ?? (seq != null ? `db:${seq}` : `anon:${nextArrived()}`)
}

function sameDbRow(a?: number | null, b?: number | null): boolean {
  return a == null || b == null || a === b
}

function fromItem(it: ConfirmItem, now: number): StoreMsg {
  return {
    key: itemKey(it.entityId, it.seq),
    seq: it.seq,
    entityId: it.entityId ?? undefined,
    role: it.role as StoreMsg['role'],
    content: it.content ?? '',
    toolName: it.toolName ?? undefined,
    input: it.input ?? undefined,
    output: it.output ?? undefined,
    state: 'confirmed',
    roundUid: undefined,
    arrivedAt: now,
  }
}

export function rowToMsg(row: Record<string, unknown>): StoreMsg {
  const seq = row.seq as number | undefined
  const entityId = ((row.entity_id ?? row.entityId)
      ?? (row.role === 'tool' ? row.tool_call_id : undefined)) as string | undefined
  return {
    key: itemKey(entityId, seq),
    seq,
    entityId,
    role: (row.role as StoreMsg['role']) ?? 'assistant',
    content: (row.content as string) ?? '',
    toolName: (row.toolName ?? row.tool_name) as string | undefined,
    input: row.input,
    output: (row.output ?? row.tool_output) as string | undefined,
    state: 'confirmed',
    roundUid: undefined,
    arrivedAt: nextArrived(),
  }
}

export interface MessageStoreApi {
  messages: StoreMsg[]
  lastSeq: number
  upsertPending: (patch: PendingPatch) => void
  confirm: (items: ConfirmItem[]) => void
  loadPage: (rows: unknown[]) => void
  prependPage: (rows: unknown[]) => void
  clearPending: (kind?: StoreMsg['kind']) => void
  dropPending: (entityId: string) => void
  reset: () => void
}

export function useMessageStore(): MessageStoreApi {
  const msgsRef = useRef<Map<string, StoreMsg>>(new Map())
  const aliasRef = useRef<Map<string, string>>(new Map())
  const [version, setVersion] = useState(0)

  const bump = useCallback(() => setVersion((v) => v + 1), [])

  const upsertPending = useCallback((patch: PendingPatch) => {
    const map = msgsRef.current
    const alias = aliasRef.current
    const aliasKey = patch.entityId
        ? (patch.kind === 'thinking' ? `th:${patch.entityId}` : patch.entityId)
        : `anon:${nextArrived()}`
    const existingKey = alias.get(aliasKey)
    const existing = existingKey ? map.get(existingKey) : undefined
    if (existing && existing.state === 'pending') {
      if (patch.role !== undefined) existing.role = patch.role
      if (patch.append) existing.content += patch.append
      if (patch.content !== undefined) existing.content = patch.content
      if (patch.toolName !== undefined) existing.toolName = patch.toolName
      if (patch.input !== undefined) existing.input = patch.input
      if (patch.output !== undefined) existing.output = patch.output
      if (patch.status !== undefined) existing.status = patch.status
      if (patch.kind !== undefined) existing.kind = patch.kind
      if (patch.inputAppend !== undefined) {
        existing.input = (typeof existing.input === 'string' ? existing.input : '') + patch.inputAppend
      }
      if (patch.roundUid !== undefined) existing.roundUid = patch.roundUid
    } else if (existing && existing.state === 'confirmed') {
      return
    } else {
      map.set(aliasKey, {
        key: aliasKey,
        entityId: patch.entityId,
        role: patch.role,
        content: patch.append ?? patch.content ?? '',
        toolName: patch.toolName,
        input: patch.input,
        output: patch.output,
        status: patch.status ?? 'streaming',
        kind: patch.kind,
        state: 'pending',
        roundUid: patch.roundUid,
        arrivedAt: nextArrived(),
      })
      alias.set(aliasKey, aliasKey)
    }
    bump()
  }, [bump])

  const confirm = useCallback((items: ConfirmItem[]) => {
    if (!items.length) return
    const map = msgsRef.current
    const alias = aliasRef.current
    const now = nextArrived()
    for (const it of items) {
      const aliasKey = it.entityId ? alias.get(it.entityId) : undefined
      const seqKey = it.seq != null ? `db:${it.seq}` : undefined
      const aliasHit = aliasKey ? map.get(aliasKey) : undefined
      const hit = (aliasHit && sameDbRow(aliasHit.seq, it.seq)) ? aliasHit
          : (seqKey ? map.get(seqKey) : undefined)
      const fresh = fromItem(it, now)
      if (hit) {
        fresh.key = hit.key
        fresh.arrivedAt = hit.arrivedAt
        map.set(hit.key, fresh)
      } else {
        if (seqKey && map.has(fresh.key)) fresh.key = seqKey
        map.set(fresh.key, fresh)
      }
      if (it.entityId && !alias.has(it.entityId)) alias.set(it.entityId, fresh.key)
    }
    bump()
  }, [bump])

  const loadPage = useCallback((rows: unknown[]) => {
    if (!rows.length) return
    const map = msgsRef.current
    const alias = aliasRef.current
    for (const row of rows) {
      const m = rowToMsg(row as Record<string, unknown>)
      const aliasKey = m.entityId ? alias.get(m.entityId) : undefined
      const aliasHit = aliasKey ? map.get(aliasKey) : undefined
      const seqKey = m.seq != null ? `db:${m.seq}` : undefined
      const hit = (aliasHit && sameDbRow(aliasHit.seq, m.seq)) ? aliasHit
          : (seqKey ? map.get(seqKey) : undefined)
      if (hit) {
        m.key = hit.key
        m.arrivedAt = hit.arrivedAt
        map.set(hit.key, m)
      } else {
        if (seqKey && map.has(m.key)) m.key = seqKey
        map.set(m.key, m)
      }
      if (m.entityId && !alias.has(m.entityId)) alias.set(m.entityId, m.key)
    }
    bump()
  }, [bump])

  const prependPage = loadPage

  const clearPending = useCallback((kind?: StoreMsg['kind']) => {
    const map = msgsRef.current
    for (const [k, m] of [...map.entries()]) {
      if (m.state === 'pending' && (!kind || m.kind === kind)) map.delete(k)
    }
    bump()
  }, [bump])

  const dropPending = useCallback((entityId: string) => {
    const map = msgsRef.current
    const key = aliasRef.current.get(entityId) ?? entityId
    if (map.get(key)?.state === 'pending') {
      map.delete(key)
      aliasRef.current.delete(entityId)
      bump()
    }
  }, [bump])

  const reset = useCallback(() => {
    msgsRef.current = new Map()
    aliasRef.current = new Map()
    bump()
  }, [bump])

  const messages = (() => {
    void version
    const all = [...msgsRef.current.values()]
    const confirmed = all.filter((m) => m.state === 'confirmed')
        .sort((a, b) => (a.seq ?? Infinity) - (b.seq ?? Infinity) || a.arrivedAt - b.arrivedAt)
    const pending = all.filter((m) => m.state === 'pending')
        .sort((a, b) => a.arrivedAt - b.arrivedAt)
    return [...confirmed, ...pending]
  })()

  const lastSeq = (() => {
    let max = -1
    for (const m of msgsRef.current.values()) {
      if (m.state === 'confirmed' && m.seq != null && m.seq > max) max = m.seq
    }
    return max
  })()

  return useMemo(() => ({messages, lastSeq, upsertPending, confirm, loadPage, prependPage, clearPending, dropPending, reset}),
      [messages, lastSeq, upsertPending, confirm, loadPage, prependPage, clearPending, dropPending, reset])
}
