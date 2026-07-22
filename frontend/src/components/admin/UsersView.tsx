import { useAutoError } from '../../hooks/useAutoMessage'
import React, { useState, useEffect } from 'react'
import { adminAPI, getApiError } from '../../services/api'
import { formatDate, formatSize } from '../../utils/format'
import type { AdminUser, AdminUserFile, AdminAvailableTenant } from '../../types'

function UsersView() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [tenants, setTenants] = useState<AdminAvailableTenant[]>([])
  const [selectedTenant, setSelectedTenant] = useState('')
  const [loadingTenants, setLoadingTenants] = useState(true)
  const [loadingUsers, setLoadingUsers] = useState(false)
  const [error, setError] = useAutoError()
  const [filter, setFilter] = useState('')
  const [roleFilter, setRoleFilter] = useState<'all' | 'rw' | 'ro'>('all')

  const [expandedMembershipId, setExpandedMembershipId] = useState<number | null>(null)
  const [membershipFiles, setMembershipFiles] = useState<Record<number, AdminUserFile[] | 'loading'>>({})
  const [sortByStorage, setSortByStorage] = useState<'asc' | 'desc' | null>(null)

  const clearExpandedRows = () => {
    setExpandedMembershipId(null)
    setMembershipFiles({})
  }

  const loadUsers = async (tenantCode = selectedTenant) => {
    if (!tenantCode) {
      setUsers([])
      clearExpandedRows()
      return
    }
    try {
      setLoadingUsers(true)
      setError(null)
      clearExpandedRows()
      setUsers(await adminAPI.getUsers(tenantCode))
    } catch (err: unknown) {
      setError(getApiError(err, 'Failed to load users'))
    } finally {
      setLoadingUsers(false)
    }
  }

  useEffect(() => {
    adminAPI.getAvailableTenants()
      .then(data => {
        const active = data.filter(t => t.has_tenant)
        setTenants(active)
        if (active.length > 0) setSelectedTenant(active[0].structure)
      })
      .catch(err => setError(getApiError(err, 'Failed to load tenants')))
      .finally(() => setLoadingTenants(false))
  }, [])

  useEffect(() => {
    loadUsers(selectedTenant)
  }, [selectedTenant])

  const handleTenantChange = (tenantCode: string) => {
    setSelectedTenant(tenantCode)
    clearExpandedRows()
  }

  const toggleMembership = async (membershipId: number) => {
    if (expandedMembershipId === membershipId) {
      setExpandedMembershipId(null)
      return
    }
    setExpandedMembershipId(membershipId)
    setMembershipFiles(prev => ({ ...prev, [membershipId]: 'loading' }))
    try {
      const files = await adminAPI.getMembershipFiles(membershipId)
      setMembershipFiles(prev => ({ ...prev, [membershipId]: files }))
    } catch {
      setMembershipFiles(prev => ({ ...prev, [membershipId]: [] }))
    }
  }

  const isWriteCapable = (role: string) => role === 'rw' || role === 'admin'

  const filtered = users
    .filter((u) => {
      if (roleFilter === 'rw' && !isWriteCapable(u.role)) return false
      if (roleFilter === 'ro' && u.role !== 'ro') return false
      if (!filter) return true
      const q = filter.toLowerCase()
      return (
        u.ceph_username.toLowerCase().includes(q) ||
        u.display_name.toLowerCase().includes(q) ||
        u.email.toLowerCase().includes(q) ||
        u.tenant_code.toLowerCase().includes(q) ||
        u.tenant_name.toLowerCase().includes(q)
      )
    })
    .sort((a, b) => {
      if (!sortByStorage) return 0
      return sortByStorage === 'desc'
        ? b.total_file_size - a.total_file_size
        : a.total_file_size - b.total_file_size
    })

  if (loadingTenants) return <div className="admin-loading">Loading users...</div>

  const chipStyle = { padding: '0.25rem 0.75rem', fontSize: '0.8rem' }

  return (
    <div>
      <div className="admin-page-header">
        <h1>Users</h1>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
          <select
            value={selectedTenant}
            onChange={e => handleTenantChange(e.target.value)}
            className="admin-select"
          >
            <option value="">Select tenant...</option>
            {tenants.map(t => <option key={t.structure} value={t.structure}>{t.structure}</option>)}
          </select>
          <input
            type="text"
            placeholder="Filter..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="admin-filter-input"
          />
          <button className="btn btn-secondary btn-sm" onClick={() => loadUsers()} disabled={loadingUsers || !selectedTenant}>
            Refresh
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '0.25rem', flexWrap: 'wrap', alignItems: 'center', marginBottom: '0.75rem' }}>
        <span style={{ fontSize: '0.75rem', color: '#888', marginRight: '0.25rem' }}>Role:</span>
        <button onClick={() => setRoleFilter('all')} className={roleFilter === 'all' ? 'button-primary' : 'button-secondary'} style={chipStyle}>All</button>
        <button onClick={() => setRoleFilter('rw')} className={roleFilter === 'rw' ? 'button-primary' : 'button-secondary'} style={chipStyle}>Read-write</button>
        <button onClick={() => setRoleFilter('ro')} className={roleFilter === 'ro' ? 'button-primary' : 'button-secondary'} style={chipStyle}>Read-only</button>
      </div>

      {error && <div className="error-message" onClick={() => setError(null)}>{error}</div>}
      {!selectedTenant ? (
        <div className="admin-empty">No active tenants found</div>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th style={{ width: '28px' }}></th>
                <th>User</th>
                <th>Email</th>
                <th>Tenant</th>
                <th>Role</th>
                <th>Access</th>
                <th>UO Code</th>
                <th
                  style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}
                  onClick={() => setSortByStorage(s => s === 'asc' ? 'desc' : s === 'desc' ? null : 'desc')}
                  title="Click to sort by storage"
                >
                  Storage {sortByStorage === 'desc' ? '▼' : sortByStorage === 'asc' ? '▲' : '⇅'}
                </th>
                <th>Last Login</th>
              </tr>
            </thead>
            <tbody>
              {loadingUsers ? (
                <tr><td colSpan={9} className="admin-empty">Loading users...</td></tr>
              ) : filtered.map((u) => {
                const isExpanded = expandedMembershipId === u.membership_id
                const files = membershipFiles[u.membership_id]
                return (
                  <React.Fragment key={u.membership_id}>
                    <tr style={{ cursor: 'pointer' }} onClick={() => toggleMembership(u.membership_id)}>
                      <td style={{ textAlign: 'center', color: '#94a3b8', userSelect: 'none' }}>
                        {isExpanded ? '▼' : '▶'}
                      </td>
                      <td>
                        <div className="admin-cell-primary">{u.display_name}</div>
                        {u.ceph_username !== u.display_name && (
                          <div className="admin-cell-secondary">{u.ceph_username}</div>
                        )}
                      </td>
                      <td>{u.email || '—'}</td>
                      <td>{u.tenant_code}</td>
                      <td><span className={`permission-badge permission-${u.role}`}>{u.role.toUpperCase()}</span></td>
                      <td>
                        <span
                          className={`permission-badge permission-${u.access_status === 'authorized' ? 'rw' : 'ro'}`}
                          style={u.access_status === 'not_configured' ? { background: '#e2e8f0', color: '#475569' } : undefined}
                          title={
                            u.access_status === 'revoked'
                              ? `${u.access_revocation_reason === 'mapping_removed' ? 'Group mapping removed' : 'Mapped group missing at login'}${u.access_revoked_group ? `: ${u.access_revoked_group}` : ''}${u.access_revoked_at ? ` · ${formatDate(u.access_revoked_at)}` : ''}`
                              : undefined
                          }
                        >
                          {u.access_status === 'authorized' ? 'AUTHORIZED' : u.access_status === 'revoked' ? 'REVOKED' : 'NOT CONFIGURED'}
                        </span>
                      </td>
                      <td>{u.uo_code || '—'}</td>
                      <td>
                        {u.total_file_size > 0 ? (
                          <span style={{ color: '#475569', fontSize: '0.8rem' }} title={`${u.file_count} files`}>
                            {formatSize(u.total_file_size)}
                          </span>
                        ) : (
                          <span style={{ color: '#cbd5e1', fontSize: '0.8rem' }}>—</span>
                        )}
                      </td>
                      <td>{u.last_login ? formatDate(u.last_login) : 'Never'}</td>
                    </tr>
                    {isExpanded && (
                      <tr>
                        <td colSpan={9} style={{ padding: 0, background: '#f8fafc' }}>
                          <div style={{ padding: '0.75rem 1.5rem 0.75rem 2.5rem' }}>
                            {files === 'loading' ? (
                              <div style={{ color: '#94a3b8', fontSize: '0.85rem' }}>Loading files...</div>
                            ) : !files || files.length === 0 ? (
                              <div style={{ color: '#94a3b8', fontSize: '0.85rem' }}>No files uploaded by this account.</div>
                            ) : (
                              <table style={{ width: '100%', fontSize: '0.82rem', borderCollapse: 'collapse' }}>
                                <thead>
                                  <tr style={{ background: '#e2e8f0' }}>
                                    <th style={{ padding: '0.3rem 0.6rem', textAlign: 'left', fontWeight: 600 }}>File</th>
                                    <th style={{ padding: '0.3rem 0.6rem', textAlign: 'left', fontWeight: 600 }}>Bucket</th>
                                    <th style={{ padding: '0.3rem 0.6rem', textAlign: 'right', fontWeight: 600 }}>Size</th>
                                    <th style={{ padding: '0.3rem 0.6rem', textAlign: 'left', fontWeight: 600 }}>Uploaded</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {files.map((f, i) => (
                                    <tr key={i} style={{ borderBottom: '1px solid #e2e8f0' }}>
                                      <td style={{ padding: '0.3rem 0.6rem', fontFamily: 'monospace', color: '#334155' }}>
                                        {f.file_key.split('/').pop() || f.file_key}
                                      </td>
                                      <td style={{ padding: '0.3rem 0.6rem', color: '#475569' }}>{f.bucket_name}</td>
                                      <td style={{ padding: '0.3rem 0.6rem', textAlign: 'right', color: '#64748b' }}>
                                        {f.file_size > 0 ? formatSize(f.file_size) : '—'}
                                      </td>
                                      <td style={{ padding: '0.3rem 0.6rem', color: '#64748b' }}>{formatDate(f.uploaded_at)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
              {!loadingUsers && filtered.length === 0 && (
                <tr><td colSpan={8} className="admin-empty">No users found</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      <div className="admin-table-footer">{filtered.length} of {users.length} users</div>
    </div>
  )
}

export default UsersView
