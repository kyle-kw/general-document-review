import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Files } from 'lucide-react'
import { NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api } from '@/lib/api'
import { ErrorState } from '@/components/Shared'
import { HomePage } from '@/pages/HomePage'
import { HistoryPage } from '@/pages/HistoryPage'
import { ReviewPage } from '@/pages/ReviewPage'

export function App() {
  const session = useQuery({ queryKey: ['session'], queryFn: api.session, staleTime: Infinity, retry: 1 })
  const location = useLocation()

  useEffect(() => {
    document.getElementById('main-content')?.focus({ preventScroll: true })
  }, [location.pathname, session.isSuccess])

  return <div className="app-shell">
    <a href="#main-content" className="skip-link">跳到主要内容</a>
    <header className="topbar"><div className="topbar-inner">
      <NavLink to="/" className="brand" aria-label="文审首页"><span className="brand-mark"><Files size={22} aria-hidden="true" /></span>文审</NavLink>
      <nav aria-label="主导航">
        <NavLink to="/" end className={({ isActive }) => `nav-item ${isActive ? 'selected' : ''}`}>新建审查</NavLink>
        <NavLink to="/history" className={({ isActive }) => `nav-item ${isActive ? 'selected' : ''}`}>审查记录</NavLink>
      </nav>
    </div></header>
    {session.isPending ? <main id="main-content" tabIndex={-1} className="page"><div className="loading-state" role="status">正在连接服务…</div></main> : session.isError ? <main id="main-content" tabIndex={-1} className="page"><ErrorState message={session.error.message} onRetry={() => void session.refetch()} /></main> : <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/history" element={<HistoryPage />} />
      <Route path="/reviews/:id" element={<ReviewPage key={location.pathname} />} />
      <Route path="*" element={<main id="main-content" tabIndex={-1} className="page"><ErrorState message="没有找到这个页面。" /><NavLink className="button secondary" to="/">返回新建审查</NavLink></main>} />
    </Routes>}
  </div>
}
