import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {ConfigView} from '../api/types'
import {api} from '../api/client'
import SettingsPage from '../config/SettingsPage'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number

    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  },
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

const API_KEY_VALUE = 'sk-test-12345-secret'

const cfg: ConfigView = {
  baseUrl: 'http://127.0.0.1:8501',
  lightModel: 'gpt-4o-mini',
  powerModel: 'gpt-4o',
  lightBaseUrl: 'http://127.0.0.1:8501',
  powerBaseUrl: 'http://127.0.0.1:8501',
  hasLightApiKey: true,
  hasPowerApiKey: true,
  lightInputPrice: 0.5,
  lightCachedPrice: 0.1,
  lightOutputPrice: 2,
  powerInputPrice: 5,
  powerCachedPrice: 1,
  powerOutputPrice: 15,
  projectRoot: 'workspace',
  port: 8501,
  host: '0.0.0.0',
  hasApiKey: true,
  contextWindow: 400000,
  channelType: '',
  hasChannel: false,
}

beforeEach(() => {
  vi.mocked(api.getConfig).mockResolvedValue(cfg)
  vi.mocked(api.saveConfig).mockResolvedValue({status: 'ok'})
  vi.mocked(api.testLlm).mockResolvedValue({ok: true, model: 'gpt-4o'})
})

afterEach(() => {

  cleanup()
  vi.clearAllMocks()
})

