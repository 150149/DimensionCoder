import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {useEffect} from 'react'
import type {Location} from 'react-router-dom'
import {MemoryRouter, useLocation} from 'react-router-dom'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {TaskDetail, TaskSummary} from '../api/types'
import {api} from '../api/client'
import FlowOverview from '../panels/FlowOverview'

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

const gateTask = {
  id: 'g1',
  type: 'dev-full-flow',
  title: '登录模块',
  status: 'paused',
  created_at: '2024-01-01T10:00:00',
  updated_at: '2024-01-01T12:00:00',
  steps: [
    {step_id: 'step-1', title: '需求分析', status: 'completed', required: true, model_tier: 'power'},
    {step_id: 'step-2', title: '方案设计', status: 'active', required: true, human_attention: 'gate', model_tier: 'light'},
    {step_id: 'step-3', title: '编码实现', status: 'stopped', required: false, model_tier: 'power'},
    {step_id: 'step-4', title: '测试验证', status: 'pending', required: true, model_tier: 'power'},
  ],
}

const activeTask = {
  id: 'a1',
  type: 'dev-full-flow',
  title: '执行中任务',
  status: 'active',
  created_at: '2024-01-01T10:00:00',
  updated_at: '2024-01-01T11:00:00',
  steps: [
    {step_id: 'step-1', title: '分析', status: 'active', required: true, model_tier: 'light'},
    {step_id: 'step-2', title: '实施', status: 'stopped', required: true, model_tier: 'power'},
    {step_id: 'step-3', title: '审查', status: 'pending', required: true, model_tier: 'power'},
  ],
}

function taskData(task: TaskSummary): TaskDetail {
  return {task, artifacts: [], monitor_conversations: {}, step_messages: {}, recent_events: []}
}

function LocationProbe({onLocation}: { onLocation: (loc: Location) => void }) {
  const location = useLocation()
  useEffect(() => {
    onLocation(location)
  }, [location, onLocation])
  return null
}

function renderFlow(taskId = 'g1', onLocation?: (loc: Location) => void) {
  return render(
      <MemoryRouter initialEntries={[`/task/${taskId}`]}>
        {onLocation && <LocationProbe onLocation={onLocation}/>}
        <FlowOverview taskId={taskId}/>
      </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.getTask).mockResolvedValue(taskData(gateTask))
  vi.mocked(api.fsTree).mockResolvedValue({path: '', entries: []})

  vi.mocked(api.getConfig).mockResolvedValue({
    baseUrl: 'http://127.0.0.1:8501', lightModel: 'gpt-4o-mini', powerModel: 'gpt-4o',
    projectRoot: 'workspace', port: 8501, host: '127.0.0.1', hasApiKey: true,
    contextWindow: 1_048_576,
  })
})

afterEach(() => {

  cleanup()
  vi.clearAllMocks()
})

