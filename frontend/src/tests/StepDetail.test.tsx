import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {Message, StepData, TaskDetail} from '../api/types'
import {api} from '../api/client'
import StepDetail from '../panels/StepDetail'

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="monaco-mock"/>,
}))
vi.mock('../editor/monacoSetup', () => ({
  monaco: {KeyMod: {CtrlCmd: 2048}, KeyCode: {KeyS: 49}},
}))

vi.mock('../api/client', () => ({
  api: {
    listTasks: vi.fn(),
    createTask: vi.fn(),
    startTask: vi.fn(),
    pauseTask: vi.fn(),
    getTask: vi.fn(),
    getMonitorConversations: vi.fn(),
    deleteTask: vi.fn(),
    getStep: vi.fn(),
    stepIntervene: vi.fn(),
    flowIntervene: vi.fn(),
    approveGate: vi.fn(),
    rejectGate: vi.fn(),
    resumeStep: vi.fn(),
    compressStep: vi.fn(),
    getConfig: vi.fn(),
    saveConfig: vi.fn(),
    testLlm: vi.fn(),
    fsTree: vi.fn(),
    fsRead: vi.fn(),
    fsWrite: vi.fn(),
  },
}))

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

const stepData: StepData = {
  stepId: 's1',
  taskId: 't1',
  prep: {
    system_message: '系统提示',
    system_prompt: '',
    step_context: '步骤上下文内容',
    temp_dir: '',
    model_tier: 'light',
    step_title: '',
    step_id: 's1',
  },
  conversation: [
    {role: 'system', content: '系统提示内容', seq: 1},
    {role: 'user', content: '用户问题', seq: 2},
    {role: 'assistant', content: 'AI 回答', seq: 3},
    {role: 'tool', content: '', toolName: 'read_file', tool_call_id: 'c1', input: {path: 'src/a.ts'}, output: '文件内容', seq: 4},
    {role: 'thinking', content: '思考过程', seq: 5},
  ],
  messages: [],
  max_seq: 5,
  step: {step_id: 's1', title: '实现功能', status: 'active', required: true, model_tier: 'light'},
}

const taskDetail: TaskDetail = {
  task: {
    id: 't1',
    type: 'dev-full-flow',
    title: '登录模块',
    status: 'active',
    created_at: '',
    updated_at: '',
    steps: [
      {step_id: 's1', title: '实现功能', status: 'active', required: true},
      {step_id: 's2', title: '测试', status: 'pending', required: true},
    ],
  },
  artifacts: [],
  monitor_conversations: {},
  step_messages: {},
  recent_events: [],
}

