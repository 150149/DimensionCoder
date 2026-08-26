import {act, cleanup, fireEvent, render, screen} from '@testing-library/react'
import {afterEach, describe, expect, it, vi} from 'vitest'
import ChatMessage from '../components/ChatMessage'
import ConfirmDialog from '../components/ConfirmDialog'
import ErrorToast from '../components/ErrorToast'
import InterveneBar from '../components/InterveneBar'
import MiniProgress from '../components/MiniProgress'
import ProgressRail from '../components/ProgressRail'
import ToolCard from '../components/ToolCard'

describe('components smoke', () => {
  afterEach(() => {
    vi.useRealTimers()
    cleanup()
  })

  it('ChatMessage 渲染 user 气泡与 system 折叠', () => {
    render(<ChatMessage msg={{role: 'user', content: '你好，帮我做一件事'}}/>)
    expect(screen.getByText('你好，帮我做一件事')).toBeTruthy()

    render(<ChatMessage msg={{role: 'system', content: 'system prompt 内容'}}/>)
    expect(screen.getByText(/系统提示/)).toBeTruthy()
    expect(screen.getByText('system prompt 内容')).toBeTruthy()
  })

  it('ChatMessage assistant 用 react-markdown 渲染', () => {
    render(<ChatMessage msg={{role: 'assistant', content: '**加粗** 与 `code`'}}/>)
    expect(screen.getByText('加粗').tagName).toBe('STRONG')
  })

  it('ToolCard 渲染工具名与输入输出', () => {
    render(
        <ToolCard
            callId="c1"
            toolName="read_file"
            input={{path: '/a.txt'}}
            output="文件内容"
            status="done"
        />,
    )
    expect(screen.getByText('读取文件')).toBeTruthy()

    expect(document.querySelector('.tool-summary')?.textContent).toContain('/a.txt')

    fireEvent.click(screen.getByText('读取文件'))
    expect(screen.getByText('输入')).toBeTruthy()
    expect(screen.getByText('输出')).toBeTruthy()
    expect(screen.getByText('文件内容')).toBeTruthy()
    expect(document.querySelector('.tool-summary')).toBeNull()

    fireEvent.click(screen.getByText('读取文件'))
    expect(document.querySelector('.tool-summary')?.textContent).toContain('/a.txt')
  })

  it('ToolCard running 状态：自动展开 + loading 提示，结果返回后保持展开', () => {
    const {rerender} = render(
        <ToolCard callId="c9" toolName="run_cmd" input={{command: 'npm test'}} status="running"/>,
    )

    expect(document.querySelector('.tool-panel')?.className).toContain('expanded')
    expect(document.querySelector('.tool-panel')?.textContent).toContain('npm test')
    expect(document.querySelectorAll('.loading-spinner').length).toBeGreaterThan(0)

    rerender(<ToolCard callId="c9" toolName="run_cmd" input={{command: 'npm test'}} output="passed" status="done"/>)
    expect(document.querySelector('.tool-panel')?.className).toContain('expanded')
    expect(document.querySelector('.loading-spinner')).toBeNull()
    expect(document.querySelector('.tool-panel')?.textContent).toContain('passed')
  })

  it('ToolCard 流式拼接 input（JSON 字符串）也能提取摘要', () => {

    render(
        <ToolCard
            callId="c2"
            toolName="read_file"
            input='{"file_path":"/tmp/a.txt","start_line":1}'
            output="内容"
            status="done"
        />,
    )
    expect(screen.getByText('读取文件')).toBeTruthy()
    expect(document.querySelector('.tool-summary')?.textContent).toContain('/tmp/a.txt')
  })

  it('ToolCard dcflow_sim：中文标签 + 完整人话摘要（动词+对象+参数）', () => {
    render(
        <ToolCard
            callId="c3"
            toolName="dcflow_sim"
            input='{"action":"run","until_addr":"0x404112","timeout_seconds":180}'
            status="done"
        />,
    )

    expect(screen.getByText('模拟器')).toBeTruthy()

    expect(document.querySelector('.tool-summary')?.textContent).toContain('模拟执行至 0x404112')
  })

  it('ToolCard dcflow_sim 摘要分支：patch/mem/load/regs 及新分析 action', () => {
    const {rerender} = render(
        <ToolCard callId="c4" toolName="sim" input={{action: 'patch', addr: '0x402DA0', data: '31 c0'}} status="done"/>,
    )
    expect(document.querySelector('.tool-summary')?.textContent).toContain('修改内存 0x402DA0 字节')

    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'mem', addr: '0x4057d8', size: 16}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('读取内存 0x4057d8（16 字节）')
    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'load', exe: 'C:\\x\\cm.exe'}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('加载程序 cm.exe')

    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'regs'}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('查看寄存器状态')

    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'deobf', addr: '0x402da0'}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('去混淆分析 0x402da0')
    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'symexec', addr: '0x404112'}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('符号执行 0x404112')
    rerender(<ToolCard callId="c4" toolName="sim" input={{action: 'blackhole'}} status="done"/>)
    expect(document.querySelector('.tool-summary')?.textContent).toContain('黑洞探测报告')
  })

  it('ProgressRail 渲染步骤圆点与连接线', () => {
    const steps = [
      {step_id: 's1', title: '分析', status: 'completed'},
      {step_id: 's2', title: '实施', status: 'active'},
      {step_id: 's3', title: '审查', status: 'pending'},
    ]
    const onDotClick = vi.fn()
    render(<ProgressRail steps={steps} currentStepId="s2" onDotClick={onDotClick}/>)
    expect(screen.getByText('分析')).toBeTruthy()
    expect(screen.getByText('实施')).toBeTruthy()
    expect(screen.getByText('审查')).toBeTruthy()
    const dots = document.querySelectorAll('.pt-dot')
    expect(dots.length).toBe(3)
    expect(document.querySelectorAll('.pt-rail').length).toBe(2)
    expect(dots[0].className).toContain('pt-dot-done')
    expect(dots[1].className).toContain('pt-dot-active')
    fireEvent.click(screen.getByText('分析'))
    expect(onDotClick).toHaveBeenCalledWith('s1')
  })

  it('ProgressRail gate/stopped 状态色（2026-08-23 用户需求）', () => {

    const steps = [
      {step_id: 's1', title: '分析', status: 'completed'},
      {step_id: 's2', title: '审批', status: 'active', human_attention: 'gate'},
      {step_id: 's3', title: '实施', status: 'stopped'},
    ]
    render(<ProgressRail steps={steps}/>)
    const dots = document.querySelectorAll('.pt-dot')
    expect(dots.length).toBe(3)
    expect(dots[0].className).toContain('pt-dot-done')
    expect(dots[1].className).toContain('pt-dot-gate')
    expect(dots[2].className).toContain('pt-dot-stopped')
  })

  it('ProgressRail 任务暂停：下一个待执行步骤深灰圆点（2026-08-23 用户反馈）', () => {
    const steps = [
      {step_id: 's1', title: '分析', status: 'pending'},
      {step_id: 's2', title: '实施', status: 'pending'},
    ]
    render(<ProgressRail steps={steps} pausedPendingId="s1"/>)
    const dots = document.querySelectorAll('.pt-dot')
    expect(dots[0].className).toContain('pt-dot-paused')
    expect(dots[1].className).toContain('pt-dot-pending')
  })

  it('ProgressRail 折叠：22 个 completed → 折叠 7+（显示 16 个）', () => {
    const steps = Array.from({length: 22}, (_, i) => ({step_id: `s${i + 1}`, title: `步骤${i + 1}`, status: 'completed' as const}))
    render(<ProgressRail steps={steps}/>)
    expect(screen.getByText('7+')).toBeTruthy()
    expect(screen.queryByText('步骤1')).toBeNull()
    expect(screen.queryByText('步骤7')).toBeNull()
    expect(screen.getByText('步骤8')).toBeTruthy()
    expect(screen.getByText('步骤22')).toBeTruthy()
    expect(document.querySelectorAll('.pt-dot').length).toBe(16)
  })

  it('ProgressRail 折叠：31 步（20 completed + active + 10 pending）→ 折叠 16+', () => {
    const steps = [
      ...Array.from({length: 20}, (_, i) => ({step_id: `s${i + 1}`, title: `步骤${i + 1}`, status: 'completed' as const})),
      {step_id: 'a', title: 'A', status: 'active' as const},
      ...Array.from({length: 10}, (_, i) => ({step_id: `p${i + 1}`, title: `后续${i + 1}`, status: 'pending' as const})),
    ]
    render(<ProgressRail steps={steps}/>)
    expect(screen.getByText('16+')).toBeTruthy()
    expect(screen.queryByText('步骤1')).toBeNull()
    expect(screen.getByText('步骤17')).toBeTruthy()
    expect(screen.getByText('A')).toBeTruthy()
    expect(screen.getByText('后续10')).toBeTruthy()
    expect(document.querySelectorAll('.pt-dot').length).toBe(16)
  })

  it('ProgressRail 折叠：连续 completed 不足 needFold → 折 foldAll 个（尽力 ≤16）', () => {
    const steps = [
      ...Array.from({length: 5}, (_, i) => ({step_id: `s${i + 1}`, title: `步骤${i + 1}`, status: 'completed' as const})),
      {step_id: 'x', title: '中断', status: 'stopped' as const},
      ...Array.from({length: 25}, (_, i) => ({step_id: `t${i + 1}`, title: `后续${i + 1}`, status: 'pending' as const})),
    ]
    render(<ProgressRail steps={steps}/>)
    expect(screen.getByText('5+')).toBeTruthy()
    expect(screen.queryByText('步骤1')).toBeNull()
    expect(screen.getByText('中断')).toBeTruthy()
    expect(screen.getByText('后续25')).toBeTruthy()
  })

  it('ProgressRail 不折叠：≤16 步', () => {
    const steps = Array.from({length: 16}, (_, i) => ({step_id: `s${i + 1}`, title: `步骤${i + 1}`, status: 'completed' as const}))
    render(<ProgressRail steps={steps}/>)
    expect(screen.queryByText(/\d+\+/)).toBeNull()
    expect(screen.getByText('步骤1')).toBeTruthy()
  })

  it('MiniProgress 渲染分段条', () => {
    const steps = [{status: 'completed'}, {status: 'active'}, {status: 'pending'}, {status: 'stopped'}]
    render(<MiniProgress steps={steps}/>)
    const segs = document.querySelectorAll('.mini-seg')
    expect(segs.length).toBe(4)
    expect(segs[0].className).toContain('seg-done')
    expect(segs[1].className).toContain('seg-active')
    expect(segs[2].className).toContain('seg-pending')

    expect(segs[3].className).toContain('seg-stopped')
  })

  it('MiniProgress 任务暂停：下一个待执行步骤深灰分段（2026-08-23 用户反馈）', () => {
    const steps = [{step_id: 's1', status: 'pending'}, {step_id: 's2', status: 'pending'}]
    render(<MiniProgress steps={steps} pausedPendingId="s1"/>)
    const segs = document.querySelectorAll('.mini-seg')
    expect(segs[0].className).toContain('seg-paused')
    expect(segs[1].className).toContain('seg-pending')
  })

  it('MiniProgress gate 待审批分段色（2026-08-23 用户需求）', () => {

    const steps = [{status: 'active', human_attention: 'gate'}, {status: 'stopped'}]
    render(<MiniProgress steps={steps}/>)
    const segs = document.querySelectorAll('.mini-seg')
    expect(segs[0].className).toContain('seg-gate')
    expect(segs[1].className).toContain('seg-stopped')
  })

  it('MiniProgress 折叠：22 个 completed → 合并 7+ 段 + 段旁徽标（16 段）', () => {
    const steps = Array.from({length: 22}, (_, i) => ({step_id: `s${i + 1}`, status: 'completed' as const}))
    render(<MiniProgress steps={steps}/>)
    const segs = document.querySelectorAll('.mini-seg')
    expect(segs.length).toBe(16)
    expect(document.querySelector('[title="7+"]')).toBeTruthy()
    expect(screen.getByText('+7')).toBeTruthy()
    expect(document.querySelectorAll('.mini-seg.seg-done').length).toBe(16)
  })

  it('MiniProgress 不折叠：12 个 completed（≤16 步）', () => {
    const steps = Array.from({length: 12}, (_, i) => ({step_id: `s${i + 1}`, status: 'completed' as const}))
    render(<MiniProgress steps={steps}/>)
    expect(document.querySelectorAll('.mini-seg').length).toBe(12)
  })

  it('InterveneBar flow 模式：空闲发送；运行中+输入 → 待发送区 + 强制介入', () => {
    const onSend = vi.fn()
    const onForce = vi.fn()
    const {rerender} = render(<InterveneBar mode="flow" onSend={onSend} onForce={onForce}/>)

    const sendBtn = document.querySelector('.intervene-bar .send-btn') as HTMLButtonElement
    expect(sendBtn).toBeTruthy()
    expect(sendBtn.disabled).toBe(true)
    expect(document.querySelector('.pending-msg')).toBeNull()

    const textarea = document.querySelector('.intervene-bar textarea') as HTMLTextAreaElement
    fireEvent.change(textarea, {target: {value: '请调整流程'}})
    expect(sendBtn.disabled).toBe(false)
    fireEvent.click(sendBtn)
    expect(onSend).toHaveBeenCalledWith('请调整流程')

    rerender(<InterveneBar mode="flow" onSend={onSend} onForce={onForce} running/>)
    fireEvent.change(textarea, {target: {value: '立即调整'}})
    const pending = document.querySelector('.pending-msg')
    expect(pending?.textContent).toContain('待发送：立即调整')
    const forceBtn = document.querySelector('.intervene-bar .force-btn') as HTMLButtonElement
    expect(forceBtn).toBeTruthy()
    fireEvent.click(forceBtn)
    expect(onForce).toHaveBeenCalledWith('立即调整')

    fireEvent.change(textarea, {target: {value: ''}})
    expect(document.querySelector('.pending-msg')).toBeNull()
    expect(document.querySelector('.intervene-bar .force-btn')).toBeNull()
  })

  it('InterveneBar step 模式：压缩/恢复图标；运行中+空输入 → 终止按钮', () => {
    const onStop = vi.fn()
    render(
        <InterveneBar mode="step" onSend={() => {
        }} onForce={() => {
        }} onStop={onStop} onResume={() => {
        }} onCompress={() => {
        }}/>,
    )
    expect(document.querySelector('.intervene-bar .compress-btn')).toBeTruthy()
    expect(document.querySelector('.intervene-bar .resume-btn')).toBeTruthy()

    expect(document.querySelector('.intervene-bar .stop-btn')).toBeNull()

    render(
        <InterveneBar mode="step" onSend={() => {
        }} onForce={() => {
        }} onStop={onStop} running/>,
    )
    const stopBtn = document.querySelector('.intervene-bar .stop-btn') as HTMLButtonElement
    expect(stopBtn).toBeTruthy()
    fireEvent.click(stopBtn)
    expect(onStop).toHaveBeenCalled()
  })

  it('ConfirmDialog 渲染确认行与按钮', () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(
        <ConfirmDialog
            open
            title="确认创建自定义任务"
            rows={[
              {step_id: 'step-1', title: '需求分析', required: true, human_attention: 'gate'},
              {step_id: 'step-2', title: '编码', required: false},
            ]}
            onConfirm={onConfirm}
            onCancel={onCancel}
        />,
    )
    expect(screen.getByText('确认创建自定义任务')).toBeTruthy()
    expect(screen.getByText('需求分析')).toBeTruthy()
    expect(screen.getByText('编码')).toBeTruthy()
    fireEvent.click(screen.getByText('确认创建'))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('ErrorToast 渲染并 3s 自动消失', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    render(<ErrorToast message="操作失败" onClose={onClose}/>)
    expect(screen.getByText('操作失败')).toBeTruthy()
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