describe('Settings（T4.5）', () => {
  it('表单回填：light/power 两组端点与价格回显；apiKey 不显示值，仅 hasLightApiKey/hasPowerApiKey 指示', async () => {
    render(<SettingsPage/>)

    expect(await screen.findAllByDisplayValue('http://127.0.0.1:8501')).toHaveLength(2)
    expect(screen.getAllByDisplayValue('gpt-4o-mini')).toHaveLength(1)
    expect(screen.getAllByDisplayValue('gpt-4o')).toHaveLength(1)
    expect(screen.getByDisplayValue('workspace')).toBeTruthy()

    expect(screen.getByDisplayValue('8501')).toBeTruthy()
    expect(screen.getByDisplayValue('0.0.0.0')).toBeTruthy()

    expect(screen.getByDisplayValue('0.5')).toBeTruthy()
    expect(screen.getByDisplayValue('0.1')).toBeTruthy()
    expect(screen.getByDisplayValue('2')).toBeTruthy()
    expect(screen.getByDisplayValue('5')).toBeTruthy()
    expect(screen.getByDisplayValue('1')).toBeTruthy()
    expect(screen.getByDisplayValue('15')).toBeTruthy()

    expect(screen.getAllByText(/（已配置）/)).toHaveLength(2)

    const keyInputs = screen.getAllByPlaceholderText('留空则保持原 Key') as HTMLInputElement[]
    expect(keyInputs).toHaveLength(2)
    for (const k of keyInputs) {
      expect(k.type).toBe('password')
      expect(k.value).toBe('')
    }

    expect(screen.queryByText(API_KEY_VALUE)).toBeNull()
    expect(screen.queryByText('sk-')).toBeNull()
  })

  it('保存 payload 正确：light/power 独立端点/Key/价格全量提交；apiKey 类空串仍发送', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')

    fireEvent.change(screen.getAllByDisplayValue('http://127.0.0.1:8501')[0],
        {target: {value: 'http://light-new:8501'}})
    fireEvent.change(screen.getByDisplayValue('gpt-4o'), {target: {value: 'gpt-4o-2024'}})
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() =>
        expect(api.saveConfig).toHaveBeenCalledWith({
          lightModel: 'gpt-4o-mini',
          powerModel: 'gpt-4o-2024',
          projectRoot: 'workspace',
          apiKey: '',
          lightBaseUrl: 'http://light-new:8501',
          lightApiKey: '',
          powerBaseUrl: 'http://127.0.0.1:8501',
          powerApiKey: '',
          lightInputPrice: 0.5,
          lightCachedPrice: 0.1,
          lightOutputPrice: 2,
          powerInputPrice: 5,
          powerCachedPrice: 1,
          powerOutputPrice: 15,
          contextWindow: 400000,
        }),
    )
  })

  it('J10 保存前校验：lightModel/powerModel 任一为空 → 阻止保存并提示', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')
    fireEvent.change(screen.getByDisplayValue('gpt-4o-mini'), {target: {value: ''}})
    fireEvent.click(screen.getByText('保存'))
    expect(await screen.findByText('请填写 light 与 power 两个模型')).toBeTruthy()
    expect(api.saveConfig).not.toHaveBeenCalled()
  })

  it('J-K2 价格非法（负数）→ 阻止保存并提示', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')
    fireEvent.change(screen.getByDisplayValue('0.5'), {target: {value: '-1'}})
    fireEvent.click(screen.getByText('保存'))
    expect(await screen.findByText(/价格必须是 ≥0 的数字/)).toBeTruthy()
    expect(api.saveConfig).not.toHaveBeenCalled()
  })

  it('testLlm 结果提示：连接成功', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')
    fireEvent.click(screen.getByText('测试连接'))
    expect(await screen.findByText(/连接成功/)).toBeTruthy()
    expect(api.testLlm).toHaveBeenCalled()
  })

  it('testLlm 结果提示：连接失败（error 文案）', async () => {
    vi.mocked(api.testLlm).mockResolvedValue({ok: false, error: 'invalid api key', model: 'gpt-4o'})
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')
    fireEvent.click(screen.getByText('测试连接'))
    expect(await screen.findByText(/连接失败：invalid api key/)).toBeTruthy()
  })

  it('projectRoot 变更保存 → 提示"需重启服务生效（运行 start.bat 前请确认）"', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')
    fireEvent.change(screen.getByDisplayValue('workspace'), {target: {value: 'new-workspace'}})
    fireEvent.click(screen.getByText('保存'))
    expect(await screen.findByText('需重启服务生效（运行 start.bat 前请确认）')).toBeTruthy()
    await waitFor(() => expect(api.saveConfig).toHaveBeenCalled())
  })

  it('newapi_channel_conn 通道 JSON 粘贴 → 两组 URL/Key 自动填充，保存 payload 含两组新 Key', async () => {
    render(<SettingsPage/>)
    await screen.findAllByDisplayValue('http://127.0.0.1:8501')

    const channelBox = screen.getByPlaceholderText(/粘贴通道 JSON/) as HTMLTextAreaElement
    fireEvent.change(channelBox, {
      target: {
        value: '{"_type":"newapi_channel_conn","key":"sk-new-api-key-777","url":"https://www.boomfirst.xyz"}',
      },
    })

    expect(await screen.findAllByDisplayValue('https://www.boomfirst.xyz')).toHaveLength(2)
    const keyInputs = screen.getAllByPlaceholderText('留空则保持原 Key') as HTMLInputElement[]
    expect(keyInputs[0].value).toBe('sk-new-api-key-777')
    expect(keyInputs[1].value).toBe('sk-new-api-key-777')
    for (const k of keyInputs) {
      expect(k.type).toBe('password')
    }

    expect(screen.queryByText('sk-new-api-key-777')).toBeNull()

    fireEvent.click(screen.getByText('保存'))
    await waitFor(() =>
        expect(api.saveConfig).toHaveBeenCalledWith(
            expect.objectContaining({
              lightBaseUrl: 'https://www.boomfirst.xyz',
              powerBaseUrl: 'https://www.boomfirst.xyz',
              lightApiKey: 'sk-new-api-key-777',
              powerApiKey: 'sk-new-api-key-777',
            }),
        ),
    )
  })

  it('llmChannel 已配置（hasChannel）→ 显示当前通道类型提示', async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      ...cfg,
      channelType: 'newapi_channel_conn',
      hasChannel: true,
    })
    render(<SettingsPage/>)
    expect(await screen.findByText(/当前使用通道：newapi_channel_conn/)).toBeTruthy()
  })
})
