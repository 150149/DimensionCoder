import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {MemoryRouter} from 'react-router-dom'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {TaskOverview, TaskSummary} from '../api/types'
import {api} from '../api/client'
import App from '../App'

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
    setBestEffort: vi.fn(),
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

function renderApp(initial = '/') {
  return render(
      <MemoryRouter initialEntries={[initial]}>
        <App/>
      </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.listTasks).mockResolvedValue(tasksOverview([activeTask]))
  vi.mocked(api.getConfig).mockResolvedValue(okConfig)
  vi.mocked(api.getTask).mockResolvedValue(undefined as never)
  vi.mocked(api.getMonitorConversations).mockResolvedValue({
    task_id: 't1',
    monitor_conversations: {},
  })
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('App 移动端抽屉', () => {
  it('渲染汉堡按钮（.hamburger-btn）', () => {
    renderApp()
    expect(document.querySelector('.hamburger-btn')).not.toBeNull()
  })

  it('点击汉堡 → sidebar 挂 open 类；再点 → 关闭', async () => {
    renderApp()
    const btn = document.querySelector('.hamburger-btn')!
    fireEvent.click(btn)
    await waitFor(() =>
        expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(true))
    fireEvent.click(btn)
    await waitFor(() =>
        expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(false))
  })

  it('open 时渲染 backdrop，点击 backdrop 关闭抽屉', async () => {
    renderApp()
    fireEvent.click(document.querySelector('.hamburger-btn')!)
    await waitFor(() => expect(document.querySelector('.sidebar-backdrop')).not.toBeNull())
    fireEvent.click(document.querySelector('.sidebar-backdrop')!)
    await waitFor(() =>
        expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(false))
    expect(document.querySelector('.sidebar-backdrop')).toBeNull()
  })

  it('打开状态下路由导航（点击任务卡跳 /task/:id）→ 自动关闭', async () => {
    renderApp()
    fireEvent.click(document.querySelector('.hamburger-btn')!)
    await waitFor(() =>
        expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(true))
    fireEvent.click(screen.getByText('实现登录页'))
    await waitFor(() =>
        expect(document.querySelector('aside.sidebar')?.classList.contains('open')).toBe(false))
    await waitFor(() => expect(window.location.pathname).toBeTruthy())
  })
})