describe('FlowOverview', () => {
  it('渲染进度轨道与状态着色：卡片类/badge/tier/必做可选；Gate 卡按钮齐备', async () => {
    renderFlow()
    expect(await screen.findByText('登录模块')).toBeTruthy()

    expect(document.querySelectorAll('.pt-dot').length).toBe(4)

    const cards = document.querySelectorAll('.flow-card')
    expect(cards.length).toBe(5)
    expect(cards[0].className).not.toContain('card-')
    expect(cards[1].className).toContain('card-gate')
    expect(cards[2].className).toContain('card-stopped')

    expect(cards[3].className).toContain('card-paused')

    expect(cards[4].className).toContain('card-pending')
    expect(cards[4].textContent).toContain('产出报告')
    expect(cards[4].textContent).toContain('待生成')

    expect(screen.getByText('完成')).toBeTruthy()
    expect(screen.getByText('Gate 待审批')).toBeTruthy()

    expect(screen.getByText('已暂停')).toBeTruthy()
    expect(screen.getByText('暂停中')).toBeTruthy()

    expect(screen.getByText('轻量')).toBeTruthy()
    expect(screen.getAllByText('强力').length).toBeGreaterThan(0)
    expect(screen.getAllByText('必做').length).toBeGreaterThan(0)
    expect(screen.getByText('可选')).toBeTruthy()

    expect(screen.getByText('审批通过')).toBeTruthy()
    expect(screen.getByText('拒绝')).toBeTruthy()
    expect(screen.getByText('去审批')).toBeTruthy()
    expect(screen.queryByText('继续')).toBeNull()

    expect(screen.queryByText('重审中')).toBeNull()
  })

  it('Gate 选项类（AI 已输出决策请求包，has_decision_pkg）：卡片显示「去决策」，不显示通过/拒绝', async () => {
    vi.mocked(api.getTask).mockResolvedValue(
        taskData({
          ...gateTask,
          steps: gateTask.steps.map((s) =>
              s.step_id === 'step-2' ? {...s, has_decision_pkg: true} : s,
          ),
        }),
    )
    renderFlow()
    expect(await screen.findByText('去决策')).toBeTruthy()
    expect(screen.queryByText('审批通过')).toBeNull()
    expect(screen.queryByText('拒绝')).toBeNull()
  })

  it('Gate 按钮 approve 调用 approveGate', async () => {
    renderFlow()
    expect(await screen.findByText('审批通过')).toBeTruthy()
    fireEvent.click(screen.getByText('审批通过'))
    await act(async () => {
    })
    expect(api.approveGate).toHaveBeenCalledWith('g1', 'step-2')
  })

  it('Gate 拒绝：弹窗原因必填，确认后调用 rejectGate', async () => {
    renderFlow()
    expect(await screen.findByText('拒绝')).toBeTruthy()
    fireEvent.click(screen.getByText('拒绝'))
    expect(screen.getByText('拒绝审批')).toBeTruthy()
    const confirmBtn = screen.getByText('确认拒绝') as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)
    const textarea = document.querySelector('.modal textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '方案不满足要求'}})
    await act(async () => {
    })
    expect(confirmBtn.disabled).toBe(false)
    fireEvent.click(confirmBtn)
    await act(async () => {
    })
    expect(api.rejectGate).toHaveBeenCalledWith('g1', 'step-2', '方案不满足要求')
  })

  it('未启动任务：顶部「启动任务」按钮 → startTask（P0-5）', async () => {
    vi.mocked(api.getTask).mockResolvedValue(
        taskData({
          id: 'u1',
          type: 'custom',
          title: '未启动任务',
          status: 'active',
          created_at: '2024-01-01T10:00:00',
          updated_at: '2024-01-01T11:00:00',
          steps: [{step_id: 's1', title: '分析', status: 'pending', required: true}],
        }),
    )
    renderFlow('u1')
    expect(await screen.findByText('启动任务')).toBeTruthy()
    fireEvent.click(screen.getByText('启动任务'))
    await act(async () => {
    })
    expect(api.startTask).toHaveBeenCalledWith('u1')
  })

  it('删除任务：确认对话框 → deleteTask → 跳转 /（P2-14）', async () => {
    const onLocation = vi.fn()
    renderFlow('g1', onLocation)
    expect(await screen.findByText('登录模块')).toBeTruthy()
    fireEvent.click(screen.getByText('删除'))
    expect(screen.getByText('将删除任务及全部对话，不可恢复')).toBeTruthy()
    fireEvent.click(screen.getByText('确认删除'))
    await act(async () => {
    })
    expect(api.deleteTask).toHaveBeenCalledWith('g1')
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/')).toBe(true)
  })

  it('重审徽标（P2-15/M7）：paused + active 非 gate → 重审中', async () => {
    vi.mocked(api.getTask).mockResolvedValue(
        taskData({
          id: 'r1',
          type: 'dev-full-flow',
          title: '重审任务',
          status: 'paused',
          created_at: '',
          updated_at: '',
          steps: [
            {step_id: 'step-3', title: '编码实现', status: 'active', required: true, human_attention: 'review'},
            {step_id: 'step-4', title: '测试', status: 'pending', required: true},
          ],
        }),
    )
    renderFlow('r1')
    expect(await screen.findByText('重审中')).toBeTruthy()
  })

  it('J3 任务级操作：active 显示⏸暂停/⏳脉冲，stopped 显示▶恢复全部，逐个 resumeStep', async () => {
    vi.mocked(api.getTask).mockResolvedValue(taskData(activeTask))
    renderFlow('a1')
    expect(await screen.findByText('暂停')).toBeTruthy()
    expect(screen.getByText('恢复全部')).toBeTruthy()

    expect(screen.getByText('正在执行')).toBeTruthy()

    fireEvent.click(screen.getByText('暂停'))
    await act(async () => {
    })
    expect(api.pauseTask).toHaveBeenCalledWith('a1')

    fireEvent.click(screen.getByText('恢复全部'))
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('a1', 'step-2')
  })

  it('恢复全部跳过 stopped 的 gate 步骤（评审项 2026-08-21）', async () => {
    vi.mocked(api.getTask).mockResolvedValue(taskData({
      id: 'r2',
      type: 'dev-full-flow',
      title: '恢复全部跳过 gate',
      status: 'paused',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {
          step_id: 'step-1', title: '决策点', status: 'stopped', required: true,
          human_attention: 'gate', model_tier: 'light'
        },
        {step_id: 'step-2', title: '实施', status: 'stopped', required: true, model_tier: 'power'},
        {step_id: 'step-3', title: '测试', status: 'pending', required: true, model_tier: 'power'},
      ],
    }))
    renderFlow('r2')
    fireEvent.click(await screen.findByText('恢复全部'))
    await act(async () => {
    })

    expect(api.resumeStep).toHaveBeenCalledTimes(1)
    expect(api.resumeStep).toHaveBeenCalledWith('r2', 'step-2')
  })

  it('J3 继续：paused + gate active（审批等待）→ 「去审批」跳转 gate 详情（2026-08-23 不再静默无反应）', async () => {
    const onLocation = vi.fn()
    renderFlow('g1', onLocation)
    expect(await screen.findByText('去审批')).toBeTruthy()
    expect(screen.queryByText('继续')).toBeNull()
    fireEvent.click(screen.getByText('去审批'))
    await act(async () => {
    })

    expect(api.startTask).not.toHaveBeenCalled()
    expect(onLocation).toHaveBeenCalledWith(expect.objectContaining({pathname: '/task/g1/step/step-2'}))
  })

  it('J3 继续：paused + gate pending（未执行）→ 「继续」→ startTask（2026-08-23 不误判审批等待）', async () => {

    vi.mocked(api.getTask).mockResolvedValue(taskData({
      id: 'gp',
      type: 'dev-full-flow',
      title: 'gate 未执行',
      status: 'paused',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-1', title: '分析', status: 'completed', required: true, model_tier: 'light'},
        {step_id: 'step-2', title: '方案', status: 'pending', required: true, model_tier: 'light'},
        {step_id: 'step-3', title: '审批', status: 'pending', required: true, human_attention: 'gate', model_tier: 'light'},
      ],
    }))
    renderFlow('gp')
    expect(await screen.findByText('继续')).toBeTruthy()
    expect(screen.queryByText('去审批')).toBeNull()
    fireEvent.click(screen.getByText('继续'))
    await act(async () => {
    })
    expect(api.startTask).toHaveBeenCalledWith('gp')
  })

  it('J3 继续：paused + stopped（非 gate）→ 恢复 stopped + startTask（2026-08-23 不跳过中断点）', async () => {
    vi.mocked(api.getTask).mockResolvedValue(taskData({
      id: 'rs',
      type: 'dev-full-flow',
      title: '继续恢复 stopped',
      status: 'paused',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-1', title: '分析', status: 'completed', required: true, model_tier: 'light'},
        {step_id: 'step-2', title: '方案', status: 'stopped', required: true, model_tier: 'light'},
        {step_id: 'step-3', title: '实施', status: 'pending', required: true, model_tier: 'power'},
      ],
    }))
    renderFlow('rs')
    fireEvent.click(await screen.findByText('继续'))
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('rs', 'step-2')
    expect(api.startTask).toHaveBeenCalledWith('rs')
  })

  it('stopped 卡片「恢复执行」→ resumeStep；completed 步骤旁 Monitor 链接跳转', async () => {
    const onLocation = vi.fn()

    const orderedGate = {
      ...gateTask,
      steps: gateTask.steps.map((s, i) => ({...s, sort_order: i + 1})),
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(orderedGate))
    renderFlow('g1', onLocation)
    expect(await screen.findByText('恢复执行')).toBeTruthy()
    fireEvent.click(screen.getByText('恢复执行'))
    await act(async () => {
    })
    expect(api.resumeStep).toHaveBeenCalledWith('g1', 'step-3')

    expect(api.startTask).toHaveBeenCalledWith('g1')

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed'},
      monitor_order: {'monitor-1': 1.5},
    })

    await act(async () => {
    })
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })
    fireEvent.click(document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')[0])
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/g1/monitor/monitor-1')).toBe(true)
  })

  it('monitor order 与触发步骤同值：眼睛挂该步骤之后的线段（2026-08-24 用户反馈 10092ff1）', async () => {

    const ordered = {
      ...gateTask,
      status: 'active' as const,
      steps: gateTask.steps.map((s, i) => ({...s, status: 'completed' as const, sort_order: i + 1})),
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(ordered))
    renderFlow('g1')
    await waitFor(() => expect(document.querySelectorAll('.flow-card').length).toBe(5))
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed'},
      monitor_order: {'monitor-1': 3},
    })
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const nodes = document.querySelectorAll('.flow-node')
    expect(nodes[3].querySelectorAll('.flow-link-btn').length).toBe(1)
    expect(nodes[2].querySelectorAll('.flow-link-btn').length).toBe(0)
  })

  it('真实数据回归（10092ff1 快照）：9 个 monitor 眼睛逐线挂触发步骤后的线段（2026-08-24 根因回归）', async () => {

    const onLocation = vi.fn()
    const realTask = {
      id: '10092ff1', type: 'dev-full-flow', title: '修复seal vm跳块缺陷',
      status: 'active' as const,
      created_at: '2026-08-23T09:00:00', updated_at: '2026-08-23T15:41:44',
      steps: [
        {step_id: 'step-1', title: '理解需求与代码现状', status: 'completed' as const, sort_order: 2, required: true, model_tier: 'power' as const},
        {step_id: 'step-2', title: '方案设计', status: 'completed' as const, sort_order: 3, required: true, model_tier: 'power' as const},
        {step_id: 'step-3', title: '方案审批', status: 'completed' as const, sort_order: 4, required: true, model_tier: 'power' as const},
        {step_id: 'step-4', title: '编写测试代码', status: 'completed' as const, sort_order: 5, required: true, model_tier: 'power' as const},
        {step_id: 'step-5', title: '修改代码与验证', status: 'completed' as const, sort_order: 6, required: true, model_tier: 'power' as const},
        {step_id: 'cr-r1', title: '代码审查', status: 'completed' as const, sort_order: 7, required: true, model_tier: 'power' as const},
        {step_id: 'step-6', title: 'P0/P1修复方案设计', status: 'completed' as const, sort_order: 8, required: true, model_tier: 'power' as const},
        {step_id: 'step-7', title: '方案审批', status: 'completed' as const, sort_order: 9, required: true, model_tier: 'power' as const},
        {step_id: 'step-8', title: '实施修复与验证', status: 'completed' as const, sort_order: 10, required: true, model_tier: 'power' as const},
        {step_id: 'step-9', title: '方案A实施', status: 'active' as const, sort_order: 11, required: true, model_tier: 'power' as const},
        {step_id: 'cr-r2', title: '代码审查', status: 'pending' as const, sort_order: 12, required: true, model_tier: 'power' as const},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(realTask))
    renderFlow('10092ff1', onLocation)
    await waitFor(() => expect(document.querySelectorAll('.flow-card').length).toBe(12))
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: '10092ff1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {
        'monitor-init': 'completed', 'review': 'completed', 'report': 'pending',
        'monitor-1': 'completed', 'monitor-2': 'completed', 'monitor-3': 'completed',
        'monitor-4': 'completed', 'monitor-5': 'completed', 'monitor-6': 'completed',
        'monitor-7': 'completed', 'monitor-8': 'completed', 'monitor-9': 'completed',
      },
      monitor_order: {
        'monitor-init': 1, 'review': 22, 'report': 23,
        'monitor-1': 3, 'monitor-2': 5, 'monitor-3': 7, 'monitor-4': 9, 'monitor-5': 11,
        'monitor-6': 21, 'monitor-7': 14, 'monitor-8': 16, 'monitor-9': 18,
      },

      monitor_anchors: {
        'monitor-1': 'step-1', 'monitor-2': 'step-2', 'monitor-3': 'step-3',
        'monitor-4': 'step-4', 'monitor-5': 'step-5', 'monitor-6': 'cr-r1',
        'monitor-7': 'step-6', 'monitor-8': 'step-7', 'monitor-9': 'step-8',
      },
    })
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })
    const lineBtns = (nodeIdx: number) =>
        [...document.querySelectorAll('.flow-node')[nodeIdx]
            .querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]

    for (let i = 1; i <= 9; i++) {
      expect(lineBtns(i).length).toBe(1)
    }

    expect(lineBtns(10).length).toBe(0)
    expect(document.querySelectorAll('.flow-phase-final .flow-link-btn').length).toBe(0)

    for (let i = 1; i <= 9; i++) {
      fireEvent.click(lineBtns(i)[0] as HTMLElement)
    }
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    for (let i = 1; i <= 9; i++) {
      expect(paths.some((p) => p === `/task/10092ff1/monitor/monitor-${i}`)).toBe(true)
    }
  })

  it('monitor sort_order 漂移 + 锚点：眼睛挂锚点步骤后的线段（2026-08-23 DB 实证 monitor-6 13→19）', async () => {

    const ordered = {
      ...gateTask,
      status: 'active' as const,
      steps: gateTask.steps.map((s, i) => ({...s, status: 'completed' as const, sort_order: i + 1})),
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(ordered))
    renderFlow('g1')
    await screen.findByText('登录模块')
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed'},
      monitor_order: {'monitor-1': 9},
      monitor_anchors: {'monitor-1': 'step-1'},
    })
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const allBtns = document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')
    expect(allBtns.length).toBe(1)
    const segBtns = document.querySelectorAll('.flow-node')[1].querySelectorAll('.flow-link-btn')
    expect(segBtns.length).toBe(1)
  })

  it('无锚点漂移 monitor → 挂产出报告前的线（2026-08-24 兜底位置修正，原挂最后 completed 节点下）', async () => {

    const ordered = {
      ...gateTask,
      status: 'active' as const,
      steps: gateTask.steps.map((s, i) => ({...s, sort_order: i + 1})),
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(ordered))
    renderFlow('g1')
    await screen.findByText('登录模块')
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed'},
      monitor_order: {'monitor-1': 9},
    })
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })
    const allBtns = document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')
    expect(allBtns.length).toBe(1)

    expect(document.querySelectorAll('.flow-phase-final .flow-link-btn').length).toBe(1)
    expect(document.querySelectorAll('.flow-area > .flow-branch .flow-link:not(.flow-link-plan) .flow-link-btn').length).toBe(0)
  })

  it('任务暂停：仅第一个 pending 步骤标「暂停中」，其余保持「待执行」（2026-08-23 反馈修正）', async () => {

    const pausedTask = {
      ...gateTask,
      status: 'paused' as const,
      steps: [
        {step_id: 'step-1', title: '需求分析', status: 'completed', required: true, sort_order: 1},
        {step_id: 'step-2', title: '方案设计', status: 'pending', required: true, sort_order: 2},
        {step_id: 'step-3', title: '编码实现', status: 'pending', required: true, sort_order: 3},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(pausedTask))
    renderFlow('g1')
    await waitFor(() => expect(document.querySelectorAll('.flow-card').length).toBe(4))
    expect(screen.getByText('暂停中')).toBeTruthy()
    expect(screen.getByText('待执行')).toBeTruthy()

    expect(document.querySelectorAll('.flow-card.card-paused').length).toBe(1)
    const card3 = Array.from(document.querySelectorAll('.flow-card')).find(
        (c) => c.querySelector('.fc-name')?.textContent === '编码实现')
    expect(card3?.className).not.toContain('card-paused')
  })

  it('输入框下 Token 明细：全流程汇总输入/缓存/输出/金额/运行统计（2026-08-23 用户需求）', async () => {

    const tokenSteps = {
      ...gateTask,
      status: 'active' as const,
      steps: [
        {
          step_id: 'step-1', title: '需求分析', status: 'completed', required: true, model_tier: 'light',
          sort_order: 1, token_prompt: 1000, token_cached: 500, token_completion: 200,
          requests: 2, ttft_total_ms: 600, ttft_samples: 2, output_duration_ms: 10000, run_duration_ms: 20000
        },
        {
          step_id: 'step-2', title: '方案设计', status: 'active', required: true, model_tier: 'power',
          sort_order: 2, token_prompt: 2000, token_cached: 1000, token_completion: 400,
          requests: 1, ttft_total_ms: 300, ttft_samples: 1, output_duration_ms: 5000, run_duration_ms: 10000
        },
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(tokenSteps))

    vi.mocked(api.getConfig).mockResolvedValue({
      lightInputPrice: 10, lightCachedPrice: 10, lightOutputPrice: 10,
      powerInputPrice: 10, powerCachedPrice: 10, powerOutputPrice: 10,
    } as never)
    renderFlow('g1')
    await screen.findByText('登录模块')
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {},
      monitor_steps: {'monitor-1': 'completed'}, monitor_order: {'monitor-1': 1.5},
      step_tokens: {'monitor-1': {token_prompt: 500, token_cached: 0, token_completion: 100}},
      step_stats: {
        'monitor-1': {
          run_duration_ms: 5000, output_duration_ms: 3000,
          ttft_total_ms: 900, ttft_samples: 3, requests: 3
        }
      },
    } as never)
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })
    const row = document.querySelector('.tus-row')
    const metrics = document.querySelector('.tus-metrics')

    expect(row?.textContent).toContain('未缓存输入')
    expect(row?.textContent).toContain('2.0K')
    expect(row?.textContent).toContain('缓存输入 1.5K')
    expect(row?.textContent).toContain('700')
    expect(row?.textContent).toContain('消耗金额')
    expect(row?.textContent).toContain('0.042')

    expect(metrics?.textContent).toContain('输出速度')
    expect(metrics?.textContent).toContain('38.9 token/s')
    expect(metrics?.textContent).toContain('0.300秒')
    expect(metrics?.textContent).toContain('0分35秒')
    expect(metrics?.textContent).toContain('请求数')
    expect(metrics?.textContent).toContain('6')
  })

  it('介入栏：paused + gate active（审批等待）也有强制插入按钮（2026-08-21 用户反馈）', async () => {

    vi.mocked(api.getTask).mockResolvedValue(taskData(gateTask))
    renderFlow('g1')
    expect(await screen.findByText('登录模块')).toBeTruthy()

    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '删除人工决策重建'}})
    const forceBtn = document.querySelector('.intervene-bar .force-btn') as HTMLButtonElement
    expect(forceBtn).not.toBeNull()
    fireEvent.click(forceBtn)
    expect(screen.getByText('将打断当前流程')).toBeTruthy()
    fireEvent.click(screen.getByText('确认介入'))
    await act(async () => {
    })
    expect(api.flowIntervene).toHaveBeenCalledWith('g1', 'immediate', '删除人工决策重建')
  })

  it('介入栏：运行中发送（待发送区排队 pending）；强制介入先确认「将打断当前流程」再 immediate', async () => {

    vi.mocked(api.getTask).mockResolvedValue(taskData(activeTask))
    renderFlow('a1')
    expect(await screen.findByText('执行中任务')).toBeTruthy()

    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '请排队处理'}})
    fireEvent.click(document.querySelector('.intervene-bar .pending-msg .queue-btn') as HTMLElement)
    await act(async () => {
    })
    expect(api.flowIntervene).toHaveBeenCalledWith('a1', 'pending', '请排队处理')

    fireEvent.change(textarea, {target: {value: '立即调整'}})
    fireEvent.click(document.querySelector('.intervene-bar .force-btn') as HTMLElement)
    expect(screen.getByText('将打断当前流程')).toBeTruthy()
    fireEvent.click(screen.getByText('确认介入'))
    await act(async () => {
    })
    expect(api.flowIntervene).toHaveBeenCalledWith('a1', 'immediate', '立即调整')
  })

  it('并行组只渲染一个 Monitor 圆钮（指向组内最后一个步骤的 monitor 实例）', async () => {

    const parallelTask = {
      id: 'p1',
      type: 'custom',
      title: '并行任务',
      status: 'completed',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-2', title: '行为观察', status: 'completed', required: true, parallel_with: ['step-3'], model_tier: 'light'},
        {step_id: 'step-3', title: '静态逆向', status: 'completed', required: true, parallel_with: ['step-2'], model_tier: 'power'},
      ],
    }
    const onLocation = vi.fn()
    vi.mocked(api.getTask).mockResolvedValue(taskData(parallelTask))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'p1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-3': 'completed'},
      monitor_order: {'monitor-3': 3.5},
    })
    renderFlow('p1', onLocation)
    await screen.findByText('并行任务')
    await act(async () => {
    })

    const btns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(btns.length).toBe(1)

    fireEvent.click(btns[0])
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/p1/monitor/monitor-3')).toBe(true)
  })

  it('skipped 步骤显示「已跳过」徽标 + card-skipped 样式（不落待执行）', async () => {
    const doneTask = {
      id: 'd1',
      type: 'custom',
      title: '检查项目文件夹内容',
      status: 'completed',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-1', title: '确认当前工作目录', status: 'completed', required: true, model_tier: 'light'},
        {step_id: 'step-2', title: '扫描项目文件', status: 'skipped', required: true, model_tier: 'light'},
        {step_id: 'step-3', title: '展示文件列表', status: 'skipped', required: true, model_tier: 'power'},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(doneTask))
    renderFlow('d1')

    expect((await screen.findAllByText('已跳过')).length).toBe(2)

    const skippedCards = document.querySelectorAll('.flow-card.card-skipped')
    expect(skippedCards.length).toBe(2)
    expect(skippedCards[0].textContent).toContain('扫描项目文件')
    expect(skippedCards[0].textContent).not.toContain('待执行')

    expect(document.querySelector('.flow-card.card-skipped')?.textContent).not.toContain('确认当前工作目录')
  })

  it('起点线编排圆钮：点击 → /task/:id/monitor/monitor-init', async () => {
    const onLocation = vi.fn()
    renderFlow('g1', onLocation)
    expect(await screen.findByText('登录模块')).toBeTruthy()
    const planBtn = document.querySelector('.flow-link-plan .flow-link-btn') as HTMLElement
    expect(planBtn).toBeTruthy()
    fireEvent.click(planBtn)
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/g1/monitor/monitor-init')).toBe(true)
  })

  it('产出报告节点：未完成 → 待生成无圆钮；完成后 → 卡片跳 /monitor/report、圆钮跳 /monitor/review', async () => {

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'g1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {}, monitor_order: {}, monitor_anchors: {},
    })
    renderFlow('g1')
    expect(await screen.findByText('产出报告')).toBeTruthy()
    expect(screen.getByText('待生成')).toBeTruthy()
    expect(document.querySelector('.flow-phase-final .flow-link-btn')).toBeNull()

    cleanup()
    const doneTask = {
      id: 'c2',
      type: 'dev-full-flow',
      title: '完成的任务',
      status: 'completed',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-1', title: '分析', status: 'completed', required: true},
        {step_id: 'step-2', title: '实施', status: 'skipped', required: true},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(doneTask))
    const onLocation = vi.fn()
    renderFlow('c2', onLocation)
    expect(await screen.findByText('产出报告')).toBeTruthy()
    expect(screen.getByText('已完成')).toBeTruthy()

    const reportBtn = document.querySelector('.flow-phase-final .flow-link-btn') as HTMLElement
    expect(reportBtn).toBeTruthy()
    fireEvent.click(reportBtn)
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/c2/monitor/review')).toBe(true)

    const card = document.querySelector('.flow-phase-final .flow-card') as HTMLElement
    expect(card).toBeTruthy()
    fireEvent.click(card)
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/c2/monitor/report')).toBe(true)
  })

  it('无 taskId：空态提示引导去 Sidebar 新建', async () => {
    render(
        <MemoryRouter>
          <FlowOverview/>
        </MemoryRouter>,
    )

    expect(await screen.findByText(/暂无任务/)).toBeTruthy()
    expect(screen.getByText(/请从左侧 Sidebar 新建任务/)).toBeTruthy()
  })

  it('编排等待页：steps 为空 → 自动重定向到 monitor-init 详情页（复用现成完整页面）', async () => {
    const emptyTask = {
      id: 'e1', type: 'custom', title: '新任务', status: 'active',
      created_at: '2024-01-01T10:00:00', updated_at: '2024-01-01T10:00:00', steps: [],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(emptyTask))

    vi.mocked(api.fsTree).mockResolvedValue({path: '', entries: [{name: 'x', type: 'dir'}]})
    const onLocation = vi.fn()
    renderFlow('e1', onLocation)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10))
    })
    expect(
        (onLocation.mock.calls as unknown as Location[][]).some(
            (c) => c[0].pathname === '/task/e1/monitor/monitor-init'),
    ).toBe(true)
  })

  it('编排等待页：工作区为空 → 显示工作区为空引导（不重定向）', async () => {
    const emptyTask = {
      id: 'e2', type: 'custom', title: '空工作区任务', status: 'active',
      created_at: '2024-01-01T10:00:00', updated_at: '2024-01-01T10:00:00', steps: [],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(emptyTask))
    vi.mocked(api.fsTree).mockResolvedValue({path: '', entries: []})
    renderFlow('e2')
    expect(await screen.findByText(/工作区为空/)).toBeTruthy()
  })

  it('圆点归属（用户需求）：step-16 线段两个圆点——monitor-16 区间归属 + monitor-intervene-1 兜底（sort_order 漂移不丢）', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'two-dots',
      type: 'custom',
      title: '双圆点任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 15},
        {step_id: 'step-16', title: '步骤16', status: 'completed', required: true, model_tier: 'light', sort_order: 16},
        {step_id: 'step-17', title: '步骤17', status: 'pending', required: true, model_tier: 'light', sort_order: 17},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'two-dots', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-16': 'completed', 'monitor-intervene-1': 'active'},
      monitor_order: {'monitor-16': 16.5, 'monitor-intervene-1': 95},
    })
    renderFlow('two-dots', onLocation)
    expect(await screen.findByText('双圆点任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const branchBtns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(branchBtns.length).toBe(1)
    const phaseBtns = [...document.querySelectorAll('.flow-phase-final .flow-link-btn')]
    expect(phaseBtns.length).toBe(1)

    for (const b of [...branchBtns, ...phaseBtns]) fireEvent.click(b as HTMLElement)
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    expect(paths.some((p) => p.includes('/monitor/monitor-intervene-1'))).toBe(true)
    expect(paths.some((p) => p.includes('/monitor/monitor-16'))).toBe(true)
  })

  it('圆点归属：monitor-intervene-1 不参与区间归属（sort_order 漂移不重复渲染），兜底只挂最后 completed 步骤', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'fb1',
      type: 'custom',
      title: '兜底任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 15},
        {step_id: 'step-16', title: '步骤16', status: 'pending', required: true, model_tier: 'light', sort_order: 16},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'fb1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-intervene-1': 'active'},
      monitor_order: {'monitor-intervene-1': 95},
    })
    renderFlow('fb1', onLocation)
    expect(await screen.findByText('兜底任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const branchBtns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(branchBtns.length).toBe(0)
    const phaseBtns = [...document.querySelectorAll('.flow-phase-final .flow-link-btn')]
    expect(phaseBtns.length).toBe(1)
    fireEvent.click(phaseBtns[0] as HTMLElement)
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/fb1/monitor/monitor-intervene-1')).toBe(true)
  })

  it('去绑定（2026-08-21）：monitor-N 区间归属 + 介入实例 monitor-intervene-1 按 sort_order 挂当前运行步骤前的线段', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'unbind1',
      type: 'custom',
      title: '去绑定任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 15},
        {step_id: 'step-16', title: '步骤16', status: 'pending', required: true, model_tier: 'light', sort_order: 16},
        {step_id: 'step-17', title: '步骤17', status: 'pending', required: true, model_tier: 'light', sort_order: 17},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'unbind1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed', 'monitor-intervene-1': 'pending'},
      monitor_order: {'monitor-1': 15.5, 'monitor-intervene-1': 15.8},
    })
    renderFlow('unbind1', onLocation)
    expect(await screen.findByText('去绑定任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const btns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(btns.length).toBe(2)
    for (const b of btns) fireEvent.click(b as HTMLElement)
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    expect(paths.some((p) => p.includes('/monitor/monitor-intervene-1'))).toBe(true)
    expect(paths.some((p) => p.includes('/monitor/monitor-1'))).toBe(true)
  })

  it('去绑定：多个介入实例（monitor-intervene-1/2）sort_order 漂移时兜底多圆点，各自可导航', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'unbind2',
      type: 'custom',
      title: '多介入任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 15},
        {step_id: 'step-16', title: '步骤16', status: 'completed', required: true, model_tier: 'light', sort_order: 16},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'unbind2', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-intervene-1': 'completed', 'monitor-intervene-2': 'pending'},
      monitor_order: {'monitor-intervene-1': 95, 'monitor-intervene-2': 96},
    })
    renderFlow('unbind2', onLocation)
    expect(await screen.findByText('多介入任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })
    const btns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(btns.length).toBe(2)
    expect(document.querySelectorAll('.flow-phase-final .flow-link-btn').length).toBe(0)
    for (const b of btns) fireEvent.click(b as HTMLElement)
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    expect(paths.some((p) => p.includes('/monitor/monitor-intervene-1'))).toBe(true)
    expect(paths.some((p) => p.includes('/monitor/monitor-intervene-2'))).toBe(true)
  })

  it('reorder 后 monitor-N 漂移出线段区间（大于所有真实步骤 order）→ 兜底挂最后 completed 节点（DB 实证 e726f3e6）', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'drift',
      type: 'custom',
      title: '漂移任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-11', title: '步骤11', status: 'completed', required: true, model_tier: 'light', sort_order: 20},
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 21},
        {step_id: 'step-16', title: '步骤16', status: 'completed', required: true, model_tier: 'light', sort_order: 22},
        {step_id: 'step-25', title: '步骤25', status: 'active', required: true, model_tier: 'light', sort_order: 23},
        {step_id: 'step-18', title: '步骤18', status: 'pending', required: true, model_tier: 'light', sort_order: 24},
        {step_id: 'step-19', title: '步骤19', status: 'pending', required: true, model_tier: 'light', sort_order: 25},
        {step_id: 'step-20', title: '步骤20', status: 'pending', required: true, model_tier: 'light', sort_order: 26},
        {step_id: 'step-21', title: '步骤21', status: 'pending', required: true, model_tier: 'light', sort_order: 27},
        {step_id: 'step-22', title: '步骤22', status: 'pending', required: true, model_tier: 'light', sort_order: 28},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'drift', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-1': 'completed', 'monitor-2': 'completed'},
      monitor_order: {'monitor-1': 30, 'monitor-2': 50},
    })
    renderFlow('drift', onLocation)
    expect(await screen.findByText('漂移任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const btns = [...document.querySelectorAll('.flow-phase-final .flow-link-btn')]
    expect(btns.length).toBe(2)
    for (const b of btns) fireEvent.click(b as HTMLElement)
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    expect(paths.some((p) => p === '/task/drift/monitor/monitor-1')).toBe(true)
    expect(paths.some((p) => p === '/task/drift/monitor/monitor-2')).toBe(true)
  })

  it('monitor-init 由起点线独立渲染，不参与兜底（order 漂移也不重复出现）', async () => {
    const onLocation = vi.fn()
    const t = {
      id: 'init1',
      type: 'custom',
      title: '起点任务',
      status: 'active',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-15', title: '步骤15', status: 'completed', required: true, model_tier: 'light', sort_order: 15},
        {step_id: 'step-16', title: '步骤16', status: 'pending', required: true, model_tier: 'light', sort_order: 16},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))

    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'init1', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {'monitor-init': 'completed', 'monitor-1': 'completed'},
      monitor_order: {'monitor-init': 10, 'monitor-1': 30},
    })
    renderFlow('init1', onLocation)
    expect(await screen.findByText('起点任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const planBtns = document.querySelectorAll('.flow-link-plan .flow-link-btn')
    expect(planBtns.length).toBe(1)
    expect(planBtns[0].getAttribute('title')).toContain('流程编排')
    const btns = [...document.querySelectorAll('.flow-phase-final .flow-link-btn')]
    expect(btns.length).toBe(1)
    expect(document.querySelectorAll('.flow-area > .flow-branch .flow-link:not(.flow-link-plan) .flow-link-btn').length).toBe(0)
    fireEvent.click(btns[0])
    expect(onLocation.mock.calls.some((c) => c[0].pathname === '/task/init1/monitor/monitor-1')).toBe(true)
  })

  it('多 phase 组：非最后组末节点不渲染终点线眼睛，monitor 不重复出现（DB 实证 60b8e589）', async () => {

    const onLocation = vi.fn()
    const t = {
      id: 'multi-phase',
      type: 'custom',
      title: '多组任务',
      status: 'completed',
      created_at: '2024-01-01T10:00:00',
      updated_at: '2024-01-01T11:00:00',
      steps: [
        {step_id: 'step-1', title: '步骤1', status: 'completed', required: true, model_tier: 'light', sort_order: 2},
        {step_id: 'step-2', title: '步骤2', status: 'completed', required: true, model_tier: 'light', sort_order: 4},
        {step_id: 'step-3', title: '步骤3', status: 'completed', required: true, model_tier: 'light', sort_order: 6},
        {step_id: 'step-4', title: '方案审批', status: 'completed', required: true, model_tier: 'light', sort_order: 8},
        {step_id: 'step-5', title: '编写测试代码', status: 'completed', required: true, model_tier: 'light', sort_order: 9},
        {step_id: 'step-6', title: '步骤6', status: 'completed', required: true, model_tier: 'light', sort_order: 11},
        {step_id: 'cr-r1', title: '代码审查', status: 'completed', required: true, model_tier: 'light', sort_order: 13},
        {step_id: 'step-7', title: '步骤7', status: 'completed', required: true, model_tier: 'light', sort_order: 14},
        {step_id: 'cr-r2', title: '修复后审查', status: 'completed', required: true, model_tier: 'light', sort_order: 16},
      ],
    }
    vi.mocked(api.getTask).mockResolvedValue(taskData(t))
    vi.mocked(api.getMonitorConversations).mockResolvedValue({
      task_id: 'multi-phase', monitor_conversations: {}, step_tokens: {},
      monitor_steps: {
        'monitor-1': 'completed', 'monitor-2': 'completed', 'monitor-3': 'completed',
        'monitor-4': 'completed', 'monitor-5': 'completed', 'monitor-6': 'completed',
        'monitor-7': 'completed', 'monitor-8': 'completed',
      },
      monitor_order: {
        'monitor-1': 3, 'monitor-2': 5, 'monitor-3': 7, 'monitor-4': 10,
        'monitor-5': 12, 'monitor-6': 13.5, 'monitor-7': 15, 'monitor-8': 17,
      },
    })
    renderFlow('multi-phase', onLocation)
    expect(await screen.findByText('多组任务')).toBeTruthy()
    await act(async () => {
      return new Promise((r) => setTimeout(r, 1100))
    })

    const btns = [...document.querySelectorAll('.flow-link:not(.flow-link-plan) .flow-link-btn')]
        .filter((b) => !b.closest('.flow-phase-final'))
    expect(btns.length).toBe(8)

    expect(document.querySelectorAll('.flow-phase-title').length).toBe(0)
    expect(document.querySelectorAll('.pt-phase').length).toBe(0)

    expect(document.querySelectorAll('.flow-area > .flow-branch').length).toBe(1)

    for (const b of btns) fireEvent.click(b as HTMLElement)
    const paths = onLocation.mock.calls.map((c) => c[0].pathname)
    for (let i = 1; i <= 8; i++) {
      const hit = paths.filter((p) => p === `/task/multi-phase/monitor/monitor-${i}`)
      expect(hit.length).toBe(1), `monitor-${i} 应恰好渲染一次: ${paths}`
    }
  })
})
