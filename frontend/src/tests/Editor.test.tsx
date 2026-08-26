import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react'
import {useState} from 'react'
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {api, ApiError} from '../api/client'
import type {FsFile, FsTree} from '../api/types'
import CodeEditor from '../editor/CodeEditor'
import FileTree from '../editor/FileTree'

vi.mock('@monaco-editor/react', () => {
  const EditorMock = (props: any) => {

    if (props.onMount) {
      props.onMount(
          {
            addCommand: (_kb: number, handler: () => void) => {
              ;(window as unknown as { __dcSaveHandler?: () => void }).__dcSaveHandler = handler
            },
          },
          {},
      )
    }
    return (
        <div data-testid="monaco-mock" data-language={props.language}>
          {props.value}
          <button type="button" onClick={() => props.onChange?.(`${props.value}changed`)}>
            simulate-edit
          </button>
        </div>
    )
  }
  return {
    __esModule: true,
    default: EditorMock,
    Editor: EditorMock,
    loader: {config: vi.fn()},
  }
})

vi.mock('../editor/monacoSetup', () => ({
  monaco: {KeyMod: {CtrlCmd: 2048}, KeyCode: {KeyS: 49}},
}))

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

function EditorHarness() {
  const [path, setPath] = useState<string | null>(null)
  return (
      <div style={{display: 'flex'}}>
        <FileTree onOpenFile={setPath}/>
        <CodeEditor path={path}/>
      </div>
  )
}

function tree(entries: FsTree['entries']): FsTree {
  return {path: '', entries}
}

const file = (path: string, content: string, mtime = 100): FsFile => ({
  path,
  content,
  mtime,
  size: content.length,
})

beforeEach(() => {
  vi.mocked(api.fsTree).mockResolvedValue(
      tree([
        {name: 'src', type: 'dir'},
        {name: 'main.py', type: 'file', size: 8},
        {name: 'README.md', type: 'file', size: 5},
      ]),
  )
})

afterEach(() => {

  cleanup()
  vi.clearAllMocks()
  delete (window as unknown as { __dcSaveHandler?: () => void }).__dcSaveHandler
})

