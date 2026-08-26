import {Navigate, Route, Routes, useLocation, useParams} from 'react-router-dom'
import {useEffect, useState} from 'react'
import Sidebar from './panels/Sidebar'
import FlowOverview from './panels/FlowOverview'
import StepDetail from './panels/StepDetail'
import MonitorDetail from './panels/MonitorDetail'
import SettingsPage from './config/SettingsPage'
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

export default function App() {

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
