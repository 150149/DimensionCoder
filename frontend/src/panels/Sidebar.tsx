import {useCallback, useEffect, useState} from 'react'
import {Link, useLocation, useNavigate} from 'react-router-dom'
import {api} from '../api/client'
import type {TaskSummary} from '../api/types'
import ErrorToast from '../components/ErrorToast'
import ConfirmDialog from '../components/ConfirmDialog'
import MiniProgress from '../components/MiniProgress'
import {Icon, typeIconName} from '../components/icons'
import {useTaskPolling} from '../hooks/useTaskPolling'

function stepLabel(task: TaskSummary): string {
  const steps = task.steps || []
  for (const s of steps) if (s.status === 'active') return s.title
  for (const s of steps) if (s.status === 'pending') return s.title
  if (task.status === 'completed') return '已完成'
  return task.status
}

export function isUnstarted(task: TaskSummary): boolean {
  if (task.status !== 'active') return false
  const steps = task.steps || []
  if (steps.length === 0) return false
  return steps.every((s) => s.status === 'pending')
}

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

  open?: boolean
  onClose?: () => void
}

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

  const [confirmRestart, setConfirmRestart] = useState(false)
  const [restartMsg, setRestartMsg] = useState('')

  useEffect(() => {
    if (!createdTask) return
    const t = setTimeout(() => setCreatedTask(null), 10000)
    return () => clearTimeout(t)
  }, [createdTask])

  useEffect(() => {
    if (!restartMsg) return
    const t = setTimeout(() => setRestartMsg(''), 3000)
    return () => clearTimeout(t)
  }, [restartMsg])

  const loadTasks = useCallback(() => api.listTasks().then((data) => setTasks(data.tasks)), [])
  useTaskPolling(1000, loadTasks, [loadTasks, refreshTick])

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

  const pathMatch = location.pathname.match(/^\/task\/([^/]+)/)
  const selectedId = pathMatch ? pathMatch[1] : null

  const sortedTasks = [...tasks].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))

  const handleCreate = async () => {
    const text = desc.trim()
    if (!text || creating) return

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
    setCreating(true)
    try {
      const res = await api.createTask(text)
      setDesc('')

      setCreatedTask({taskId: res.task_id, title: res.title})
      loadTasks()
    } catch (err) {

      setError(err instanceof Error ? err.message : '创建失败，请稍后重试')
    } finally {
      setCreating(false)
    }
  }

  const handleStart = async (taskId: string, thenNavigate: boolean) => {
    try {
      await api.startTask(taskId)

      setCreatedTask((cur) => (cur && cur.taskId === taskId ? null : cur))
      if (thenNavigate) navigate(`/task/${taskId}`)
      else loadTasks()
    } catch (err) {
      setError(err instanceof Error ? err.message : '启动失败，请稍后重试')
    }
  }

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
                      {}
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
