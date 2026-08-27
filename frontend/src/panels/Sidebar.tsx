// ═══════════════════════════════════════════════════════════════
// Sidebar（SWP4-B / WP4-3 §3.5）
// - Props：无（自取数据）。挂载 api.listTasks() + useTaskPolling(1000) 轮询
// - 卡片列表按 updated_at 倒序：type 图标（N4 映射表 + custom ✎）/标题/
//   <MiniProgress steps/>/当前步骤标题/更新时间
// - 新建：textarea + 「启动 ➜」→ 点击前 api.getConfig() 预检（C4/J10：
//   hasApiKey=false 或 lightModel/powerModel 任一为空 → 按钮禁用 + 内联
//   红字，不发起创建）→ api.createTask(desc) → 创建成功一律不自动启动
//   （B4 统一 V2）：提示条「立即启动」（点击才 api.startTask 并跳 /task/:taskId）。
//   统一编排（Monitor 初始编排）：任务创建为空步骤，流程由 Monitor 后台自动
//   生成（总览页 _monitor 事件实时可见）
// - 提交失败（V-04/B5）：ErrorToast 优先展示后端 error 原文（ApiError.
//   message 已按 error→detail→HTTP {status} 提取链收敛，见 WP4-2 §3.3）
// - 底部「设置」链接 /settings +「刷新」按钮立即轮询
// - P0-5：未启动卡片（task active 且全 pending 步骤）显示「▶ 启动」按钮
// ═══════════════════════════════════════════════════════════════

