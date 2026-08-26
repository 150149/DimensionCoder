import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {createStepStream, getLastSeq} from '../api/sse'

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }
}

function fire(es: MockEventSource, ev: Record<string, unknown>): void {
  es.onmessage?.({data: JSON.stringify(ev)})
}

describe('sse client', () => {
  beforeEach(() => {
    MockEventSource.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('test_full_marker_rerender', () => {
    const onFullRerender = vi.fn()
    const onChunk = vi.fn()
    const stream = createStepStream('t1', {onFullRerender, onChunk})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '前缀 __DC_FULL__', seq: 1})

    expect(onFullRerender).toHaveBeenCalledTimes(1)
    expect(onFullRerender.mock.calls[0][0]).toMatchObject({command: 'streamChunk', seq: 1})
    expect(onChunk).not.toHaveBeenCalled()

    expect(getLastSeq('t1')).toBe(1)
  })

  it('test_pending_buffer_replay', () => {
    const onChunk = vi.fn()
    const stream = createStepStream('t1', {onChunk})

    stream.connect(0)
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '缓冲文本', seq: 1})
    expect(onChunk).not.toHaveBeenCalled()

    stream.setActiveStep('s1')
    expect(onChunk).toHaveBeenCalledTimes(1)
    expect(onChunk.mock.calls[0][0]).toMatchObject({chunk: '缓冲文本', seq: 1})

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '实时文本', seq: 2})
    expect(onChunk).toHaveBeenCalledTimes(2)
  })

  it('test_retry_marker', () => {
    const onRetry = vi.fn()
    const onRetryClear = vi.fn()
    const onChunk = vi.fn()
    const stream = createStepStream('t1', {onRetry, onRetryClear, onChunk})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__3/10__DC_RETRY__', seq: 1})
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onRetry.mock.calls[0][1]).toBe(3)

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '继续输出', seq: 2})
    expect(onRetryClear).toHaveBeenCalledTimes(1)
    expect(onChunk).toHaveBeenCalledTimes(1)
    expect(onChunk.mock.calls[0][0]).toMatchObject({chunk: '继续输出'})

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__5/10__DC_RETRY__', seq: 3})
    expect(onRetry).toHaveBeenCalledTimes(2)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 4})
    expect(onRetryClear).toHaveBeenCalledTimes(2)
  })

  it('test_retry_marker_cleared_by_thinking_chunk', () => {

    const onRetry = vi.fn()
    const onRetryClear = vi.fn()
    const onThinkingChunk = vi.fn()
    const stream = createStepStream('t1', {onRetry, onRetryClear, onThinkingChunk})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__2/10__DC_RETRY__', seq: 1})
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onRetryClear).not.toHaveBeenCalled()

    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考', seq: 2})
    expect(onRetryClear).toHaveBeenCalledTimes(1)
    expect(onThinkingChunk).toHaveBeenCalledTimes(1)
  })

  it('test_retry_marker_cleared_by_tool_start', () => {

    const onRetry = vi.fn()
    const onRetryClear = vi.fn()
    const onToolStart = vi.fn()
    const stream = createStepStream('t1', {onRetry, onRetryClear, onToolStart})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__2/10__DC_RETRY__', seq: 1})
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(onRetryClear).not.toHaveBeenCalled()

    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c1', toolName: 'x', input: '', seq: 2})
    expect(onRetryClear).toHaveBeenCalledTimes(1)
    expect(onToolStart).toHaveBeenCalledTimes(1)
  })

  it('test_retry_marker_cleared_by_next_round_events', () => {

    const onRetry = vi.fn()
    const onRetryClear = vi.fn()
    const stream = createStepStream('t1', {onRetry, onRetryClear})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__1/10__DC_RETRY__', seq: 1})
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考', seq: 2})
    expect(onRetryClear).toHaveBeenCalledTimes(1)
    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c1', toolName: 'x', input: '', seq: 3})
    fire(es, {command: 'toolCallParam', taskId: 't1', stepId: 's1', callId: 'c1', delta: 'a', seq: 4})
    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c1', output: 'ok', seq: 5})
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 6})

    expect(onRetryClear).toHaveBeenCalledTimes(1)
  })

  it('test_inline_user_msg_marker', () => {

    const onUserMessage = vi.fn()
    const onChunk = vi.fn()
    const stream = createStepStream('t1', {onUserMessage, onChunk})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    const marker = `__DC_USER_MSG__${JSON.stringify({stepId: 's1', content: '请调整'})}__DC_USER_MSG__`
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: marker, seq: 1})
    expect(onUserMessage).toHaveBeenCalledTimes(1)
    expect(onUserMessage.mock.calls[0][0]).toMatchObject({command: 'userMessage', stepId: 's1', message: '请调整'})
    expect(onChunk).not.toHaveBeenCalled()
  })

  it('test_thinking_chunk_dispatch', () => {
    const onThinkingChunk = vi.fn()
    const stream = createStepStream('t1', {onThinkingChunk})

    stream.connect(0)
    stream.setActiveStep('s1')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考片段', seq: 1})
    expect(onThinkingChunk).toHaveBeenCalledTimes(1)
    expect(onThinkingChunk.mock.calls[0][0]).toMatchObject({command: 'thinkingChunk', chunk: '思考片段'})
  })

  it('test_reconnect_with_lastSeq', () => {
    vi.useFakeTimers()
    const stream = createStepStream('t1', {})

    stream.connect(0)
    const es1 = MockEventSource.instances[0]
    expect(es1.url).toBe('/sse?taskId=t1&lastSeq=0')

    fire(es1, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: 'a', seq: 5})
    fire(es1, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: 'b', seq: 7})
    expect(getLastSeq('t1')).toBe(7)

    es1.onerror?.()
    expect(es1.closed).toBe(true)

    vi.advanceTimersByTime(3000)
    expect(MockEventSource.instances.length).toBe(2)
    const es2 = MockEventSource.instances[1]
    expect(es2.url).toBe('/sse?taskId=t1&lastSeq=7')
  })
})
