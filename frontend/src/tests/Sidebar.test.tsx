import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {useEffect} from 'react'
import type {Location} from 'react-router-dom'
import {MemoryRouter, useLocation} from 'react-router-dom'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {TaskOverview, TaskSummary} from '../api/types'
import {api} from '../api/client'
import Sidebar, {taskStateClass} from '../panels/Sidebar'

vi.mock('../api/client', () => ({
  api: {
    listTasks: vi.fn(),
    createTask: vi.fn(),
    startTask: vi.fn(),
    gracefulRestart: vi.fn(),
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

const okConfig = {
  baseUrl: 'http://127.0.0.1:8501',
  lightModel: 'gpt-4o-mini',
  powerModel: 'gpt-4o',
  projectRoot: '',
  port: 8501,
  host: '127.0.0.1',
  hasApiKey: true,
  contextWindow: 400000,
}

const activeTask = {
  id: 't1',
  type: 'dev-full-flow',
  title: '实现登录页',
  status: 'active',
  created_at: '2024-01-01T10:00:00',
  updated_at: '2024-01-01T12:30:00',
  steps: [
    {step_id: 'step-1', title: '需求分析', status: 'completed', required: true},
    {step_id: 'step-2', title: '方案设计', status: 'active', required: true},
    {step_id: 'step-3', title: '编码实现', status: 'pending', required: true},
  ],
}

function tasksOverview(tasks: TaskSummary[]): TaskOverview {
  return {
    epics: [],
    tasks,
    task_count: tasks.length,
    status_distribution: {},
    available_task_types: [],
  }
}

function LocationProbe({onLocation}: { onLocation: (loc: Location) => void }) {
  const location = useLocation()
  useEffect(() => {
    onLocation(location)
  }, [location, onLocation])
  return null
}

class MockEventSource {
  static instances: MockEventSource[] = []
  onmessage: ((msg: MessageEvent<string>) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(public url: string) {
    MockEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }
}

function fire(es: MockEventSource, ev: Record<string, unknown>): void {
  es.onmessage?.({data: JSON.stringify(ev)} as MessageEvent<string>)
}

function renderSidebar(onLocation?: (loc: Location) => void) {
  return render(
      <MemoryRouter>
        {onLocation && <LocationProbe onLocation={onLocation}/>}
        <Sidebar/>
      </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.listTasks).mockResolvedValue(tasksOverview([activeTask]))
  vi.mocked(api.getConfig).mockResolvedValue(okConfig)
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {

  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('Sidebar', () => {

  it('抽屉 open/close：open=true 挂 open 类，false 不挂', () => {
    const {rerender} = render(
        <MemoryRouter>
          <Sidebar open/>
        </MemoryRouter>,
    )
    expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(true)
    rerender(
        <MemoryRouter>
          <Sidebar open={false}/>
        </MemoryRouter>,
    )
    expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(false)
  })

  it('backdrop：仅 open 时渲染，点击触发 onClose', () => {
    const onClose = vi.fn()
    const {rerender} = render(
        <MemoryRouter>
          <Sidebar open onClose={onClose}/>
        </MemoryRouter>,
    )
    const backdrop = document.querySelector('.sidebar-backdrop')
    expect(backdrop).not.toBeNull()
    fireEvent.click(backdrop!)
    expect(onClose).toHaveBeenCalledTimes(1)
    rerender(
        <MemoryRouter>
          <Sidebar open={false} onClose={onClose}/>
        </MemoryRouter>,
    )
    expect(document.querySelector('.sidebar-backdrop')).toBeNull()
  })

  it('无 props 渲染（默认值兼容）：不挂 open 类、无 backdrop', () => {
    const {container} = render(
        <MemoryRouter>
          <Sidebar/>
        </MemoryRouter>,
    )
    const aside = container.querySelector('aside.sidebar')
    expect(aside).not.toBeNull()
    expect(aside?.classList.contains('open')).toBe(false)
    expect(container.querySelector('.sidebar-backdrop')).toBeNull()
  })
  it('渲染任务卡：type 图标/mini 进度/当前步骤标题/更新时间', async () => {
    renderSidebar()
    expect(await screen.findByText('实现登录页')).toBeTruthy()

    expect(document.querySelector('.type-icon svg')).toBeTruthy()

    const segs = document.querySelectorAll('.mini-seg')
    expect(segs.length).toBe(3)
    expect(segs[0].className).toContain('seg-done')
    expect(segs[1].className).toContain('seg-active')
    expect(segs[2].className).toContain('seg-pending')

    expect(screen.getByText('方案设计')).toBeTruthy()

    expect(screen.getByText('12:30')).toBeTruthy()

    expect(document.querySelector('.new-task-box textarea')).toBeTruthy()
    expect(screen.getByText('启动')).toBeTruthy()
  })

  it('新建提交：config 预检通过 → createTask → 提示条（无操作按钮）→ 10s 自动消失', async () => {
    vi.mocked(api.createTask).mockResolvedValue({task_id: 't9', task_type: 'dev-full-flow', title: '新任务'})

    vi.mocked(api.listTasks).mockResolvedValue(
        tasksOverview([
          activeTask,
          {
            id: 't9',
            type: 'dev-full-flow',
            title: '新任务',
            status: 'active',
            created_at: '2024-01-01T13:00:00',
            updated_at: '2024-01-01T13:00:00',
            steps: [{step_id: 'step-1', title: '重构', status: 'pending', required: true}],
          },
        ]),
    )
    renderSidebar()
    await act(async () => {
    })

    const textarea = document.querySelector('.new-task-box textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '帮我重构登录模块'}})

    fireEvent.click(document.querySelector('.new-task-box .primary') as HTMLElement)
    await act(async () => {
    })

    expect(api.createTask).toHaveBeenCalledWith('帮我重构登录模块')

    expect(screen.getByText(/任务已创建/)).toBeTruthy()
    expect(screen.queryByText('立即启动')).toBeNull()
    expect(api.startTask).not.toHaveBeenCalled()

    expect(document.querySelector('.error-toast.success button')).toBeNull()
  })

  it('新建提交：提示条 10s 自动消失', async () => {
    vi.mocked(api.createTask).mockResolvedValue({task_id: 't9', task_type: 'custom', title: '新任务'})
    vi.mocked(api.listTasks).mockResolvedValue(tasksOverview([activeTask]))
    vi.useFakeTimers()
    renderSidebar()
    await act(async () => {
    })

    const textarea = document.querySelector('.new-task-box textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '帮我重构登录模块'}})
    fireEvent.click(document.querySelector('.new-task-box .primary') as HTMLElement)
    await act(async () => {
    })

    expect(screen.getByText(/任务已创建/)).toBeTruthy()
    act(() => {
      vi.advanceTimersByTime(10001)
    })
    expect(screen.queryByText(/任务已创建/)).toBeNull()
    vi.useRealTimers()
  })

  it('新建提交：步骤未生成时提示「AI 正在生成流程…」，不显示立即启动', async () => {
    vi.mocked(api.createTask).mockResolvedValue({task_id: 't9', task_type: 'custom', title: '新任务'})

    vi.mocked(api.listTasks).mockResolvedValue(tasksOverview([activeTask]))
    renderSidebar()
    await act(async () => {
    })

    const textarea = document.querySelector('.new-task-box textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '帮我重构登录模块'}})
    fireEvent.click(screen.getByText('启动'))
    await act(async () => {
    })

    expect(screen.getByText(/AI 正在生成流程/)).toBeTruthy()
    expect(screen.queryByText('立即启动')).toBeNull()
  })

  it('config 未配置：按钮禁用 + 内联红字，不发起创建', async () => {
    vi.mocked(api.getConfig).mockResolvedValue({...okConfig, hasApiKey: false})
    renderSidebar()
    await act(async () => {
    })

    const btn = screen.getByText('启动') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(screen.getByText('请先在设置页配置 LLM 与双模型')).toBeTruthy()

    const textarea = document.querySelector('.new-task-box textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '描述'}})
    fireEvent.click(btn)
    await act(async () => {
    })
    expect(api.createTask).not.toHaveBeenCalled()
  })

  it('创建失败：ErrorToast 展示后端错误', async () => {
    vi.mocked(api.createTask).mockRejectedValue(new Error('LLM 调用失败（status=500）'))
    renderSidebar()
    await act(async () => {
    })

    const textarea = document.querySelector('.new-task-box textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '描述'}})
    fireEvent.click(screen.getByText('启动'))
    await act(async () => {
    })
    await act(async () => {
    })

    expect(screen.getByText('LLM 调用失败（status=500）')).toBeTruthy()
  })

  it('优雅重启：点击重启按钮 → 确认对话框 → 确认后请求 gracefulRestart', async () => {
    vi.mocked(api.gracefulRestart).mockResolvedValue({
      status: 'ok',
      action: 'restart',
      message: '优雅重启已请求：等待当前命令完成后自动重启',
    })
    renderSidebar()
    await act(async () => {
    })

    const restartBtn = document.querySelector('.new-task-box button[title*="优雅重启"]') as HTMLElement
    expect(restartBtn).toBeTruthy()
    fireEvent.click(restartBtn)

    expect(screen.getByText('优雅重启服务')).toBeTruthy()
    fireEvent.click(screen.getByText('重启'))
    await act(async () => {
    })

    expect(api.gracefulRestart).toHaveBeenCalledWith('restart')

    expect(screen.getByText(/优雅重启已请求/)).toBeTruthy()
  })

  it('优雅重启：取消不请求', async () => {
    renderSidebar()
    await act(async () => {
    })

    fireEvent.click(document.querySelector('.new-task-box button[title*="优雅重启"]') as HTMLElement)
    expect(screen.getByText('优雅重启服务')).toBeTruthy()
    fireEvent.click(screen.getByText('取消'))
    await act(async () => {
    })
    expect(api.gracefulRestart).not.toHaveBeenCalled()
  })

  it('优雅重启：成功提示无关闭按钮，3s 自动消失', async () => {
    vi.mocked(api.gracefulRestart).mockResolvedValue({
      status: 'ok', action: 'restart', message: '优雅重启已请求：等待当前命令完成后自动重启',
    })
    vi.useFakeTimers()
    renderSidebar()
    await act(async () => {
    })

    fireEvent.click(document.querySelector('.new-task-box button[title*="优雅重启"]') as HTMLElement)
    fireEvent.click(screen.getByText('重启'))
    await act(async () => {
    })

    expect(screen.getByText(/优雅重启已请求/)).toBeTruthy()

    expect(document.querySelector('.error-toast.success button')).toBeNull()
    act(() => {
      vi.advanceTimersByTime(3001)
    })
    expect(screen.queryByText(/优雅重启已请求/)).toBeNull()
    vi.useRealTimers()
  })

  it('未启动卡片显示「启动」按钮（P0-5）', async () => {
    vi.mocked(api.listTasks).mockResolvedValue(
        tasksOverview([
          {
            id: 'u1',
            type: 'custom',
            title: '未启动任务',
            status: 'active',
            created_at: '2024-01-01T10:00:00',
            updated_at: '2024-01-01T11:00:00',
            steps: [{step_id: 's1', title: '分析', status: 'pending', required: true}],
          },
        ]),
    )
    renderSidebar()

    await waitFor(() => expect(document.querySelector('.task-card .resume-btn')).toBeTruthy())

    expect(document.querySelector('.task-card .type-icon svg')).toBeTruthy()

    fireEvent.click(document.querySelector('.task-card .resume-btn') as HTMLElement)
    await act(async () => {
    })
    expect(api.startTask).toHaveBeenCalledWith('u1')
  })

  it('任务卡状态色 class（2026-08-23 用户需求）：gate 橙/执行中蓝/暂停灰/已完成绿/stopped 红', async () => {
    const gateSteps = [
      {step_id: 'step-1', title: '需求分析', status: 'pending', required: true},
      {step_id: 'gate-1', title: '方案审批', status: 'active', required: true, human_attention: 'gate'},
    ]
    const tasks = [
      {...activeTask, id: 't-gate', status: 'paused', steps: gateSteps},
      {...activeTask, id: 't-active', status: 'active'},
      {
        ...activeTask, id: 't-paused', status: 'paused',
        steps: activeTask.steps.map((s) => ({...s, status: 'pending'}))
      },
      {
        ...activeTask, id: 't-done', status: 'completed',
        steps: activeTask.steps.map((s) => ({...s, status: 'completed'}))
      },
      {
        ...activeTask, id: 't-stopped', status: 'paused',
        steps: [{step_id: 's1', title: 'x', status: 'stopped', required: true}]
      },
    ]
    vi.mocked(api.listTasks).mockResolvedValue(tasksOverview(tasks))
    renderSidebar()
    await waitFor(() => expect(document.querySelectorAll('.task-card').length).toBe(5))
    expect(document.querySelector('.task-card.st-gate')).toBeTruthy()
    expect(document.querySelector('.task-card.st-active')).toBeTruthy()
    expect(document.querySelector('.task-card.st-paused')).toBeTruthy()
    expect(document.querySelector('.task-card.st-done')).toBeTruthy()
    expect(document.querySelector('.task-card.st-stopped')).toBeTruthy()
  })

  it('taskStateClass 优先级：gate > stopped > active > paused > done > abandoned', () => {
    expect(taskStateClass({
      ...activeTask, status: 'paused',
      steps: [{step_id: 'g', title: '审批', status: 'active', required: true, human_attention: 'gate'}]
    })).toBe('st-gate')
    expect(taskStateClass({
      ...activeTask, status: 'paused',
      steps: [{step_id: 's', title: 'x', status: 'stopped', required: true}]
    })).toBe('st-stopped')
    expect(taskStateClass({...activeTask, status: 'active'})).toBe('st-active')
    expect(taskStateClass({
      ...activeTask, status: 'paused',
      steps: activeTask.steps.map((s) => ({...s, status: 'pending'}))
    })).toBe('st-paused')
    expect(taskStateClass({
      ...activeTask, status: 'completed',
      steps: activeTask.steps.map((s) => ({...s, status: 'completed'}))
    })).toBe('st-done')
    expect(taskStateClass({...activeTask, status: 'abandoned'})).toBe('st-abandoned')
  })
})