import {useCallback, useEffect, useState} from 'react'
import {Link, useLocation, useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {TaskSummary} from '../api/types'
import ErrorToast from '../components/ErrorToast'
import ConfirmDialog from '../components/ConfirmDialog'
import CreateTaskModal from '../components/CreateTaskModal'
import MiniProgress from '../components/MiniProgress'
import {Icon, typeIconName} from '../components/icons'
import {useTaskPolling} from '../hooks/useTaskPolling'

/** N4 修订：type 图标由 SVG 图标集承担（v2 产品化，替换旧 emoji 映射） */

/** sidebar.html stepLabel 逐字逻辑：active → pending → completed → status */
function stepLabel(task: TaskSummary): string {
  const steps = task.steps || []
  for (const s of steps) if (s.status === 'active') return s.title
  for (const s of steps) if (s.status === 'pending') return s.title
  if (task.status === 'completed') return '已完成'
  return task.status
}

/** P0-5：已创建未启动判定（task status=active 且存在 pending 步骤且无任何非 pending 步骤） */
export function isUnstarted(task: TaskSummary): boolean {
  if (task.status !== 'active') return false
  const steps = task.steps || []
  if (steps.length === 0) return false
  return steps.every((s) => s.status === 'pending')
}

/** 2026-08-23（用户需求）：任务级状态 class——与总览卡片同视觉语言（左侧 3px 色条）
 *  优先级：gate 待审批（橙，等用户处理）> 有 stopped 步骤（红，异常中断）>
 *  执行中（蓝）> 暂停（灰弱化）> 已完成（绿）> 已放弃（暗红，预留终态） */
export function taskStateClass(t: TaskSummary): string {
  const steps = t.steps || []
  if (t.status === 'paused'
      && steps.some((s) => s.status === 'active' && s.human_attention === 'gate')) return 'st-gate'
  if (steps.some((s) => s.status === 'stopped')) return 'st-stopped'
  if (t.status === 'active') return 'st-active'
  if (t.status === 'paused') return 'st-paused'
  if (t.status === 'completed') return 'st-done'
  if (t.status === 'abandoned') return 'st-abandoned'
  return ''
}

/** 2026-08-23（用户反馈）：任务暂停时下一个待执行的 pending 步骤 id——侧栏分段
 *  深灰标记「暂停中断点」（与总览卡片「暂停中」一致）；非 paused 任务返回 undefined */
function pausedFirstPending(t: TaskSummary): string | undefined {
  if (t.status !== 'paused') return undefined
  return [...(t.steps || [])].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
      .find((s) => s.status === 'pending')?.step_id
}

interface CreatedInfo {
  taskId: string
  title: string
}

interface SidebarProps {
  /** 2026-08-25（移动端优化）：抽屉式侧栏——open 挂 open 类，onClose 由遮罩点击触发 */
  open?: boolean
  onClose?: () => void
}

// 默认 noop（无 props 渲染兼容既有用例/桌面端）
const noop = () => {
}

export default function Sidebar({open = false, onClose = noop}: SidebarProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [desc, setDesc] = useState('')
  const [creating, setCreating] = useState(false)
  const [configReady, setConfigReady] = useState(false)
  const [configError, setConfigError] = useState('')
  const [createdTask, setCreatedTask] = useState<CreatedInfo | null>(null)
  const [error, setError] = useState('')
  const [refreshTick, setRefreshTick] = useState(0)
  // 优雅重启（复用原刷新按钮）：确认后请求排空重启；restartMsg 为成功提示
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [restartMsg, setRestartMsg] = useState('')
  // 创建配置弹窗（2026-08-26 用户需求：创建流程可选工作目录）——desc 校验与
  // config 预检通过后弹出，确认后才发起 createTask
  const [wsModalOpen, setWsModalOpen] = useState(false)

  // 创建提示条自动消失（10s 后收起；2026-08-20 移除 × 关闭按钮——顶部提示
  // 统一自动关闭、不留按钮）
  useEffect(() => {
    if (!createdTask) return
    const t = setTimeout(() => setCreatedTask(null), 10000)
    return () => clearTimeout(t)
  }, [createdTask])

  // 优雅重启成功提示自动消失（3s，与 ErrorToast 自动关闭时长一致）
  useEffect(() => {
    if (!restartMsg) return
    const t = setTimeout(() => setRestartMsg(''), 3000)
    return () => clearTimeout(t)
  }, [restartMsg])

  // §3.5.1：挂载 listTasks + useTaskPolling(1000)；refreshTick 变化 → 立即轮询
  const loadTasks = useCallback(() => api.listTasks().then((data) => setTasks(data.tasks)), [])
  useTaskPolling(1000, loadTasks, [loadTasks, refreshTick])

  // §3.5.3：初始 config 预检（C4/J10）——不合格禁用按钮 + 内联红字
  useEffect(() => {
    api
        .getConfig()
        .then((cfg) => {
          const ok = cfg.hasApiKey && !!cfg.lightModel && !!cfg.powerModel
          setConfigReady(ok)
          setConfigError(ok ? '' : '请先在设置页配置 LLM 与双模型')
        })
        .catch(() => {
          setConfigReady(false)
          setConfigError('请先在设置页配置 LLM 与双模型')
        })
  }, [])

  // 当前路由 /task/:taskId → 卡片高亮
  const pathMatch = location.pathname.match(/^\/task\/([^/]+)/)
  const selectedId = pathMatch ? pathMatch[1] : null

  const sortedTasks = [...tasks].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))

  // 创建入口：desc 校验 + config 预检 → 弹「创建配置弹窗」（工作目录选择）；
  // 确认后才调用 doCreate（2026-08-26 用户需求：创建流程可选工作目录）
  const handleCreate = async () => {
    const text = desc.trim()
    if (!text || creating) return
    // C4/J10：点击前 getConfig 预检（每次点击重新检查；不通过不发起创建）
    let cfgOk = false
    try {
      const cfg = await api.getConfig()
      cfgOk = cfg.hasApiKey && !!cfg.lightModel && !!cfg.powerModel
    } catch {
      cfgOk = false
    }
    if (!cfgOk) {
      setConfigReady(false)
      setConfigError('请先在设置页配置 LLM 与双模型')
      return
    }
    setWsModalOpen(true)
  }

  const doCreate = async (workspaceDir?: string) => {
    setCreating(true)
    try {
      // 自动选择（无 workspace_dir）与指定目录（带 workspace_dir）区分调用形态，
      // 保证旧行为不变（createTask 单参）
      const res = workspaceDir
          ? await api.createTask(desc.trim(), {workspace_dir: workspaceDir})
          : await api.createTask(desc.trim())
      setDesc('')
      // 统一编排（Monitor 初始编排）：任务创建为空步骤，流程由 Monitor 在后台
      // 自动生成（总览页 _monitor 事件实时可见），创建后直接提示成功
      setCreatedTask({taskId: res.task_id, title: res.title})
      loadTasks()
    } catch (err) {
      // V-04/B5：优先展示后端 error 原文（提取链已在 ApiError.message 收敛）
      setError(err instanceof Error ? err.message : '创建失败，请稍后重试')
    } finally {
      setCreating(false)
    }
  }

  const handleStart = async (taskId: string, thenNavigate: boolean) => {
    try {
      await api.startTask(taskId)
      // 启动成功后关闭创建提示条（防止跳转详情页返回后提示条仍挂在侧栏）
      setCreatedTask((cur) => (cur && cur.taskId === taskId ? null : cur))
      if (thenNavigate) navigate(`/task/${taskId}`)
      else loadTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动失败，请稍后重试')
    }
  }

  // 优雅重启：等待当前命令完成后自动重启（安全点语义见后端 /api/admin/graceful-restart）
  const handleGracefulRestart = async () => {
    try {
      const res = await api.gracefulRestart('restart')
      setRestartMsg(res.message || '已请求优雅重启')
    } catch (err) {
      setError(err instanceof Error ? err.message : '请求失败，请稍后重试')
    }
  }

  return (
      <>
        {open && <div className="sidebar-backdrop" onClick={onClose}/>}
        <aside className={`sidebar${open ? ' open' : ''}`}>
          <div className="header">
            <span className="dot"/> 工作流任务
          </div>
          {createdTask && (
              <div className="error-toast success">
                {(() => {
                  // 统一编排：步骤未生成前提示「正在生成流程」；步骤生成后提示创建成功。
                  // 不含操作按钮（启动入口在任务卡片/详情页，避免重复按钮；顶部提示
                  // 2026-08-20 起统一自动关闭、不留按钮）
                  const t = tasks.find((x) => x.id === createdTask.taskId)
                  const ready = !!t && (t.steps || []).length > 0
                  return ready
                      ? `任务已创建：${createdTask.title}`
                      : `任务已创建：${createdTask.title}（AI 正在生成流程…）`
                })()}
              </div>
          )}
          <div className="task-list">
            {sortedTasks.length === 0 ? (
                <div className="empty-state">
                  暂无任务
                  <br/>
                  在下方输入描述创建第一个任务
                </div>
            ) : (
                sortedTasks.map((t) => (
                    <div
                        key={t.id}
                        className={`task-card${selectedId === t.id ? ' selected' : ''}${taskStateClass(t) ? ` ${taskStateClass(t)}` : ''}`}
                        onClick={() => navigate(`/task/${t.id}`)}
                    >
                      <div className="title">
                <span className="type-icon">
                  <Icon name={typeIconName(t.type)} size={14}/>
                </span>
                        {t.title || 'Untitled'}
                      </div>
                      <MiniProgress steps={t.steps || []} pausedPendingId={pausedFirstPending(t)}/>
                      <div className="meta">
                        <span>{stepLabel(t)}</span>
                        <span>{t.updated_at ? t.updated_at.substring(11, 16) : ''}</span>
                      </div>
                      {/* P0-5：未启动卡片同步显示启动按钮 */}
                      {isUnstarted(t) && (
                          <button
                              className="resume-btn"
                              onClick={(e) => {
                                e.stopPropagation()
                                handleStart(t.id, false)
                              }}
                          >
                            <Icon name="play" size={12}/>
                            启动
                          </button>
                      )}
                    </div>
                ))
            )}
          </div>
          <div className="new-task-box">
        <textarea
            placeholder="描述你需要做什么…"
            rows={3}
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
        />
            {configError && (
                <div style={{color: 'var(--red)', fontSize: 12, marginTop: 4}}>{configError}</div>
            )}
            <div className="actions">
              <button className="primary" onClick={handleCreate} disabled={!configReady || creating}>
                启动 <Icon name="arrowRight" size={13} gap={2}/>
              </button>
              <Link className="gear-btn" to="/settings" title="设置">
                <Icon name="settings" size={15}/>
              </Link>
              <button
                  className="gear-btn"
                  onClick={() => setConfirmRestart(true)}
                  title="优雅重启服务（等待当前命令完成后重启）"
              >
                <Icon name="refresh" size={14}/>
              </button>
            </div>
          </div>
          {error && <ErrorToast message={error} onClose={() => setError('')}/>}
          {restartMsg && <div className="error-toast success">{restartMsg}</div>}
          <CreateTaskModal
              open={wsModalOpen}
              onConfirm={(workspaceDir) => {
                setWsModalOpen(false)
                void doCreate(workspaceDir)
              }}
              onCancel={() => setWsModalOpen(false)}
          />
          <ConfirmDialog
              open={confirmRestart}
              title="优雅重启服务"
              rows={[]}
              confirmText="重启"
              description="将等待当前正在执行的命令完成后自动重启；期间不再启动新命令，运行中的任务会从待执行恢复（可重试），无进度损失。"
              onCancel={() => setConfirmRestart(false)}
              onConfirm={() => {
                setConfirmRestart(false)
                handleGracefulRestart()
              }}
          />
        </aside>
      </>
  )
}
