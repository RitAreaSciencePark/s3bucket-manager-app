import { useEffect } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { adminAPI, authStorage } from '../../services/api'

const NAV_ITEMS: Array<{ to: string; label: string; external?: boolean }> = [
  { to: '/admin/buckets', label: 'Buckets' },
  { to: '/admin/users', label: 'Users' },
  { to: '/admin/uo-mappings', label: 'UO Mappings' },
  { to: '/admin/tenants', label: 'Tenants' },
  { to: '/admin/sync', label: 'Sync' },
  { to: '/admin/file-rules', label: 'File Rules' },
  { to: '/admin/file-deviations', label: 'Deviations' },
  { to: '/admin/file-formats', label: 'File Formats' },
  { to: '/admin/tenant-docs', label: 'Tenant Docs' },
  { to: '/api/docs/', label: 'API Docs', external: true },
]

function AdminLayout() {
  const navigate = useNavigate()
  const adminUser = authStorage.getAdminUsername()

  useEffect(() => {
    if (!adminAPI.isAuthenticated()) {
      navigate('/admin/login', { replace: true })
    }
  }, [navigate])

  const handleLogout = () => {
    adminAPI.logout()
    navigate('/admin/login', { replace: true })
  }

  if (!adminAPI.isAuthenticated()) return null

  return (
    <div className="admin-layout">
      <aside className="admin-sidebar">
        <div className="admin-sidebar-header">
          <h2>Buckets Explorer</h2>
          <span className="admin-sidebar-subtitle">Admin Panel</span>
        </div>
        <nav className="admin-nav">
          {NAV_ITEMS.map((item) =>
            'external' in item && item.external ? (
              <a
                key={item.to}
                href={item.to}
                target="_blank"
                rel="noopener noreferrer"
                className="admin-nav-link"
              >
                {item.label}
              </a>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => `admin-nav-link${isActive ? ' active' : ''}`}
              >
                {item.label}
              </NavLink>
            )
          )}
        </nav>
        <div className="admin-sidebar-footer">
          <span className="admin-sidebar-user">{adminUser}</span>
          <button onClick={handleLogout} className="admin-logout-btn">Logout</button>
          <a href="/dashboard" className="admin-nav-link" style={{ marginTop: '0.5rem', fontSize: '0.8rem', opacity: 0.7 }}>
            Back to App
          </a>
        </div>
      </aside>
      <main className="admin-main">
        <Outlet />
      </main>
    </div>
  )
}

export default AdminLayout
