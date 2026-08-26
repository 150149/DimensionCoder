import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {api} from '../api/client'
import MonitorDetail from '../panels/MonitorDetail'

vi.mock('../api/client', () => ({
  api: {
    listTasks: vi.fn(),
    createTask: vi.fn(),
    startTask: vi.fn(),
    pauseTask: vi.fn(),
    getTask: vi.fn(),
    getMonitorConversations: vi.fn(),
    getStepMessages: vi.fn(),
    monitorControl: vi.fn(),
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

const convWithDecision = [
  {role: 'assistant', content: '审查中', seq: 1},
  {role: 'tool', content: '', toolName: 'dcflow_adjust_flow', tool_call_id: 'm1', input: {action: 'add_steps', reasoning: '需要追加一个测试步骤'}, output: 'ok', seq: 2},
  {role: 'assistant', content: '已调整流程', seq: 3},
]
const convNoChange = [
  {role: 'tool', content: '', toolName: 'dcflow_adjust_flow', tool_call_id: 'm2', input: {action: 'no_change', reasoning: '流程正常无需调整'}, output: 'ok', seq: 1},
]
const convMonitor = [{role: 'assistant', content: '监控中', seq: 1}]

function renderMonitor(stepId: string) {
  return render(
      <MemoryRouter initialEntries={[`/task/t1/monitor/${stepId}`]}>
        <MonitorDetail taskId="t1" stepId={stepId}/>
      </MemoryRouter>,
  )
}

beforeEach(() => {

  vi.mocked(api.getMonitorConversations).mockResolvedValue({
    task_id: 't1',
    monitor_conversations: {
      'monitor-2': convWithDecision,
      'monitor-3': convNoChange,
      'monitor-init': convMonitor,
    },
  })

  vi.mocked(api.getStepMessages).mockResolvedValue({messages: [], max_seq: -1, after_seq: -1})
  vi.mocked(api.getTask).mockResolvedValue({
    task: {
      id: 't1', title: 'T', status: 'active', pause_level: null,
      steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
    },
  } as never)
  vi.mocked(api.monitorControl).mockResolvedValue({status: 'ok', task_id: 't1', action: 'stop'})
  vi.mocked(api.getConfig).mockResolvedValue({
    baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
    projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
    contextWindow: 400000,
  })

  vi.mocked(api.getStep).mockResolvedValue({} as never)
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {

  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('MonitorDetail', () => {
  it('决策卡渲染：dcflow_adjust_flow + action≠no_change → .decision-card（高亮 action + reasoning）', async () => {

    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convWithDecision, max_seq: 3, after_seq: -1,
    })
    renderMonitor('monitor-2')
    expect(await screen.findByText('审查中')).toBeTruthy()

    const card = document.querySelector('.decision-card')
    expect(card).toBeTruthy()
    expect(card?.querySelector('.dc-label')?.textContent).toContain('add_steps')
    expect(card?.querySelector('.dc-reasoning')?.textContent).toContain('需要追加一个测试步骤')

    expect(document.querySelector('.tool-panel')?.textContent).toContain('调整流程')
  })

  it('M9：no_change 不渲染决策卡（仅通过对话可见）', async () => {
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convNoChange, max_seq: 1, after_seq: -1,
    })
    renderMonitor('monitor-3')

    expect(await screen.findByText(/"action": "no_change"/)).toBeTruthy()

    expect(document.querySelector('.decision-card')).toBeNull()
    expect(document.querySelector('.tool-panel')).toBeTruthy()
  })

  it('路由直用实体 id（去绑定 2026-08-21）：step-9 原样查询，无映射重定向；无该实例行 → 空状态', async () => {
    renderMonitor('step-9')

    expect(await screen.findByText(/该步骤暂无 Monitor 对话记录/)).toBeTruthy()
    expect(screen.queryByText('监控中')).toBeNull()

    expect(api.getStepMessages).toHaveBeenCalledWith('t1', 'step-9')
  })

  it('路由直用实体 id：monitor-2 页标题「Monitor 审查 monitor-2」+ 事件按实体 id 匹配', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {review: convWithDecision},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convWithDecision, max_seq: 3, after_seq: -1,
    })
    renderMonitor('monitor-2')
    expect(await screen.findByText(/Monitor 审查 monitor-2/)).toBeTruthy()

    expect(api.getStepMessages).toHaveBeenCalledWith('t1', 'monitor-2')

    const es = MockEventSource.instances[0]
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-2', seq: 5})
    await act(async () => {
    })
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-2', chunk: '最终审查结论', seq: 6})
    await act(async () => {
    })
    expect(screen.getByText('最终审查结论')).toBeTruthy()

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-init', chunk: '不该显示', seq: 7})
    await act(async () => {
    })
    expect(screen.queryByText('不该显示')).toBeNull()
  })

  it('纯文本分段：工具开始执行清空流式文本块，工具轮间不叠加（2026-08-21 与 thinking 同款修复）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-init': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({messages: [], max_seq: -1, after_seq: -1})
    renderMonitor('monitor-init')
    await act(async () => {
    })
    const es = MockEventSource.instances[0]
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-init', seq: 1})
    await act(async () => {
    })
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-init', chunk: '文本A', seq: 2})
    await act(async () => {
    })
    expect(screen.getByText('文本A')).toBeTruthy()

    fire(es, {
      command: 'toolCallStart', taskId: 't1', stepId: 'monitor-init',
      callId: 'c1', toolName: 'dcflow_list_steps', input: {}, seq: 3
    })
    fire(es, {command: 'toolExecuting', taskId: 't1', stepId: 'monitor-init', callId: 'c1', seq: 4})
    await act(async () => {
    })
    expect(screen.queryByText('文本A')).toBeNull()

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-init', chunk: '文本B', seq: 5})
    await act(async () => {
    })
    expect(screen.getByText('文本B')).toBeTruthy()
    expect(screen.queryByText('文本A')).toBeNull()
  })

  it('SSE 匹配：monitor-8 路由精确匹配自己的流式事件（2026-08-21 实体化）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-8': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({messages: [], max_seq: -1, after_seq: -1})
    renderMonitor('monitor-8')
    await act(async () => {
    })
    const es = MockEventSource.instances[0]
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-8', seq: 1})
    await act(async () => {
    })

    expect(screen.getByText('Monitor 正在思考...')).toBeTruthy()
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-8', chunk: '本轮编排输出', seq: 2})
    await act(async () => {
    })
    expect(screen.getByText('本轮编排输出')).toBeTruthy()
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 'monitor-8', seq: 3})
    await act(async () => {
    })
    expect(screen.queryByText('Monitor 正在思考...')).toBeNull()

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-intervene-1', chunk: '介入不该显示', seq: 4})
    await act(async () => {
    })
    expect(screen.queryByText('介入不该显示')).toBeNull()
  })

  it('SSE 匹配：monitor-intervene-1 路由（介入审查）精确匹配介入流式事件', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-intervene-1': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({messages: [], max_seq: -1, after_seq: -1})
    renderMonitor('monitor-intervene-1')
    await act(async () => {
    })
    const es = MockEventSource.instances[0]
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-intervene-1', seq: 1})
    await act(async () => {
    })
    expect(screen.getByText('Monitor 正在思考...')).toBeTruthy()
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-intervene-1', chunk: '介入评估输出', seq: 2})
    await act(async () => {
    })
    expect(screen.getByText('介入评估输出')).toBeTruthy()
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 'monitor-intervene-1', seq: 3})
    await act(async () => {
    })
    expect(screen.queryByText('Monitor 正在思考...')).toBeNull()
  })

  it('数据源单一：介入审查路由只查 monitor-intervene-1 行消息（无 artifact 拼接/无双源）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-intervene-1': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{role: 'assistant', content: '介入评估中：先删掉跳过的步骤', seq: 1}],
      max_seq: 1, after_seq: -1,
    })
    renderMonitor('monitor-intervene-1')

    expect(await screen.findByText('介入评估中：先删掉跳过的步骤')).toBeTruthy()
    expect(api.getStepMessages).toHaveBeenCalledWith('t1', 'monitor-intervene-1')

    expect(api.getStepMessages).toHaveBeenCalledTimes(1)
  })

  it('产出报告路由 report：独立对话 + 标题「产出报告」+ 只匹配 report 事件', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {report: [{role: 'assistant', content: '最终答案：flag{ok}', seq: 1}]},
      step_tokens: {},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{role: 'assistant', content: '最终答案：flag{ok}', seq: 1}],
      max_seq: 1, after_seq: -1,
    })
    renderMonitor('report')

    expect(await screen.findByText(/产出报告/)).toBeTruthy()
    expect(await screen.findByText('最终答案：flag{ok}')).toBeTruthy()

    const es = MockEventSource.instances[0]
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'report', seq: 2})
    await act(async () => {
    })
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'report', chunk: '报告补充内容', seq: 3})
    await act(async () => {
    })
    expect(screen.getByText('报告补充内容')).toBeTruthy()

    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'review', chunk: '审查不该显示', seq: 4})
    await act(async () => {
    })
    expect(screen.queryByText('审查不该显示')).toBeNull()
  })

  it('Token 展示：review 路由 step_tokens 直查（key = 实体 id）（上下文占用 + token 明细）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {review: convWithDecision},
      step_tokens: {
        'monitor-init': {token_prompt: 80000, token_cached: 60000, token_completion: 2000},
        review: {token_prompt: 120000, token_cached: 90000, token_completion: 3000},
      },
    })
    renderMonitor('review')

    expect(await screen.findByText('120K / 400K')).toBeTruthy()
    expect(screen.getByText('30%')).toBeTruthy()

    const row = document.querySelector('.tus-row')?.textContent ?? ''
    expect(row).toContain('未缓存输入 30.0K')
    expect(row).toContain('缓存输入 90.0K')
    expect(row).toContain('(75%)')
    expect(row).toContain('输出 3.0K')
  })

  it('Token 展示：monitor-2 路由 step_tokens 直查', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {'monitor-2': convWithDecision},
      step_tokens: {
        'monitor-2': {token_prompt: 80000, token_cached: 60000, token_completion: 2000},
      },
    })
    renderMonitor('monitor-2')

    expect(await screen.findByText('80K / 400K')).toBeTruthy()
    expect(screen.getByText('20%')).toBeTruthy()
    expect(document.querySelector('.tus-row')?.textContent).toContain('未缓存输入 20.0K')
  })

  it('Token 展示：无 step_tokens 数据时显示占位 --', async () => {
    renderMonitor('monitor-9')
    expect(await screen.findByText('-- / 400K')).toBeTruthy()
    expect(document.querySelector('.tus-row')?.textContent).toContain('--')
  })

  it('J-K3 Token 展示：消耗金额——monitor 用 power 组价格（无单位）', async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
      projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
      contextWindow: 400000,
      lightInputPrice: 0.5, lightCachedPrice: 0.1, lightOutputPrice: 2,
      powerInputPrice: 5, powerCachedPrice: 1, powerOutputPrice: 15,
    })
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {review: convWithDecision},
      step_tokens: {
        review: {token_prompt: 120000, token_cached: 90000, token_completion: 3000},
      },
    })
    renderMonitor('review')
    await waitFor(() =>
        expect(document.querySelector('.tus-row')?.textContent).toContain('输出 3.0K'))

    const row = document.querySelector('.tus-row')?.textContent ?? ''
    expect(row).toContain('消耗金额 0.285')
    expect(row).not.toContain('$')
  })

  it('J-K3 Token 展示：价格未配置（全 0）→ 消耗金额 0.0000', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {'monitor-2': convWithDecision},
      step_tokens: {
        'monitor-2': {token_prompt: 80000, token_cached: 60000, token_completion: 2000},
      },
    })
    renderMonitor('monitor-2')
    await screen.findByText('80K / 400K')
    expect(document.querySelector('.tus-row')?.textContent).toContain('消耗金额 0.0000')
  })

  it('统计栏：step_stats 有请求记录 → 输出速度/首字延迟/运行时长/请求数显示数据', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {'monitor-step-9': convWithDecision},
      step_tokens: {
        'monitor-step-9': {token_prompt: 80000, token_cached: 60000, token_completion: 2000},
      },
      step_stats: {
        'monitor-step-9': {
          run_duration_ms: 30000, output_duration_ms: 4000,
          ttft_total_ms: 5000, ttft_samples: 2, requests: 3,
        },
      },
    })
    renderMonitor('monitor-step-9')
    await screen.findByText('80K / 400K')
    const text = document.querySelector('.tus-metrics')?.textContent ?? ''

    expect(text).toContain('500.0 token/s')
    expect(text).toContain('2.500秒')
    expect(text).toContain('0分30秒')
    expect(text).toContain('请求数')
    expect(text).toContain('3')
  })

  it('实时数据源：实例运行中（active）→ getStepMessages 实时消息（无 artifact 快照拼接）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-step-2': convWithDecision}, step_tokens: {},
      monitor_steps: {'monitor-step-2': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [
        {role: 'user', content: '用户排队消息', seq: 1},
        {role: 'assistant', content: 'Monitor 实时思考', seq: 2},
      ],
      max_seq: 2,
      after_seq: -1,
    })
    renderMonitor('monitor-step-2')
    expect(await screen.findByText('Monitor 实时思考')).toBeTruthy()
    expect(screen.getByText('用户排队消息')).toBeTruthy()

    expect(api.getStepMessages).toHaveBeenCalledWith('t1', 'monitor-step-2')
  })

  it('数据源：实例已完成（completed）→ getStepMessages 完整对话（迁移后行内消息完整）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-step-2': convWithDecision}, step_tokens: {},
      monitor_steps: {'monitor-step-2': 'completed'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convWithDecision, max_seq: 3, after_seq: -1,
    })
    renderMonitor('monitor-step-2')
    expect(await screen.findByText('审查中')).toBeTruthy()

    expect(document.querySelector('.run-toggle-btn')).toBeNull()
  })

  it('介入栏：运行中（实例 active）发送 → 注入当前实例续跑（stepIntervene send）；不弹窗', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-2': 'active'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'active', pause_level: null,
        steps: [{step_id: 'step-2', title: '方案', status: 'active', required: true}],
        artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-2')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    expect(ta).toBeTruthy()
    fireEvent.change(ta, {target: {value: '排队指令'}})
    await act(async () => {
    })

    fireEvent.click(document.querySelector('.queue-btn') as HTMLButtonElement)
    await act(async () => {
    })

    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 'monitor-2', 'send', '排队指令')
    expect(api.flowIntervene).not.toHaveBeenCalledWith('t1', 'rebuild', expect.anything())
  })

  it('介入栏：monitor-init 已完成（实例 completed）发送 → 不弹 rebuild，排队等流程处理', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-init': convMonitor}, step_tokens: {},
      monitor_steps: {'monitor-init': 'completed'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convMonitor, max_seq: 1, after_seq: -1,
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'active', pause_level: null,
        steps: [{step_id: 'step-9', title: '验证', status: 'completed', required: true}],
        artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-init')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '应该尝试使用模拟器进行debug'}})
    await act(async () => {
    })

    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })
    expect(document.querySelector('.modal-overlay.show')).toBeNull()
    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 'monitor-init', 'send', '应该尝试使用模拟器进行debug')
    expect(api.flowIntervene).not.toHaveBeenCalledWith('t1', 'rebuild', expect.anything())
  })

  it('介入栏：任务 completed 发送 → monitor-init 页不弹 rebuild，排队（后端补触发收尾链）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-init': convMonitor}, step_tokens: {},
      monitor_steps: {'monitor-init': 'completed'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convMonitor, max_seq: 1, after_seq: -1,
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'completed', pause_level: null,
        steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-init')
    await screen.findByText('监控中')
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '完成后继续'}})
    await act(async () => {
    })

    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })

    expect(document.querySelector('.modal-overlay.show')).toBeNull()
    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 'monitor-init', 'send', '完成后继续')
    expect(api.flowIntervene).not.toHaveBeenCalledWith('t1', 'rebuild', expect.anything())
  })

  it('介入栏：当前 monitor 步骤已完成（实例 completed，任务仍 active）→ 弹窗确认 → rebuild 传路由步骤', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-9': 'completed'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'active', pause_level: null,
        steps: [
          {step_id: 'step-9', title: '验证', status: 'completed', required: true},
          {step_id: 'step-10', title: '收尾', status: 'pending', required: true},
        ],
        artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-9')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '完成后继续'}})
    await act(async () => {
    })

    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })

    expect(document.querySelector('.modal-overlay.show')).toBeTruthy()
    fireEvent.click(document.querySelector('.modal-overlay.show .primary') as HTMLButtonElement)
    await act(async () => {
    })
    expect(api.flowIntervene).toHaveBeenCalledWith('t1', 'rebuild', '完成后继续', 'monitor-9')
  })

  it('介入栏：步骤未完成（实例未 completed）→ 不弹窗，直接 pending 排队', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'active', pause_level: null,
        steps: [
          {step_id: 'step-9', title: '验证', status: 'active', required: true},
        ],
        artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('step-9')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '排队指令'}})
    await act(async () => {
    })

    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })
    expect(document.querySelector('.modal-overlay.show')).toBeNull()
    expect(api.stepIntervene).toHaveBeenCalledWith('t1', 'step-9', 'send', '排队指令')
  })

  it('介入栏：停止/恢复按钮 → monitorControl stop/resume（传当前页面 id）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-init': 'active'},
    })
    renderMonitor('monitor-init')

    await screen.findByText(/该步骤暂无 Monitor 对话记录/)

    expect(document.querySelector('.run-toggle-btn')).toBeNull()

    const stopBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '终止当前输出',
    )
    fireEvent.click(stopBtn as HTMLButtonElement)
    await act(async () => {
    })

    expect(api.monitorControl).toHaveBeenCalledWith('t1', 'stop', '', 'monitor-init')

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-init': 'stopped'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'paused', pause_level: 'step',
        steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1100))
    })
    const resumeBtn = document.querySelector('.run-toggle-btn') as HTMLButtonElement
    expect(resumeBtn).toBeTruthy()
    fireEvent.click(resumeBtn)
    await act(async () => {
    })
    expect(api.monitorControl).toHaveBeenCalledWith('t1', 'resume', '', 'monitor-init')
  })

  it('已完成 monitor（实例 completed）：任务 paused 也不显示恢复执行按钮', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-2': convWithDecision}, step_tokens: {},
      monitor_steps: {'monitor-2': 'completed'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convWithDecision, max_seq: 3, after_seq: -1,
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'paused', pause_level: 'step',
        steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-2')
    await screen.findByText('审查中')
    expect(document.querySelector('.run-toggle-btn')).toBeNull()
  })

  it('任务 active + 实例 stopped：顶栏显示恢复按钮（2026-08-23：llmError 停摆自救）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-2': 'stopped'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'active', pause_level: null,
        steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-2')
    await act(async () => {
    })
    const resumeBtn = document.querySelector('.run-toggle-btn') as HTMLButtonElement
    expect(resumeBtn).toBeTruthy()
    fireEvent.click(resumeBtn)
    await act(async () => {
    })
    expect(api.monitorControl).toHaveBeenCalledWith('t1', 'resume', '', 'monitor-2')
  })

  it('llmError retryable=false：错误卡显示恢复按钮（2026-08-23：400/unknown 可自救）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-2': 'stopped'},
    })
    renderMonitor('monitor-2')
    await screen.findByText(/该步骤暂无 Monitor 对话记录/)

    const es = MockEventSource.instances[0]
    fire(es, {
      command: 'llmError', taskId: 't1', stepId: 'monitor-2',
      code: 'unknown', message: 'Error code: 400 - Bad Request', retryable: false, retryCount: 0, seq: 9
    })
    await act(async () => {
    })
    expect(document.querySelector('.llm-error-card')).toBeTruthy()
    expect(Array.from(document.querySelectorAll('.llm-error-card button')).some(
        (b) => b.textContent?.includes('重试'))).toBe(false)
    const resumeBtn = Array.from(document.querySelectorAll('.llm-error-card button')).find(
        (b) => b.textContent?.includes('恢复'),
    ) as HTMLButtonElement
    expect(resumeBtn).toBeTruthy()
    fireEvent.click(resumeBtn)
    await act(async () => {
    })
    expect(api.monitorControl).toHaveBeenCalledWith('t1', 'resume', '', 'monitor-2')
  })

  it('介入栏：暂停时发送 → monitorControl resume 自动恢复执行（不排队）', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-init': 'stopped'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'paused', pause_level: 'step',
        steps: [], artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-init')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '暂停后继续'}})
    await act(async () => {
    })

    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })

    expect(api.monitorControl).toHaveBeenCalledWith('t1', 'resume', '暂停后继续', 'monitor-init')
    expect(api.flowIntervene).not.toHaveBeenCalled()
  })

  it('介入栏：flow 暂停（介入中）+ 审查已完成 → 弹窗 rebuild（清理后续，不走 resume）', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-7a': 'completed'},
    })
    vi.mocked(api.getTask).mockResolvedValue({
      task: {
        id: 't1', title: 'T', status: 'paused', pause_level: 'flow',
        steps: [{step_id: 'step-7a', title: '验证', status: 'completed', required: true}],
        artifacts: {}, monitor_conversations: {}, step_messages: {}, recent_events: [],
      },
    } as never)
    renderMonitor('monitor-7a')
    await act(async () => {
    })
    const ta = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(ta, {target: {value: '删掉跳过的步骤'}})
    await act(async () => {
    })
    const sendBtn = Array.from(document.querySelectorAll('.intervene-bar button')).find(
        (b) => (b as HTMLButtonElement).title === '排队等流程完成后Monitor调整',
    )
    fireEvent.click(sendBtn as HTMLButtonElement)
    await act(async () => {
    })
    expect(document.querySelector('.modal-overlay.show')).toBeTruthy()
    expect(api.monitorControl).not.toHaveBeenCalled()
    fireEvent.click(document.querySelector('.modal-overlay.show .primary') as HTMLButtonElement)
    await act(async () => {
    })
    expect(api.flowIntervene).toHaveBeenCalledWith('t1', 'rebuild', '删掉跳过的步骤', 'monitor-7a')
  })

  it('介入栏：压缩按钮 → compressStep(实体 id)（审查步骤为真实行，可直接压缩）', async () => {
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convMonitor, max_seq: 1, after_seq: -1,
    })
    renderMonitor('monitor-init')
    await screen.findByText('监控中')
    vi.mocked(api.compressStep).mockResolvedValue({status: 'ok', original_count: 20, compressed_count: 7})
    fireEvent.click(document.querySelector('.intervene-bar .compress-btn') as HTMLButtonElement)
    await act(async () => {
    })
    expect(api.compressStep).toHaveBeenCalledWith('t1', 'monitor-init')
  })

  it('J3：注入消息展示——step_context 用户气泡按 Markdown 渲染；系统提示由 DB system 消息折叠展示', async () => {
    vi.mocked(api.getStep).mockResolvedValue({
      prep: {
        system_prompt: '你是 Monitor 编排 Agent。\n规则一：先读任务再排步骤。',
        step_context: '# 步骤: 初始编排\n\n## 任务背景\n- 标题: X\n\n```python\nprint(1)\n```\n',
      },
    } as never)

    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{role: 'system', content: '你是 Monitor 编排 Agent。\n规则一：先读任务再排步骤。', seq: 0},
        ...convMonitor],
      max_seq: 2, after_seq: -1,
    })
    renderMonitor('monitor-init')

    expect(await screen.findByRole('heading', {level: 1, name: '步骤: 初始编排'})).toBeTruthy()
    expect(screen.getByText(/标题: X/)).toBeTruthy()
    expect(document.querySelector('.user-bubble code')?.textContent).toContain('print(1)')

    expect(await screen.findByText(/系统提示 \(/)).toBeTruthy()
    expect(document.querySelector('.msg-system')?.classList.contains('collapsed')).toBe(true)
    fireEvent.click(screen.getByText(/系统提示 \(/))
    expect(document.querySelector('.msg-system')?.classList.contains('collapsed')).toBe(false)
    expect(screen.getByText(/规则一/)).toBeTruthy()

    const log = document.querySelector('.chat-log')
    expect(log?.children[0]?.className).toContain('msg-system')
    expect(log?.children[1]?.className).toContain('msg-user')
  })

  it('J4：多轮思考只显示当前请求（工具轮开始清空旧思考，工具卡保留）', async () => {
    renderMonitor('monitor-init')
    const es = MockEventSource.instances[0]

    act(() => {
      fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 'monitor-init', chunk: '思考A', seq: 11})
    })
    expect(document.querySelector('.think-box')?.textContent).toContain('思考A')

    act(() => {
      fire(es, {
        command: 'toolCallStart', taskId: 't1', stepId: 'monitor-init',
        callId: 'cX', toolName: 'dcflow_adjust_flow', input: '{}', seq: 12
      })
    })
    expect(document.querySelector('.think-box')).toBeNull()
    expect(document.querySelectorAll('.ai-tool-inline').length).toBe(1)

    act(() => {
      fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 'monitor-init', chunk: '思考B', seq: 13})
    })
    const boxes = document.querySelectorAll('.think-box')
    expect(boxes.length).toBe(1)
    expect(boxes[0].textContent).toContain('思考B')
    expect(boxes[0].textContent).not.toContain('思考A')
    expect(document.querySelectorAll('.ai-tool-inline').length).toBe(1)

    const toolCard = document.querySelector('.ai-tool-inline')
    expect(toolCard && (toolCard.compareDocumentPosition(boxes[0]) &
        Node.DOCUMENT_POSITION_FOLLOWING)).toBeTruthy()
  })

  it('llmError 重试卡：事件到达 → 错误卡出现；点重试 → resumeStep + startTask', async () => {
    renderMonitor('monitor-init')
    const es = MockEventSource.instances[0]
    act(() => {
      fire(es, {
        command: 'llmError', taskId: 't1', stepId: 'monitor-init', seq: 1,
        code: 'E_LIMIT', message: 'rate limited', retryCount: 2, retryable: true,
      })
    })
    expect(await screen.findByText(/LLM 错误: E_LIMIT/)).toBeTruthy()
    expect(screen.getByText(/已重试 2 次（可重试）/)).toBeTruthy()
    vi.mocked(api.resumeStep).mockResolvedValue({status: 'ok'} as never)
    vi.mocked(api.startTask).mockResolvedValue({status: 'ok'} as never)
    fireEvent.click(screen.getByText('重试'))
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('t1', 'monitor-init')
    expect(api.startTask).toHaveBeenCalledWith('t1')
  })

  it('J-E1：工具参数流式逐片累积进工具卡输入区', async () => {
    renderMonitor('monitor-init')
    const es = MockEventSource.instances[0]

    act(() => {
      fire(es, {
        command: 'toolCallStart', taskId: 't1', stepId: 'monitor-init',
        callId: 'c8', toolName: 'dcflow_adjust_flow', input: '', seq: 41
      })
    })
    const livePanel = () => document.querySelector('.ai-tool-inline .tool-panel')
    act(() => {
      fire(es, {
        command: 'toolCallParam', taskId: 't1', stepId: 'monitor-init',
        callId: 'c8', delta: '{"action":', seq: 42
      })
    })
    expect(livePanel()?.textContent).toContain('{"action":')
    act(() => {
      fire(es, {
        command: 'toolCallParam', taskId: 't1', stepId: 'monitor-init',
        callId: 'c8', delta: ' "add_steps"}', seq: 43
      })
    })
    expect(livePanel()?.textContent).toContain('{"action": "add_steps"}')

    act(() => {
      fire(es, {
        command: 'toolCallResult', taskId: 't1', stepId: 'monitor-init',
        callId: 'c8', toolName: 'dcflow_adjust_flow', output: 'ok', seq: 44
      })
    })
    expect(livePanel()?.textContent).toContain('ok')
  })

  it('J-E2：空参数工具调用显示空对象 {}（不空白）', async () => {
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{
        role: 'tool', content: '', toolName: 'dcflow_adjust_flow',
        tool_call_id: 'c-empty',
        output: '[Error] 缺少 task_id 参数（dcflow_adjust_flow 必须携带 task_id）',
        seq: 15,
      }],
      max_seq: 15, after_seq: -1,
    })
    renderMonitor('monitor-init')

    expect(await screen.findByText(/缺少 task_id 参数/)).toBeTruthy()
    const panels = document.querySelectorAll('.tool-panel')
    expect(panels.length).toBe(1)
    const pre = panels[0].querySelector('.tool-section pre')
    expect(pre?.textContent).toBe('{}')
  })

  it('统计栏（J-G2）：SSE 事件驱动统计实时显示（含请求进行中速度）', async () => {

    vi.useFakeTimers()
    renderMonitor('monitor-2')
    await act(async () => {
    })
    const es = MockEventSource.instances[0]
    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-2', seq: 1})
    vi.setSystemTime(2_500)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 'monitor-2', chunk: '思考', seq: 2})
    vi.setSystemTime(4_000)
    fire(es, {command: 'thinkingChunk', taskId: 't1', stepId: 'monitor-2', chunk: '过程', seq: 3})
    await act(async () => {
    })

    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    let text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).not.toContain('输出速度 --')
    expect(text).toContain('0.8 token/s')

    vi.setSystemTime(9_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 'monitor-2', seq: 4})
    await act(async () => {
    })
    text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('首字延迟')
    expect(text).toContain('1.500秒')
    expect(text).toContain('请求数 1')
    expect(text).toContain('0分08秒')
    vi.useRealTimers()
  })

  it('统计栏（J-G3）：轮询后端 stats 校准前端', async () => {
    vi.useFakeTimers()
    renderMonitor('monitor-2')
    await act(async () => {
    })
    const es = MockEventSource.instances[0]

    vi.setSystemTime(1_000)
    fire(es, {command: 'stepStart', taskId: 't1', stepId: 'monitor-2', seq: 1})
    vi.setSystemTime(2_000)
    fire(es, {command: 'streamChunk', taskId: 't1', stepId: 'monitor-2', chunk: '一', seq: 2})
    vi.setSystemTime(3_000)
    fire(es, {command: 'streamEnd', taskId: 't1', stepId: 'monitor-2', seq: 3})
    await act(async () => {
    })

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1',
      monitor_conversations: {
        'monitor-2': convWithDecision,
        'monitor-3': convNoChange,
        'monitor-init': convMonitor,
      },
      step_stats: {
        'monitor-2': {
          requests: 2, ttft_total_ms: 500, ttft_samples: 1,
          output_duration_ms: 5000, run_duration_ms: 20000
        },
      },
    } as never)
    await act(async () => {
      vi.advanceTimersByTime(1000)
    })
    vi.useRealTimers()
    const text = document.querySelector('.tus-metrics')?.textContent ?? ''
    expect(text).toContain('请求数 2')
    expect(text).toContain('0分20秒')
  })

  it('自动滚动：已完成实例（completed）轮询刷新不滚动', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-2': convWithDecision}, step_tokens: {},
      monitor_steps: {'monitor-2': 'completed'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [
        {role: 'assistant', content: '审查完成', seq: 0},
        {role: 'assistant', content: '结论', seq: 1},
      ],
      max_seq: 1, after_seq: -1,
    })
    vi.useFakeTimers()
    const setterSpy = vi.spyOn(HTMLDivElement.prototype, 'scrollTop', 'set')
    try {
      renderMonitor('monitor-2')
      await act(async () => {
      })

      const afterInitial = setterSpy.mock.calls.length
      expect(afterInitial).toBe(1)

      vi.mocked(api.getStepMessages).mockResolvedValue({
        messages: [
          {role: 'assistant', content: '审查完成', seq: 0},
          {role: 'assistant', content: '结论', seq: 1},
          {role: 'assistant', content: '轮询刷新新增', seq: 2},
        ],
        max_seq: 2, after_seq: -1,
      })
      await act(async () => {
        vi.advanceTimersByTime(1000)
      })
      expect(setterSpy.mock.calls.length).toBe(afterInitial)
    } finally {
      setterSpy.mockRestore()
      vi.useRealTimers()
    }
  })

  it('自动滚动：active 实例消息更新跟随滚动', async () => {
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 't1', monitor_conversations: {'monitor-2': convWithDecision}, step_tokens: {},
      monitor_steps: {'monitor-2': 'active'},
    })
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{role: 'assistant', content: '审查中', seq: 0}],
      max_seq: 0, after_seq: -1,
    })
    vi.useFakeTimers()
    const setterSpy = vi.spyOn(HTMLDivElement.prototype, 'scrollTop', 'set')
    try {
      renderMonitor('monitor-2')
      await act(async () => {
      })
      const before = setterSpy.mock.calls.length
      expect(before).toBeGreaterThanOrEqual(1)

      vi.mocked(api.getStepMessages).mockResolvedValue({
        messages: [
          {role: 'assistant', content: '审查中', seq: 0},
          {role: 'assistant', content: '新输出', seq: 1},
        ],
        max_seq: 1, after_seq: -1,
      })
      await act(async () => {
        vi.advanceTimersByTime(1000)
      })
      expect(setterSpy.mock.calls.length).toBeGreaterThan(before)
    } finally {
      setterSpy.mockRestore()
      vi.useRealTimers()
    }
  })

  it('getStepMessages 失败时保留旧消息，不清空对话（2026-08-23：loadConv 失败闪断修复）', async () => {
    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: convWithDecision, max_seq: 3, after_seq: -1,
    })
    renderMonitor('monitor-2')
    await screen.findByText('审查中')

    vi.mocked(api.getStepMessages).mockRejectedValueOnce(new Error('network'))
    const es = MockEventSource.instances[0]
    fire(es, {command: 'refreshData', taskId: 't1', seq: 5})
    await act(async () => {
    })
    expect(screen.getByText('审查中')).toBeTruthy()
    expect(screen.getByText('已调整流程')).toBeTruthy()

    vi.mocked(api.getStepMessages).mockResolvedValue({
      messages: [{role: 'assistant', content: '审查完成', seq: 4}], max_seq: 4, after_seq: -1,
    })
    fire(es, {command: 'refreshData', taskId: 't1', seq: 6})
    await act(async () => {
    })
    expect(screen.getByText('审查完成')).toBeTruthy()
  })
})
