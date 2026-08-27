// ═══════════════════════════════════════════════════════════════
// ToolCard（WP4-1 §3 组件清单：props {callId, toolName, input, output?,
//   status: "running"|"done", defaultExpanded?}）
// callId 关联的工具卡片（WP4-3 §3.7 第 4 条）：
// - 可折叠 .tool-panel（.expanded / .tool-arrow 旋转动画）
// - 头部：中文工具名 + 参数摘要（用户要求：不展示 dcflow_* 原始名，
//   如「读取文件 a.ts」「执行命令 npm test」；摘要取关键参数省略展示）
// - 正文：tool_input JSON 格式化 + tool_output 前 500 字符
// - running 状态显示加载指示（loading-spinner）
// ═══════════════════════════════════════════════════════════════

import {useEffect, useRef, useState} from 'react'
import {Icon, toolIconName} from './icons'

interface ToolCardProps {
  callId: string
  toolName: string
  input: unknown
  output?: string
  status: 'running' | 'done'
  defaultExpanded?: boolean
  /** 受控展开回调（ChatMessage 的 onToggleTool 接入） */
  onToggle?: () => void
  /** V-19：内容（输入/输出）增长时自动滚动到底（与全局自动滚动联动） */
  autoScroll?: boolean
}

/** 工具名 → 中文标签（折叠态头部；兼容带/不带 dcflow_ 前缀） */
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
  // 2026-08-24（逆向专家）：ctf_tool 融入项目后的 4 个逆向工具
  get_decompiled_code: '反编译',
  extract_constants: '常量提取',
  search_bytes: '字节搜索',
  solve_z3: 'Z3求解',
}

function stripPrefix(name: string): string {
  return name.startsWith('dcflow_') ? name.slice(7) : name
}

/** 按工具提取关键参数作为摘要（路径显示完整绝对路径；命令/正则取首行） */
function inputSummary(toolName: string, input: unknown): string {
  if (input == null) return ''
  // 流式路径（V-18）：input 是 toolCallParam 逐片拼接的 JSON 文本字符串；
  // DB 回读路径是对象——先归一化为对象，否则流式期间摘要缺失（只显示
  // 工具名无参数，刷新后才正常）。
  let o: Record<string, unknown> | null = null
  if (typeof input === 'object') {
    o = input as Record<string, unknown>
  } else if (typeof input === 'string' && input.trim()) {
    try {
      o = JSON.parse(input) as Record<string, unknown>
    } catch {
      // 非 JSON 纯文本参数 → 直接截取首行作为摘要
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
    // dcflow_sim：摘要用「动词 + 对象 + 关键参数」的完整人话，
    // 直白可读（如「查看寄存器状态」而非「寄存器」）
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
  // 2026-08-22：无参调用（arguments={} 落库 tool_input=None，DB 实证
  // 1d5def81 dcflow_adjust_flow 缺 task_id）→ 显示空对象 {}，不空白
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
  // running 卡自动展开（loading 可见）；defaultExpanded 未传时不覆盖（undefined）
  const [expanded, setExpanded] = useState(defaultExpanded === true || status === 'running')

  useEffect(() => {
    if (defaultExpanded !== undefined) setExpanded(defaultExpanded)
  }, [defaultExpanded])

  // V-19：自动滚动开启时，输入/输出内容增长（参数流式追加/结果到达）→ 滚到底
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

  // 输出截断：保留较长结果（pre 区内滚动），仅防极端超大输出（后端事件已限 5000）
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
          {/* 展开态标题只保留工具名（参数/路径已在正文可见，避免标题重复冗长） */}
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