function renderStep() {
  return render(
      <MemoryRouter initialEntries={['/task/t1/step/s1']}>
        <StepDetail taskId="t1" stepId="s1"/>
      </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.getStep).mockResolvedValue(stepData)
  vi.mocked(api.getTask).mockResolvedValue(taskDetail)
  vi.mocked(api.getMonitorConversations).mockResolvedValue({task_id: 't1', monitor_conversations: {}})
  vi.mocked(api.resumeStep).mockResolvedValue({})
  vi.mocked(api.startTask).mockResolvedValue({status: 'ok'} as never)
  vi.mocked(api.compressStep).mockResolvedValue({status: 'skipped', count: 3})
  vi.mocked(api.stepIntervene).mockResolvedValue({})
  vi.mocked(api.getConfig).mockResolvedValue({
    baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
    projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
    contextWindow: 1_048_576,
  })
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {

  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('StepDetail', () => {
  it('渲染 5 类消息：system 可折叠/user 气泡/assistant markdown/tool 卡/thinking', async () => {
    renderStep()
    expect(await screen.findByText('AI 回答')).toBeTruthy()

    expect(screen.getByText('系统提示 (6 字)')).toBeTruthy()
    expect(document.querySelector('.msg-system .sys-body')?.textContent).toContain('系统提示内容')

    expect(document.querySelector('.msg-system')?.className).toContain('collapsed')

    const sysEl = document.querySelector('.msg-system') as HTMLElement
    const ctxBubble = document.querySelectorAll('.user-bubble')[0] as HTMLElement
    expect(sysEl.compareDocumentPosition(ctxBubble) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    const bubbles = document.querySelectorAll('.user-bubble')
    expect(bubbles[0]?.textContent).toContain('步骤上下文内容')
    expect(bubbles[1]?.textContent).toBe('用户问题')

    expect(document.querySelector('.ai-block')).toBeTruthy()

    const toolPanel = document.querySelector('.tool-panel')
    expect(toolPanel?.textContent).toContain('读取文件')
    expect(toolPanel?.textContent).toContain('a.ts')

    expect(document.querySelector('.think-box')?.textContent).toContain('思考过程')

    expect(screen.getByText('轻量模型')).toBeTruthy()
    expect(screen.getByText('必做')).toBeTruthy()
    expect(screen.getByText('状态: active')).toBeTruthy()
  })

  it('step_context 系统注入上下文按 Markdown 渲染（2026-08-22：不再一坨纯文本）', async () => {
    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData,
      prep: {
        ...stepData.prep,
        step_context: '# 步骤: 改造方案设计\n\n## 任务背景\n- 标题: X\n\n```python\nprint(1)\n```',
      },
    })
    renderStep()

    const h1 = await screen.findByText('步骤: 改造方案设计')
    expect(h1.tagName).toBe('H1')
    expect(screen.getByText('任务背景').tagName).toBe('H2')

    expect(document.querySelector('.user-bubble li')?.textContent).toBe('标题: X')
    expect(document.querySelector('.user-bubble pre code')?.textContent).toContain('print(1)')
  })

  it('错误卡：llmError → .llm-error-card（code/message/重试次数/可重试标记），🔄 重试 → resumeStep', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'llmError', taskId: 't1', stepId: 's1', code: 'rate_limit', message: '请求限流', retryable: true, retryCount: 2, seq: 10})
    await act(async () => {
    })

    const card = document.querySelector('.llm-error-card')
    expect(card).toBeTruthy()
    expect(card?.querySelector('strong')?.textContent).toContain('LLM 错误: rate_limit')
    expect(card?.textContent).toContain('请求限流')
    expect(card?.textContent).toContain('已重试 2 次')
    expect(card?.textContent).toContain('（可重试）')

    expect(vi.mocked(api.getStep).mock.calls.length).toBeGreaterThanOrEqual(2)

    const retryBtn = card?.querySelector('.retry-btn') as HTMLButtonElement
    expect(retryBtn).toBeTruthy()
    fireEvent.click(retryBtn)
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('t1', 's1')

    expect(api.startTask).toHaveBeenCalledWith('t1')

    expect(document.querySelector('.llm-error-card')).toBeNull()
  })

  it('限流重试进度条：恢复后 thinkingChunk（思考流）立即清除（2026-08-24 用户反馈）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__2/10__DC_RETRY__', seq: 1})
    await act(async () => {
    })
    expect(document.querySelector('.retry-progress')?.textContent).toContain('正在重试 (2/10)')

    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考', seq: 2})
    await act(async () => {
    })
    expect(document.querySelector('.retry-progress')).toBeNull()
  })

  it('限流重试进度条：切换步骤后清空（2026-08-24）', async () => {
    const {rerender} = render(
        <MemoryRouter initialEntries={['/task/t1/step/s1']}>
          <StepDetail taskId="t1" stepId="s1"/>
        </MemoryRouter>,
    )
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '__DC_RETRY__3/10__DC_RETRY__', seq: 1})
    await act(async () => {
    })
    expect(document.querySelector('.retry-progress')).toBeTruthy()

    rerender(
        <MemoryRouter initialEntries={['/task/t1/step/s2']}>
          <StepDetail taskId="t1" stepId="s2"/>
        </MemoryRouter>,
    )
    await act(async () => {
    })
    expect(document.querySelector('.retry-progress')).toBeNull()
  })

  it('错误卡：retryable=false 不显示重试按钮（仅不可重试标记）', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'llmError', taskId: 't1', stepId: 's1', code: 'auth', message: '密钥无效', retryable: false, retryCount: 1, seq: 11})
    await act(async () => {
    })

    const card = document.querySelector('.llm-error-card')
    expect(card?.textContent).toContain('（不可重试）')
    expect(card?.querySelector('.retry-btn')).toBeNull()
  })

  it('压缩按钮：📦压缩 → compressStep 调用 + 成功后刷新（getStep 重拉）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    expect(api.getStep).toHaveBeenCalledTimes(1)

    fireEvent.click(document.querySelector('.intervene-bar .compress-btn') as HTMLElement)
    await act(async () => {
    })

    expect(api.compressStep).toHaveBeenCalledWith('t1', 's1')
    expect(api.getStep).toHaveBeenCalledTimes(2)
  })

  it('__DC_FULL__：重新 getStep 全量渲染（保留已展开的工具卡状态：callId 集合）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    expect(api.getStep).toHaveBeenCalledTimes(1)

    const header = document.querySelector('.tool-header') as HTMLElement
    fireEvent.click(header)
    expect(document.querySelector('.tool-panel')?.className).toContain('expanded')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '前缀 __DC_FULL__', seq: 20})
    await act(async () => {
    })
    expect(api.getStep).toHaveBeenCalledTimes(2)

    expect(document.querySelector('.tool-panel')?.className).toContain('expanded')
  })

  it('工具卡 callId 防覆盖：SSE live 卡渲染，轮询/重拉不覆盖已渲染卡', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    const es = MockEventSource.instances[0]

    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', input: '{"command":"npm test"}', seq: 6})
    await act(async () => {
    })
    expect(document.querySelectorAll('.tool-panel').length).toBe(2)
    expect(document.querySelectorAll('.loading-spinner').length).toBeGreaterThan(0)

    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData,
      conversation: [
        ...stepData.conversation,
        {role: 'tool', content: '', toolName: 'run_cmd', tool_call_id: 'c9', input: {command: 'npm test'}, output: 'passed', seq: 7},
      ],
    })
    fire(es, {command: 'refreshData', taskId: 't1', seq: 8})
    await act(async () => {
    })
    expect(document.querySelectorAll('.tool-panel').length).toBe(2)

    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', output: 'passed', seq: 9})
    await act(async () => {
    })
    expect(document.querySelectorAll('.loading-spinner').length).toBe(1)
    expect(screen.getByText(/AI 正在思考/)).toBeTruthy()
    const livePanel = document.querySelectorAll('.tool-panel')[1]
    expect(livePanel?.textContent).toContain('passed')

    expect(livePanel?.className).toContain('expanded')
  })

  it('发送后立即显示待发送气泡（发送中…），userMessage 事件后转正并全量重拉', async () => {

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'pending', required: true},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')
    expect(document.querySelector('.user-bubble.sending')).toBeNull()

    vi.mocked(api.stepIntervene).mockImplementation(
        () => new Promise((res) => setTimeout(() => res({}), 100)) as ReturnType<typeof api.stepIntervene>,
    )
    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '请调整方向'}})
    fireEvent.click(document.querySelector('.intervene-bar .send-btn') as HTMLElement)
    await act(async () => {
    })
    const pending = document.querySelector('.user-bubble.sending')
    expect(pending).toBeTruthy()
    expect(pending?.textContent).toContain('请调整方向')
    expect(pending?.textContent).toContain('发送中')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'userMessage', taskId: 't1', stepId: 's1', message: '请调整方向', seq: 21})
    await act(async () => {
    })
    expect(document.querySelector('.user-bubble.sending')).toBeNull()
    expect(api.getStep).toHaveBeenCalledTimes(2)
  })

  it('thinkingChunk 流式展示思考过程（think-box），streamEnd 后清除', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    expect(document.querySelectorAll('.think-box').length).toBe(1)
    expect(document.querySelector('.think-box')?.textContent).not.toContain('先分析')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '先分析', seq: 31})
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '再推理', seq: 32})
    await act(async () => {
    })

    const boxes = document.querySelectorAll('.think-box')
    expect(boxes.length).toBe(2)
    expect(boxes[1]?.textContent).toContain('先分析再推理')

    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 33})
    await act(async () => {
    })
    expect(document.querySelectorAll('.think-box').length).toBe(1)
    expect(document.querySelector('.think-box')?.textContent).not.toContain('先分析')
  })

  it('J1/J2：多轮思考只显示当前请求——旧轮次思考随工具执行清空，工具卡保留', async () => {
    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData, conversation: [], messages: [], max_seq: -1,
    } as never)
    renderStep()
    const es = MockEventSource.instances[0]

    act(() => {
      fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考A', seq: 21})
    })
    expect(document.querySelector('.think-box')?.textContent).toContain('思考A')

    act(() => {
      fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'cX', toolName: 'read_file', input: '{}', seq: 22})
    })
    const boxA = document.querySelector('.think-box')
    const toolX = document.querySelector('.ai-tool-inline')
    expect(boxA && toolX &&
        (boxA.compareDocumentPosition(toolX) & Node.DOCUMENT_POSITION_FOLLOWING)).toBeTruthy()

    act(() => {
      fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callId: 'cX', seq: 23})
    })
    expect(document.querySelector('.think-box')).toBeNull()
    expect(document.querySelectorAll('.ai-tool-inline').length).toBe(1)

    act(() => {
      fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考B', seq: 24})
    })
    let boxes = document.querySelectorAll('.think-box')
    expect(boxes.length).toBe(1)
    expect(boxes[0].textContent).toContain('思考B')
    expect(boxes[0].textContent).not.toContain('思考A')

    act(() => {
      fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'cY', toolName: 'list_dir', input: '{}', seq: 25})
      fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callId: 'cY', seq: 26})
    })
    expect(document.querySelector('.think-box')).toBeNull()
    expect(document.querySelectorAll('.ai-tool-inline').length).toBe(2)

    act(() => {
      fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考C', seq: 27})
    })
    boxes = document.querySelectorAll('.think-box')
    expect(boxes.length).toBe(1)
    expect(boxes[0].textContent).toContain('思考C')
    expect(boxes[0].textContent).not.toContain('思考B')
    expect(document.querySelectorAll('.ai-tool-inline').length).toBe(2)

    const tools = document.querySelectorAll('.ai-tool-inline')
    const lastTool = tools[tools.length - 1]
    expect(lastTool.compareDocumentPosition(boxes[0]) &
        Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('V-18: 工具参数流式累积动画 + toolExecuting 状态条 + toolCallResult 后恢复「AI 正在思考」', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c8', toolName: 'run_cmd', input: '', seq: 41})
    fire(es, {command: 'toolCallParam', taskId: 't1', stepId: 's1', callId: 'c8', delta: '{"command":', seq: 42})
    await act(async () => {
    })
    const livePanel = () => document.querySelectorAll('.tool-panel')[1]
    expect(livePanel()?.textContent).toContain('{"command":')
    fire(es, {command: 'toolCallParam', taskId: 't1', stepId: 's1', callId: 'c8', delta: ' "dir"}', seq: 43})
    await act(async () => {
    })
    expect(livePanel()?.textContent).toContain('{"command": "dir"}')

    fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callIds: ['c8'], toolNames: ['run_cmd'], seq: 44})
    await act(async () => {
    })
    expect(screen.queryByText(/正在执行工具/)).toBeNull()
    const runningPanel = document.querySelectorAll('.tool-panel')[1]
    expect(runningPanel?.querySelector('.loading-spinner')).toBeTruthy()

    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c8', toolName: 'run_cmd', output: 'ok', seq: 45})
    await act(async () => {
    })
    expect(screen.getByText(/AI 正在思考/)).toBeTruthy()
  })

  it('V-19: 思考只保留当前请求——工具执行清思考但文本保留定稿（2026-08-23 用户反馈：AI 调工具时文本“快速消失”）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '第一轮思考', seq: 51})
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '第一轮文本', seq: 52})
    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', input: '{"command":"x"}', seq: 53})
    fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callIds: ['c9'], toolNames: ['run_cmd'], seq: 54})
    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', output: 'ok', seq: 55})
    await act(async () => {
    })

    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '第二轮思考', seq: 56})
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '第二轮文本', seq: 57})
    await act(async () => {
    })

    const boxes = document.querySelectorAll('.think-box')
    expect(boxes.length).toBe(2)
    expect(boxes[1]?.textContent).toContain('第二轮思考')
    expect(boxes[1]?.textContent).not.toContain('第一轮思考')

    const textBlocks = Array.from(document.querySelectorAll('.ai-block .ai-text'))
        .map((el) => el.textContent ?? '')
        .filter((t) => t.includes('文本'))
    expect(textBlocks.length).toBe(2)
    expect(textBlocks[0]).toContain('第一轮文本')
    expect(textBlocks[1]).toContain('第二轮文本')

    const streamPanels = Array.from(document.querySelectorAll('.tool-panel')).filter((p) =>
        p.textContent?.includes('执行命令'),
    )
    expect(streamPanels.length).toBe(1)
    expect(streamPanels[0]?.className).toContain('expanded')
  })

  it('toolExecuting 后已流式文本保留（md 定稿渲染）——归档轮不再清空（2026-08-23）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '# 标题一\n\n**加粗**', seq: 71})
    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', input: '{"command":"x"}', seq: 72})
    fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callIds: ['c9'], toolNames: ['run_cmd'], seq: 73})
    await act(async () => {
    })

    expect(document.querySelector('.ai-block h1')?.textContent).toContain('标题一')

    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', output: 'ok', seq: 74})
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '## 标题二', seq: 75})
    await act(async () => {
    })
    const allText = document.querySelectorAll('.ai-block .ai-text')
    expect(allText[allText.length - 1]?.textContent).toContain('## 标题二')
  })

  it('streamEnd 后 getStep 未返回时流式块保留，返回后才清（2026-08-23 刷新才出现修复）', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '最终文本', seq: 81})
    await act(async () => {
    })
    expect(document.body.textContent).toContain('最终文本')

    let resolveGet: (d: StepData) => void
    vi.mocked(api.getStep).mockImplementationOnce(
        () => new Promise<StepData>((res) => {
          resolveGet = res
        }),
    )
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 82})
    await act(async () => {
    })

    expect(document.body.textContent).toContain('最终文本')

    await act(async () => {
      resolveGet!(stepData)
    })
    expect(document.body.textContent).not.toContain('最终文本')
    expect(document.body.textContent).toContain('AI 回答')
  })

  it('userMessage 事件后 getStep 未返回时“发送中…”气泡保留，返回后才转正（2026-08-23）', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'pending', required: true},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')
    expect(document.querySelector('.user-bubble.sending')).toBeNull()

    vi.mocked(api.stepIntervene).mockImplementation(
        () => new Promise((res) => setTimeout(() => res({}), 100)) as ReturnType<typeof api.stepIntervene>,
    )
    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '请调整方向'}})
    fireEvent.click(document.querySelector('.intervene-bar .send-btn') as HTMLElement)
    await act(async () => {
    })
    expect(document.querySelector('.user-bubble.sending')?.textContent).toContain('请调整方向')

    let resolveGet: (d: StepData) => void
    vi.mocked(api.getStep).mockImplementationOnce(
        () => new Promise<StepData>((res) => {
          resolveGet = res
        }),
    )
    const es = MockEventSource.instances[0]
    fire(es, {command: 'userMessage', taskId: 't1', stepId: 's1', message: '请调整方向', seq: 91})
    await act(async () => {
    })
    expect(document.querySelector('.user-bubble.sending')).toBeTruthy()

    await act(async () => {
      resolveGet!(stepData)
    })
    expect(document.querySelector('.user-bubble.sending')).toBeNull()
  })

  it('V-19: 工具卡展开规则——更早完成的折叠，最近完成的一张 + 执行中的保持展开', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]

    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', input: '{"command":"a"}', seq: 61})
    fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 's1', callIds: ['c9'], toolNames: ['run_cmd'], seq: 62})
    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'run_cmd', output: 'A', seq: 63})
    await act(async () => {
    })

    fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c10', toolName: 'run_cmd', input: '{"command":"b"}', seq: 64})
    await act(async () => {
    })

    const runPanels = Array.from(document.querySelectorAll('.tool-panel')).filter((p) =>
        p.textContent?.includes('执行命令'),
    )
    expect(runPanels.length).toBe(2)

    expect(runPanels[0]?.className).toContain('expanded')
    expect(runPanels[1]?.className).toContain('expanded')

    fire(es, {command: 'toolCallResult', taskId: 't1', stepId: 's1', callId: 'c10', toolName: 'run_cmd', output: 'B', seq: 65})
    await act(async () => {
    })
    const afterPanels = Array.from(document.querySelectorAll('.tool-panel')).filter((p) =>
        p.textContent?.includes('执行命令'),
    )
    expect(afterPanels.length).toBe(2)
    expect(afterPanels[0]?.className).not.toContain('expanded')
    expect(afterPanels[1]?.className).toContain('expanded')
  })

  it('V-19: 工具卡内部滚动——自动滚动开启时参数流式追加 → pre 滚动到底', async () => {

    const setterSpy = vi.spyOn(HTMLPreElement.prototype, 'scrollTop', 'set')
    try {
      renderStep()
      await screen.findByText('AI 回答')
      const es = MockEventSource.instances[0]
      fire(es, {command: 'toolCallStart', taskId: 't1', stepId: 's1', callId: 'c9', toolName: 'write_file', input: '', seq: 71})
      fire(es, {command: 'toolCallParam', taskId: 't1', stepId: 's1', callId: 'c9', delta: '{"path":', seq: 72})
      await act(async () => {
      })
      expect(setterSpy).toHaveBeenCalled()
    } finally {
      setterSpy.mockRestore()
    }
  })

  it('Token 展示：上下文占用条 + token 明细（当前上下文 context_tokens + 累计明细）', async () => {

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            context_tokens: 150000, token_prompt: 400000, token_cached: 80000, token_completion: 5000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()

    expect(await screen.findByText('150K / 1.0M')).toBeTruthy()
    expect(screen.getByText('14%')).toBeTruthy()

    const row = document.querySelector('.tus-row')?.textContent ?? ''
    expect(row).toContain('未缓存输入 320.0K')
    expect(row).toContain('缓存输入 80.0K')
    expect(row).toContain('(20%)')
    expect(row).toContain('输出 5.0K')
  })

  it('Token 展示：无用量时显示占位 --（不渲染 0%）', async () => {
    renderStep()
    expect(await screen.findByText('-- / 1.0M')).toBeTruthy()
    expect(document.querySelector('.tus-row')?.textContent).toContain('--')
    expect(document.querySelector('.tus-row')?.textContent).not.toContain('(0%)')
  })

  it('J-K3 Token 展示：消耗金额——model_tier=light 用 light 组价格（无单位）', async () => {

    vi.mocked(api.getConfig).mockResolvedValue({
      baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
      projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
      contextWindow: 1_048_576,
      lightInputPrice: 0.5, lightCachedPrice: 0.1, lightOutputPrice: 2,
      powerInputPrice: 5, powerCachedPrice: 1, powerOutputPrice: 15,
    })
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            model_tier: 'light',
            context_tokens: 150000, token_prompt: 400000, token_cached: 80000, token_completion: 5000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await waitFor(() =>
        expect(document.querySelector('.tus-row')?.textContent).toContain('输出 5.0K'))

    expect(document.querySelector('.tus-row')?.textContent).toContain('消耗金额 0.178')
    expect(document.querySelector('.tus-row')?.textContent).not.toContain('$')
  })

  it('J-K3 Token 展示：消耗金额——model_tier 缺省回退 power 组价格', async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
      projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
      contextWindow: 1_048_576,
      lightInputPrice: 0.5, lightCachedPrice: 0.1, lightOutputPrice: 2,
      powerInputPrice: 5, powerCachedPrice: 1, powerOutputPrice: 15,
    })
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            context_tokens: 150000, token_prompt: 400000, token_cached: 80000, token_completion: 5000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await waitFor(() =>
        expect(document.querySelector('.tus-row')?.textContent).toContain('输出 5.0K'))

    expect(document.querySelector('.tus-row')?.textContent).toContain('消耗金额 1.75')
  })

  it('J-K3 Token 展示：价格未配置（全 0）→ 消耗金额 0.0000', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            context_tokens: 150000, token_prompt: 400000, token_cached: 80000, token_completion: 5000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await waitFor(() =>
        expect(document.querySelector('.tus-row')?.textContent).toContain('输出 5.0K'))
    expect(document.querySelector('.tus-row')?.textContent).toContain('消耗金额 0.0000')
  })

  it('分页：truncated 显示「加载更早消息」，点击往前翻页 prepend 到列表头部', async () => {
    const recent: Message[] = [
      {role: 'assistant', content: 'msg 2', seq: 2},
      {role: 'assistant', content: 'msg 3', seq: 3},
    ]
    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData,
      total: 4,
      truncated: true,
      conversation: recent,
      messages: recent,
      max_seq: 3,
    })
    renderStep()

    expect(await screen.findByText(/加载更早消息（共 4 条，已显示 2 条）/)).toBeTruthy()

    const earlier: Message[] = [
      {role: 'assistant', content: 'msg 0', seq: 0},
      {role: 'assistant', content: 'msg 1', seq: 1},
    ]
    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData,
      total: 4,
      truncated: false,
      conversation: earlier,
      messages: earlier,
      max_seq: 3,
    })
    fireEvent.click(screen.getByText(/加载更早消息/))
    expect(await screen.findByText('msg 0')).toBeTruthy()
    expect(screen.getByText('msg 3')).toBeTruthy()
    expect(api.getStep).toHaveBeenCalledWith('t1', 's1', {limit: 200, beforeSeq: 2})
  })

  it('已完成步骤发送 → 弹确认框（告知清除后续消息），确认才发送、取消不发送', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'completed', required: true, sort_order: 1},
          {step_id: 's2', title: '测试', status: 'completed', required: true, sort_order: 2},
          {step_id: 's3', title: '验收', status: 'completed', required: true, sort_order: 3},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')

    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '继续优化'}})
    fireEvent.click(document.querySelector('.intervene-bar .send-btn') as HTMLElement)
    await act(async () => {
    })

    expect(screen.getByText('重置后续流程？')).toBeTruthy()
    expect(document.querySelector('.modal-desc')?.textContent).toContain('清除')
    const modal = document.querySelector('.modal') as HTMLElement
    expect(modal?.textContent).toContain('测试')
    expect(modal?.textContent).toContain('验收')
    expect(api.stepIntervene).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('取消'))
    await act(async () => {
    })
    expect(api.stepIntervene).not.toHaveBeenCalled()
    expect(screen.queryByText('重置后续流程？')).toBeNull()

    fireEvent.change(textarea, {target: {value: '继续优化'}})
    fireEvent.click(document.querySelector('.intervene-bar .send-btn') as HTMLElement)
    await act(async () => {
    })
    fireEvent.click(screen.getByText('确认发送并重置'))
    await act(async () => {
    })
    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 's1', 'send', '继续优化')
  })

  it('执行中（active）：顶栏无暂停按钮，输入框「终止当前输出」可用（基于 active 状态）→ stop', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    expect(document.querySelector('.run-toggle-btn')).toBeNull()

    const stopBtn = document.querySelector('.intervene-bar .stop-btn') as HTMLButtonElement
    expect(stopBtn).toBeTruthy()
    fireEvent.click(stopBtn)
    await act(async () => {
    })
    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 's1', 'stop', '打断')
  })

  it('stopped：顶栏显示「恢复」→ resumeStep + startTask 重启执行循环', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'stopped', required: true},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')
    const resumeBtn = document.querySelector('.run-toggle-btn') as HTMLButtonElement
    expect(resumeBtn?.textContent).toContain('恢复')
    fireEvent.click(resumeBtn)
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('t1', 's1')
    expect(api.startTask).toHaveBeenCalledWith('t1')
  })

  it('pending + 任务暂停（流程暂停打断后）：顶栏显示「恢复」→ 直接 startTask 重启执行循环', async () => {

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        status: 'paused',
        steps: [
          {step_id: 's1', title: '实现功能', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')
    const resumeBtn = document.querySelector('.run-toggle-btn') as HTMLButtonElement
    expect(resumeBtn?.textContent).toContain('恢复')
    fireEvent.click(resumeBtn)
    await act(async () => {
    })

    expect(api.startTask).toHaveBeenCalledWith('t1')
    expect(api.resumeStep).not.toHaveBeenCalled()
  })

  it('gate 审批类（AI 未输出决策包）：消息流末尾渲染通过/拒绝，approve → approveGate；拒绝 → 必填原因后 rejectGate', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        status: 'paused',
        steps: [
          {step_id: 's1', title: '人工决策：是否接受当前成果', status: 'active', required: true, human_attention: 'gate'},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()

    await screen.findByText('人工决策：是否接受当前成果')

    const card = document.querySelector('.gate-review-card')
    expect(card).toBeTruthy()
    expect(card?.textContent).toContain('人工审批')
    expect(card?.textContent).toContain('AI 回答')
    const btns = Array.from(card?.querySelectorAll('button') ?? [])
    const approveBtn = btns.find((b) => b.textContent?.includes('审批通过')) as HTMLButtonElement
    const rejectBtn = btns.find((b) => b.textContent?.includes('拒绝')) as HTMLButtonElement
    expect(approveBtn).toBeTruthy()
    expect(rejectBtn).toBeTruthy()
    expect(btns.some((b) => b.textContent?.includes('按所选选项继续'))).toBe(false)

    fireEvent.click(approveBtn)
    await act(async () => {
    })
    expect(api.approveGate).toHaveBeenCalledWith('t1', 's1')

    fireEvent.click(rejectBtn)
    await act(async () => {
    })
    const modal = document.querySelector('.modal-overlay.show')
    expect(modal).toBeTruthy()
    const confirmBtn = modal?.querySelector('.primary') as HTMLButtonElement
    expect(confirmBtn?.hasAttribute('disabled')).toBe(true)
    const ta = modal?.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '需要重新求解'}})
    await act(async () => {
    })
    expect(confirmBtn?.hasAttribute('disabled')).toBe(false)
    fireEvent.click(confirmBtn)
    await act(async () => {
    })
    expect(api.rejectGate).toHaveBeenCalledWith('t1', 's1', '需要重新求解')
  })

  it('gate 待审批：决策请求包聊天流 markdown 展示（无卡片）+ 选项操作区，选中选项发送给 AI', async () => {
    const pkgJson = JSON.stringify(
        {
          options: [
            {option: '继续等待调查结果', cost: '时间', risk: '无', pros: '可能突破'},
            {option: '投入外部算力', cost: '数小时', risk: '概率未知', pros: '唯一剩余路径'},
            {option: '接受现状收尾', cost: 'flag 未解出', risk: '无', pros: '立即结束'},
          ],
          recommendation: '优先 A，无突破则 B，不愿投入则 C',
          questions: ['Q1: 调查结果是什么？', 'Q2: 是否授权超算？'],
        },
        null,
        2,
    )
    vi.mocked(api.getStep).mockResolvedValue({
      ...stepData,
      conversation: [
        ...stepData.conversation,
        {role: 'assistant', content: `决策请求包如下：\n\n\`\`\`json\n${pkgJson}\n\`\`\``, seq: 6},
      ],
    })
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        status: 'paused',
        steps: [
          {step_id: 's1', title: '人工决策：是否接受当前成果', status: 'active', required: true, human_attention: 'gate'},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('人工决策：是否接受当前成果')

    const dcard = document.querySelector('.decision-card')
    expect(dcard).toBeNull()
    const chat = document.querySelector('.chat-log')
    expect(chat?.textContent).toContain('决策请求包如下：')
    expect(chat?.textContent).not.toContain('为什么需要人类拍板')
    expect(chat?.textContent).not.toContain('"options"')

    expect(chat?.textContent).not.toContain('```json')

    const action = document.querySelector('.gate-action-card')
    expect(action).toBeTruthy()
    const opts = document.querySelectorAll('.gate-option')
    expect(opts.length).toBe(3)

    const actionBtns = Array.from(action?.querySelectorAll('button') ?? [])
    expect(actionBtns.some((b) => b.textContent?.includes('审批通过'))).toBe(false)
    expect(actionBtns.some((b) => b.textContent?.includes('拒绝'))).toBe(false)

    const customTa = action?.querySelector('.gate-custom textarea') as HTMLTextAreaElement
    expect(customTa).toBeTruthy()

    fireEvent.click(opts[1] as HTMLButtonElement)
    await act(async () => {
    })
    expect((opts[1] as HTMLElement).className).toContain('selected')
    const btns = Array.from(document.querySelectorAll('.gate-action-card .gate-review-actions button'))
    const chooseBtn = btns.find((b) => b.textContent?.includes('按所选选项继续')) as HTMLButtonElement
    expect(chooseBtn?.hasAttribute('disabled')).toBe(false)
    fireEvent.click(chooseBtn)
    await act(async () => {
    })
    const modal = document.querySelector('.modal-overlay.show')
    expect(modal?.textContent).toContain('投入外部算力')
    const note = modal?.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.change(note, {target: {value: '授权，用云服务器'}})
    await act(async () => {
    })
    fireEvent.click(modal?.querySelector('.primary') as HTMLButtonElement)
    await act(async () => {
    })

    expect(api.approveGate).toHaveBeenCalledWith(
        't1',
        's1',
        expect.stringContaining('【决策选择】用户选择选项 B：投入外部算力'),
    )
    expect(api.approveGate).toHaveBeenCalledWith(
        't1',
        's1',
        expect.stringContaining('补充说明：授权，用云服务器'),
    )
    expect(api.stepIntervene).not.toHaveBeenCalled()
    expect(document.querySelector('.modal-overlay.show')).toBeNull()

    const customTa2 = action?.querySelector('.gate-custom textarea') as HTMLTextAreaElement
    fireEvent.change(customTa2, {target: {value: '投入超算，预算上限 500 元'}})
    await act(async () => {
    })
    const chooseBtn2 = Array.from(
        document.querySelectorAll('.gate-action-card .gate-review-actions button'),
    ).find((b) => b.textContent?.includes('按所选选项继续')) as HTMLButtonElement
    expect(chooseBtn2?.hasAttribute('disabled')).toBe(false)
    fireEvent.click(chooseBtn2)
    await act(async () => {
    })
    expect(document.querySelector('.modal-overlay.show')).toBeNull()
    expect(api.approveGate).toHaveBeenCalledWith(
        't1',
        's1',
        expect.stringContaining('【决策选择】用户自定义决策：投入超算，预算上限 500 元'),
    )

    fireEvent.change(customTa2, {target: {value: '自定义 X'}})
    await act(async () => {
    })
    fireEvent.click(opts[0] as HTMLButtonElement)
    await act(async () => {
    })
    expect((customTa2 as HTMLTextAreaElement).value).toBe('')
  })

  it('非 gate 步骤（active 非 gate）：不渲染审批区域', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    expect(document.querySelector('.gate-review-card')).toBeNull()
  })

  it('自动滚动开关：输入框旁显示，默认开（锁定），点击切换 on 态', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const toggle = document.querySelector('.intervene-bar .autoscroll-toggle') as HTMLButtonElement
    expect(toggle).toBeTruthy()
    expect(toggle?.className).toContain('on')
    fireEvent.click(toggle)
    expect(toggle?.className).not.toContain('on')
    fireEvent.click(toggle)
    expect(toggle?.className).toContain('on')
  })

  it('「回到底部」悬浮按钮：接近底部隐藏，滚离后显示，点击回底并隐藏', async () => {
    renderStep()
    await screen.findByText('AI 回答')

    expect(document.querySelector('.scroll-jump-btn')).toBeNull()

    const area = document.querySelector('.content-area') as HTMLElement
    Object.defineProperty(area, 'scrollHeight', {value: 2000, configurable: true})
    Object.defineProperty(area, 'clientHeight', {value: 400, configurable: true})
    area.scrollTop = 300
    await act(async () => {
      fireEvent.scroll(area)
    })
    const jump = document.querySelector('.scroll-jump-btn') as HTMLButtonElement
    expect(jump).toBeTruthy()
    expect(jump?.querySelector('svg')).toBeTruthy()

    await act(async () => {
      fireEvent.click(jump)
    })
    expect(area.scrollTop).toBe(2000)
    expect(document.querySelector('.scroll-jump-btn')).toBeNull()
  })

  it('对话/代码 tab：顶栏 segmented 按钮组，点击「代码」显示文件树，点击「对话」回到对话', async () => {
    vi.mocked(api.fsTree).mockResolvedValue({path: '', entries: []} as never)
    renderStep()
    await screen.findByText('AI 回答')

    expect(document.querySelector('.top-bar .seg-tabs')).toBeTruthy()
    expect(screen.getByText('对话')).toBeTruthy()
    expect(screen.getByText('代码')).toBeTruthy()

    fireEvent.click(screen.getByText('代码'))
    await act(async () => {
    })
    expect(document.querySelector('.flow-code-split')).toBeTruthy()
    expect(document.querySelector('.file-tree')).toBeTruthy()
    expect(api.fsTree).toHaveBeenCalledWith('')
    expect(document.querySelector('.content-area')).toBeNull()
    expect(document.querySelector('.step-footer')).toBeNull()

    fireEvent.click(screen.getByText('对话'))
    await act(async () => {
    })
    expect(document.querySelector('.content-area')).toBeTruthy()
    expect(document.querySelector('.flow-code-split')).toBeNull()
  })

  it('统计栏：多轮 LLM 流（工具轮）→ 请求数按 streamEnd 计数、输出时长=API 调用开始→流结束（含首字等待）、首字延迟采样', async () => {

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'active', required: true, token_completion: 1200, token_prompt: 1200},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })

    vi.useFakeTimers()
    renderStep()

    await act(async () => {
    })

    expect(document.querySelector('.tus-metrics')).toBeTruthy()
    expect(document.querySelector('.tus-metrics')?.textContent).toContain('--')

    const es = MockEventSource.instances[0]

    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 's1', seq: 1})
    vi.setSystemTime(2_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '第', seq: 2})
    vi.setSystemTime(3_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '一轮', seq: 3})
    vi.setSystemTime(9_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 4})
    await act(async () => {
    })

    vi.setSystemTime(10_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '第', seq: 5})
    vi.setSystemTime(15_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '二轮', seq: 6})
    vi.setSystemTime(20_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 7})
    await act(async () => {
    })

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            token_completion: 1200, token_prompt: 1200, requests: 2,
            output_duration_ms: 6000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    vi.useRealTimers()

    const metrics = document.querySelector('.tus-metrics')
    expect(metrics).toBeTruthy()
    const text = metrics?.textContent ?? ''
    expect(text).toContain('请求数')
    expect(text).toContain('2')

    expect(text).toContain('1.000秒')

    expect(text).toContain('200.0 token/s')

    expect(text).toContain('0分19秒')

    expect(document.querySelectorAll('.tus-metric-sep').length).toBeGreaterThanOrEqual(4)
  })

  it('统计栏：thinkingChunk（思考流，无文本）也采样首字延迟', async () => {
    renderStep()
    await screen.findByText('AI 回答')
    const es = MockEventSource.instances[0]
    vi.useFakeTimers()

    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 's1', seq: 1})
    vi.setSystemTime(2_500)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考', seq: 2})
    vi.setSystemTime(4_000)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '过程', seq: 3})
    vi.setSystemTime(9_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 4})
    await act(async () => {
    })
    vi.useRealTimers()
    const text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('首字延迟')
    expect(text).toContain('1.500秒')
  })

  it('统计栏：无首字（streamEnd 前无 chunk）→ 首字延迟/输出速度显示横线占位 --', async () => {
    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {step_id: 's1', title: '实现功能', status: 'active', required: true, token_completion: 800},
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')

    const es = MockEventSource.instances[0]
    vi.useFakeTimers()
    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 's1', seq: 1})
    vi.setSystemTime(5_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 2})
    await act(async () => {
    })
    vi.useRealTimers()

    const text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('首字延迟')
    expect(text).toContain('--')
    expect(text).toContain('0分04秒')
  })

  it('统计栏：刷新后从 DB 步骤级统计恢复（未运行本会话，读 task_steps 同表字段）', async () => {

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            token_completion: 1200, token_prompt: 1200,
            requests: 5, ttft_total_ms: 3000, ttft_samples: 2,
            output_duration_ms: 6000, run_duration_ms: 123000,
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    renderStep()
    await screen.findByText('AI 回答')

    const text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('请求数 5')
    expect(text).toContain('1.500秒')
    expect(text).toContain('2分03秒')
    expect(text).toContain('200.0 token/s')
  })

  it('统计栏（J-G1）：轮询后端 stats 校准前端', async () => {

    vi.useFakeTimers()
    renderStep()
    await act(async () => {
    })
    const es = MockEventSource.instances[0]

    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 's1', seq: 1})
    vi.setSystemTime(2_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '一', seq: 2})
    vi.setSystemTime(3_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 's1', seq: 3})
    await act(async () => {
    })

    vi.mocked(api.getTask).mockResolvedValue({
      ...taskDetail,
      task: {
        ...taskDetail.task,
        steps: [
          {
            step_id: 's1', title: '实现功能', status: 'active', required: true,
            token_completion: 4000, token_prompt: 2000,
            requests: 5, ttft_total_ms: 3000, ttft_samples: 2,
            output_duration_ms: 8000, run_duration_ms: 50000
          },
          {step_id: 's2', title: '测试', status: 'pending', required: true},
        ],
      },
    })
    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    vi.useRealTimers()
    const text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('请求数 5')
    expect(text).toContain('1.500秒')
    expect(text).toContain('500.0 token/s')
    expect(text).toContain('0分50秒')
  })

  it('统计栏（J-G2b）：API 请求进行中输出速度实时显示', async () => {
    vi.useFakeTimers()
    renderStep()
    await act(async () => {
    })
    const es = MockEventSource.instances[0]

    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 's1', seq: 1})
    vi.setSystemTime(2_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '第', seq: 2})
    vi.setSystemTime(3_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 's1', chunk: '一轮', seq: 3})
    vi.setSystemTime(4_000)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '思考', seq: 4})
    vi.setSystemTime(5_000)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 's1', chunk: '过程', seq: 5})
    vi.setSystemTime(6_000)
    fire(es, {command: 'toolCallParam', taskId: 't1', stepId: 's1', callId: 'x1', delta: '{"a":1}', seq: 6})
    await act(async () => {
    })

    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    let text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).not.toContain('输出速度 --')
    expect(text).toContain('1.6 token/s')

    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('1.3 token/s')
    vi.useRealTimers()
  })
})
