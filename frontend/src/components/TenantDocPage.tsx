import { useState, useEffect } from 'react'
import { authAPI } from '../services/api'
import Navbar from './Navbar'
import MarkdownViewer from './MarkdownViewer'
import type { User } from '../types'

function TenantDocPage() {
  const [user, setUser] = useState<User | null>(null)
  const [content, setContent] = useState<string | null>(null)
  const [tabName, setTabName] = useState('')
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)

  const activeTenant = authAPI.getActiveTenant()

  useEffect(() => {
    const ac = new AbortController()
    Promise.all([
      authAPI.getCurrentUser({ signal: ac.signal }),
      authAPI.getTenantDocument(),
    ])
      .then(([userData, docData]) => {
        if (ac.signal.aborted) return
        setUser(userData)
        setContent(docData.content)
        setTabName(docData.tab_name)
      })
      .catch((err) => {
        if (ac.signal.aborted || (err as {name?: string}).name === 'CanceledError') return
        const status = (err as {response?: {status?: number}}).response?.status
        if (status === 404) {
          setNotFound(true)
        } else {
          setNotFound(true)
        }
      })
      .finally(() => { if (!ac.signal.aborted) setLoading(false) })
    return () => ac.abort()
  }, [])

  if (loading) {
    return (
      <div className="page-container">
        <Navbar user={null} />
        <div className="loading-container"><h2>Loading…</h2></div>
      </div>
    )
  }

  return (
    <div className="page-container">
      <Navbar user={user} />
      <div className="dashboard-container" style={{ maxWidth: '1024px' }}>
        {notFound || !content ? (
          <div style={{ color: '#64748b', padding: '2rem 0' }}>
            No documentation is available for this tenant.
          </div>
        ) : (
          <>
            <h1 style={{ marginBottom: '1.5rem', color: '#0f172a' }}>
              {tabName || activeTenant?.document?.tab_name || 'Documentation'}
            </h1>
            <MarkdownViewer content={content} />
          </>
        )}
      </div>
    </div>
  )
}

export default TenantDocPage
