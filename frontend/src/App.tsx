import {Navigate, Route, Routes, useLocation, useParams} from 'react-router-dom'
import {useEffect, useState} from 'react'
import Sidebar from './panels/Sidebar'
import FlowOverview from './panels/FlowOverview'
import StepDetail from './panels/StepDetail'
import MonitorDetail from './panels/MonitorDetail'
import ChatDetail from './panels/ChatDetail'
import SettingsPage from './config/SettingsPage'
import LoginPage from './panels/LoginPage'
import {getAuthStatus} from './auth/api'
import {Icon} from './components/icons'


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

function ChatDetailRoute() {
  const {taskId} = useParams()
  return <ChatDetail taskId={taskId ?? ''}/>
}

export default function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()
  useEffect(() => {
    setSidebarOpen(false)
  }, [location.pathname])

  const [authLocked, setAuthLocked] = useState<boolean | null>(null)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const st = await getAuthStatus()
        if (!cancelled) setAuthLocked(Boolean(st.enabled && !st.authenticated))
      } catch {
        if (!cancelled) setAuthLocked(false)
      }
    })()
    const onExpired = () => setAuthLocked(true)
    window.addEventListener('dc:auth-expired', onExpired)
    return () => {
      cancelled = true
      window.removeEventListener('dc:auth-expired', onExpired)
    }
  }, [])

  if (authLocked === null) {
    return <div className="app-shell auth-loading" style={{display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '100vh'}}>检查登录状态…</div>
  }
  if (authLocked) {
    return <LoginPage onLoggedIn={() => setAuthLocked(false)}/>
  }

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
            <Route path="/task/:taskId/chat" element={<ChatDetailRoute/>}/>
            <Route path="/settings" element={<SettingsPage/>}/>
            <Route path="*" element={<Navigate to="/" replace/>}/>
          </Routes>
        </main>
      </div>
  )
}
