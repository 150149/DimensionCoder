import {useEffect, useRef, useState} from 'react'
import {Icon, toolIconName} from './icons'

interface ToolCardProps {
  callId: string
  toolName: string
  input: unknown
  output?: string
  status: 'running' | 'done'
  defaultExpanded?: boolean

  onToggle?: () => void

  autoScroll?: boolean
}

const TOOL_LABELS: Record<string, string> = {
  list_dir: '浏览目录',
  read_file: '读取文件',
  write_file: '写入文件',
  edit_file: '修改文件',
  search_code: '搜索代码',
  run_cmd: '执行命令',
  read_doc: '读取文档',
  step_done: '步骤完成',
  list_steps: '查看步骤',
  adjust_flow: '调整流程',
  sim: '模拟器',

  get_decompiled_code: '反编译',
  extract_constants: '常量提取',
  search_bytes: '字节搜索',
  solve_z3: 'Z3求解',
}

function stripPrefix(name: string): string {
  return name.startsWith('dcflow_') ? name.slice(7) : name
}

function inputSummary(toolName: string, input: unknown): string {
  if (input == null) return ''

  let o: Record<string, unknown> | null = null
  if (typeof input === 'object') {
    o = input as Record<string, unknown>
  } else if (typeof input === 'string' && input.trim()) {
    try {
      o = JSON.parse(input) as Record<string, unknown>
    } catch {

      return input.trim().split('\n')[0].slice(0, 80)
    }
  }
  if (!o) return ''
  const pick = (keys: string[]): string => {
    for (const k of keys) {
      const v = o[k]
      if (typeof v === 'string' && v.trim()) return v.trim()
      if (typeof v === 'number') return String(v)
    }
    return ''
  }
  let s = ''
  const t = stripPrefix(toolName)
  if (t === 'run_cmd') s = pick(['command', 'cmd']).split('\n')[0]
  else if (t === 'search_code') s = pick(['pattern', 'query']).split('\n')[0]
  else if (t === 'adjust_flow') s = pick(['action', 'reasoning'])
  else if (t === 'sim') {

    const action = pick(['action'])
    const addr = pick(['until_addr', 'addr'])
    if (action === 'run') s = addr ? `模拟执行至 ${addr}` : '模拟执行'
    else if (action === 'patch') s = addr ? `修改内存 ${addr} 字节` : '修改内存字节'
    else if (action === 'mem') {
      const size = pick(['size'])
      s = addr ? `读取内存 ${addr}${size ? `（${size} 字节）` : ''}` : '读取内存'
    } else if (action === 'dump') s = addr ? `导出内存 ${addr} 到文件` : '导出内存到文件'
    else if (action === 'write') s = addr ? `写入内存 ${addr}` : '写入内存'
    else if (action === 'load') {
      const exe = pick(['exe'])
      s = exe ? `加载程序 ${exe.split(/[\\/]/).pop()}` : '加载程序'
    } else if (action === 'regs') s = '查看寄存器状态'
    else if (action === 'hook') s = '开启执行流记录'
    else if (action === 'snapshot') s = '保存运行快照'
    else if (action === 'restore') s = '恢复运行快照'
    else if (action === 'replay') s = '重放输入验证'
    else if (action === 'trace') s = '查看执行流记录'
    else if (action === 'dyncode') s = '查看动态生成代码'
    else if (action === 'antidbg') s = '反调试行为报告'
    else if (action === 'output') s = '查看程序输出'
    else if (action === 'status') s = '查看模拟器状态'
    else if (action === 'cleanup') s = '清理模拟器会话'
    else if (action === 'deobf') s = addr ? `去混淆分析 ${addr}` : '去混淆分析'
    else if (action === 'fixcfg') s = addr ? `控制流推演 ${addr}` : '控制流推演'
    else if (action === 'symexec') s = addr ? `符号执行 ${addr}` : '符号执行'
    else if (action === 'blackhole') s = '黑洞探测报告'
    else if (action === 'fast' || action === 'ff') s = addr ? `安全区快跑至 ${addr}` : '安全区快跑'
    else if (action) s = `模拟器操作：${action}`
  } else s = pick(['path', 'file_path', 'dir_path', 'file', 'doc', 'fn', 'title'])
  return s
}

function formatInput(input: unknown): string {

  if (input == null) return '{}'
  if (typeof input === 'string') return input
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

export default function ToolCard({
                                   toolName,
                                   input,
                                   output,
                                   status,
                                   defaultExpanded,
                                   onToggle,
                                   autoScroll,
                                 }: ToolCardProps) {

  const [expanded, setExpanded] = useState(defaultExpanded === true || status === 'running')

  useEffect(() => {
    if (defaultExpanded !== undefined) setExpanded(defaultExpanded)
  }, [defaultExpanded])

  const inputRef = useRef<HTMLPreElement>(null)
  const outputRef = useRef<HTMLPreElement>(null)
  const inputText = typeof input === 'string' ? input : ''
  useEffect(() => {
    if (autoScroll && inputRef.current) inputRef.current.scrollTop = inputRef.current.scrollHeight
  }, [inputText, autoScroll])
  useEffect(() => {
    if (autoScroll && outputRef.current) outputRef.current.scrollTop = outputRef.current.scrollHeight
  }, [output, autoScroll])

  const toggle = () => {
    setExpanded((v) => !v)
    onToggle?.()
  }

  const trimmedOutput = output != null && output.length > 20000 ? `${output.slice(0, 20000)}…` : output
  const label = TOOL_LABELS[stripPrefix(toolName)] ?? (toolName || '(工具)')
  const summary = inputSummary(toolName, input)

  return (
      <div className={`tool-panel${expanded ? ' expanded' : ''}`}>
        <div className="tool-header" onClick={toggle}>
        <span className="tool-arrow">
          <Icon name="chevronRight" size={11} gap={0}/>
        </span>
          <span className="tool-icon">
          <Icon name={toolIconName(toolName)} size={14} gap={0}/>
        </span>
          <span className="tool-name">{label}</span>
          {}
          {!expanded && summary && <span className="tool-summary" title={summary}>{summary}</span>}
          {status === 'running' && <span className="loading-spinner"/>}
        </div>
        <div className="tool-body">
          {(input == null || input !== '') && (
              <div className="tool-section">
                <div className="tool-section-label">输入</div>
                <pre ref={inputRef}>{formatInput(input)}</pre>
              </div>
          )}
          {trimmedOutput != null && trimmedOutput !== '' && (
              <div className="tool-section">
                <div className="tool-section-label">输出</div>
                <pre ref={outputRef}>{trimmedOutput}</pre>
              </div>
          )}
        </div>
      </div>
  )
}