describe('Editor（T4.4）', () => {
  it('文件树渲染 + 懒展开：展开目录时请求该层 api.fsTree(path) 单层', async () => {
    render(<EditorHarness/>)

    expect(await screen.findByText('src')).toBeTruthy()
    expect(screen.getByText('main.py')).toBeTruthy()
    expect(api.fsTree).toHaveBeenCalledWith('')

    vi.mocked(api.fsTree).mockResolvedValue({path: 'src', entries: [{name: 'app.ts', type: 'file', size: 5}]})
    fireEvent.click(screen.getByText('src'))
    expect(await screen.findByText('app.ts')).toBeTruthy()
    expect(api.fsTree).toHaveBeenCalledWith('src')
  })

  it('打开文件调 fsRead，monaco-mock 收到正确 language/value（py→python）', async () => {
    vi.mocked(api.fsRead).mockResolvedValue(file('main.py', 'print(1)\n'))
    render(<EditorHarness/>)
    await screen.findByText('main.py')
    fireEvent.click(screen.getByText('main.py'))
    await waitFor(() => expect(api.fsRead).toHaveBeenCalledWith('main.py'))
    const mock = document.querySelector('[data-testid="monaco-mock"]')
    expect(mock).toBeTruthy()
    expect(mock!.getAttribute('data-language')).toBe('python')
    expect(mock!.textContent).toContain('print(1)')
  })

  it('保存调 fsWrite：Ctrl+S 命令（onMount addCommand）→ api.fsWrite(path, 当前内容)', async () => {
    vi.mocked(api.fsRead).mockResolvedValue(file('main.py', 'print(1)\n'))
    vi.mocked(api.fsWrite).mockResolvedValue({status: 'ok', size: 9})
    render(<EditorHarness/>)
    await screen.findByText('main.py')
    fireEvent.click(screen.getByText('main.py'))
    await waitFor(() => expect(api.fsRead).toHaveBeenCalledWith('main.py'))

    fireEvent.click(screen.getByText('simulate-edit'))

    const save = (window as unknown as { __dcSaveHandler?: () => void }).__dcSaveHandler
    expect(save).toBeTypeOf('function')
    await act(async () => {
      await save!()
    })
    expect(api.fsWrite).toHaveBeenCalledWith('main.py', 'print(1)\nchanged')
  })

  it('大文件提示：fsRead mock 413 响应 → 错误提示（文案来自后端）', async () => {
    vi.mocked(api.fsRead).mockRejectedValue(new ApiError(413, '文件超过 2MB 上限（3000000 bytes），请在本地编辑'))
    render(<EditorHarness/>)
    await screen.findByText('main.py')
    fireEvent.click(screen.getByText('main.py'))

    expect((await screen.findAllByText(/文件超过 2MB 上限/)).length).toBeGreaterThanOrEqual(1)
  })

  it('扩展名映射表固定：ts/tsx→typescript、js/jsx→javascript、py→python、json→json、md→markdown、html→html、css→css、其他→plaintext', async () => {
    vi.mocked(api.fsTree).mockResolvedValue(
        tree([
          {name: 'a.ts', type: 'file', size: 1},
          {name: 'b.tsx', type: 'file', size: 1},
          {name: 'c.js', type: 'file', size: 1},
          {name: 'd.jsx', type: 'file', size: 1},
          {name: 'e.py', type: 'file', size: 1},
          {name: 'f.json', type: 'file', size: 1},
          {name: 'g.md', type: 'file', size: 1},
          {name: 'h.html', type: 'file', size: 1},
          {name: 'i.css', type: 'file', size: 1},
          {name: 'j.xyz', type: 'file', size: 1},
        ]),
    )
    vi.mocked(api.fsRead).mockImplementation((p: string) =>
        Promise.resolve(file(p, `content of ${p}`)),
    )
    render(<EditorHarness/>)
    const cases: Array<[string, string]> = [
      ['a.ts', 'typescript'],
      ['b.tsx', 'typescript'],
      ['c.js', 'javascript'],
      ['d.jsx', 'javascript'],
      ['e.py', 'python'],
      ['f.json', 'json'],
      ['g.md', 'markdown'],
      ['h.html', 'html'],
      ['i.css', 'css'],
      ['j.xyz', 'plaintext'],
    ]
    for (const [name, lang] of cases) {
      fireEvent.click(await screen.findByText(name))
      await waitFor(() => {
        const mock = document.querySelector('[data-testid="monaco-mock"]')
        expect(mock!.getAttribute('data-language')).toBe(lang)
        expect(mock!.textContent).toContain(`content of ${name}`)
      })
    }
  })

  it('保存冲突 409：三选弹窗（复制我的版本/强制覆盖/放弃修改，J2a），放弃修改重载服务器版', async () => {
    vi.mocked(api.fsRead).mockResolvedValue(file('main.py', 'print(1)\n'))
    render(<EditorHarness/>)
    await screen.findByText('main.py')
    fireEvent.click(screen.getByText('main.py'))
    await waitFor(() => expect(api.fsRead).toHaveBeenCalledWith('main.py'))
    fireEvent.click(screen.getByText('simulate-edit'))

    vi.mocked(api.fsWrite).mockRejectedValue(new ApiError(409, 'file changed elsewhere'))
    const save = (window as unknown as { __dcSaveHandler?: () => void }).__dcSaveHandler
    await act(async () => {
      await save!()
    })

    expect(screen.getByText('复制我的版本')).toBeTruthy()
    expect(screen.getByText('强制覆盖')).toBeTruthy()
    expect(screen.getByText('放弃修改')).toBeTruthy()

    vi.mocked(api.fsRead).mockResolvedValue(file('main.py', 'server version\n'))
    fireEvent.click(screen.getByText('放弃修改'))
    await waitFor(() => expect(screen.getByTestId('monaco-mock').textContent).toContain('server version'))
  })
})
