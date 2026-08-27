import {Navigate, Route, Routes, useLocation, useParams} from 'react-router-dom'
import {useEffect, useState} from 'react'
import Sidebar from './panels/Sidebar'
import FlowOverview from './panels/FlowOverview'
import StepDetail from './panels/StepDetail'
import MonitorDetail from './panels/MonitorDetail'
import SettingsPage from './config/SettingsPage'
import {Icon} from './components/icons'

// WP4-1 §3.1 路由表（逐行固定，无登录守卫、无 /login 路由）：
//   /                              → FlowOverview
//   /task/:taskId                  → FlowOverview（带 taskId 参数）
//   /task/:taskId/step/:stepId     → StepDetail
//   /task/:taskId/monitor/:stepId  → MonitorDetail
//   /settings                      → SettingsPage
//   *                              → 重定向 /
// 面板组件占位导出（E7：与 SWP4-B/C 真实组件同路径同名，本文件 import 路径一经写入不再改）。

function FlowOverviewRoute() {
  const {taskId} = useParams()
  return <FlowOverview taskId={taskId}/>
}

function StepDetailRoute() {
  const {taskId, stepId} = useParams()
  return <StepDetail taskId={taskId ?? ''} stepId={stepId ?? ''}/>
}

function MonitorDetailRoute() {
  const {taskId, stepId} = useParams()
  return <MonitorDetail taskId={taskId ?? ''} stepId={stepId ?? ''}/>
}

export default function App() {
  // 2026-08-25（移动端优化）：抽屉式侧栏——<768px 时侧栏默认隐藏，汉堡按钮展开；
  // 路由切换自动收起（避免抽屉遮住新页面内容）
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()
  useEffect(() => {
    setSidebarOpen(false)
  }, [location.pathname])

  return (
      <div className="app-shell">
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)}/>
        <main className="app-main">
          <button
              className="hamburger-btn"
              aria-label="打开侧边栏"
              onClick={() => setSidebarOpen((v) => !v)}
          >
            <Icon name="list" size={18}/>
          </button>
          <Routes>
            <Route path="/" element={<FlowOverviewRoute/>}/>
            <Route path="/task/:taskId" element={<FlowOverviewRoute/>}/>
            <Route path="/task/:taskId/step/:stepId" element={<StepDetailRoute/>}/>
            <Route path="/task/:taskId/monitor/:stepId" element={<MonitorDetailRoute/>}/>
            <Route path="/settings" element={<SettingsPage/>}/>
            <Route path="*" element={<Navigate to="/" replace/>}/>
          </Routes>
        </main>
      </div>
  )
}
