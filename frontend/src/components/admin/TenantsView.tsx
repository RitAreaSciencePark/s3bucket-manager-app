import { useAutoError } from '../../hooks/useAutoMessage'
import { useState, useEffect, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { adminAPI, getApiError } from '../../services/api'
import { formatSize } from '../../utils/format'
import type { AdminTenant, AdminTenantActivation, CreateTenantPayload } from '../../types'

interface SyncStats {
  users_synced?: number
  users_skipped_unregistered?: number
  buckets_synced?: number
  permissions_synced?: number
  users_deactivated?: number
  objects_synced?: number
  objects_discovered?: number
  object_records_removed?: number
  object_sync_errors?: number
  initialized?: boolean
}

interface ActivateResult {
  sync_stats: SyncStats | null
  sync_error: string | null
}

interface MappingDraft {
  group: string
  role: 'rw' | 'ro'
}

function suggestGroupName(structure: string, role: 'rw' | 'ro') {
  const slug = structure.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
  return role === 'rw' ? `${slug}-users` : `${slug}-ext`
}

function defaultDraft(row: AdminTenantActivation): MappingDraft {
  if (row.required_group_name) {
    return { role: 'rw', group: row.required_group_name }
  }
  return {
    role: 'rw',
    group: row.suggested_rw_group || suggestGroupName(row.structure, 'rw'),
  }
}

function statusBadge(ok: boolean, label: string) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '0.2rem 0.55rem',
        borderRadius: '4px',
        fontSize: '0.75rem',
        fontWeight: 600,
        background: ok ? '#d1fae5' : '#fef3c7',
        color: ok ? '#065f46' : '#92400e',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  )
}

function activationStep(label: string, ok: boolean, detail?: string) {
  return (
    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline' }}>
      <span style={{ color: ok ? '#059669' : '#d97706', fontWeight: 700 }}>{ok ? '✓' : '•'}</span>
      <span style={{ fontWeight: 500 }}>{label}</span>
      {detail && <span style={{ color: '#64748b', fontSize: '0.8rem' }}>{detail}</span>}
    </div>
  )
}

function TenantsView() {
  const [rows, setRows] = useState<AdminTenantActivation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useAutoError()

  const [activating, setActivating] = useState<string | null>(null)
  const [activateLoading, setActivateLoading] = useState(false)
  const [activateError, setActivateError] = useState<string | null>(null)
  const [activateResult, setActivateResult] = useState<ActivateResult | null>(null)
  const [activationAccessModel, setActivationAccessModel] = useState<'' | 'rgwsquared_synced' | 'authentik_managed'>('')

  const [refreshingCode, setRefreshingCode] = useState<string | null>(null)
  const [refreshResults, setRefreshResults] = useState<Record<string, { ok: boolean; msg: string }>>({})
  const [mappingDrafts, setMappingDrafts] = useState<Record<string, MappingDraft>>({})
  const [mappingLoading, setMappingLoading] = useState<string | null>(null)
  const [deletingMappingId, setDeletingMappingId] = useState<number | null>(null)

  async function load() {
    setLoading(true)
    try {
      setRows(await adminAPI.getTenantActivation())
    } catch (err: unknown) {
      setError(getApiError(err, 'Failed to load tenant activation status'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  function openActivate(structure: string) {
    setActivating(structure)
    setActivateError(null)
    setActivateResult(null)
    setActivationAccessModel(structure.toUpperCase() === 'NFFADI' ? 'rgwsquared_synced' : '')
  }

  async function handleActivate() {
    if (!activating || !activationAccessModel) return
    setActivateLoading(true)
    setActivateError(null)
    setActivateResult(null)
    try {
      const payload: CreateTenantPayload = {
        structure: activating,
        name: activating,
        bucket_name_prefix: activating.toLowerCase(),
        access_model: activationAccessModel,
      }
      const res = await adminAPI.createTenant(payload) as AdminTenant & { sync_stats?: SyncStats; sync_error?: string }
      setActivateResult({
        sync_stats: res.sync_stats ?? null,
        sync_error: res.sync_error ?? null,
      })
      await load()
    } catch (err: unknown) {
      setActivateError(getApiError(err, 'Failed to activate tenant'))
    } finally {
      setActivateLoading(false)
    }
  }

  async function handleRefreshTenant(code: string) {
    setRefreshingCode(code)
    setRefreshResults(prev => { const n = { ...prev }; delete n[code]; return n })
    try {
      const stats = await adminAPI.syncRefresh(code) as SyncStats
      setRefreshResults(prev => ({
        ...prev,
        [code]: {
          ok: (stats.object_sync_errors ?? 0) === 0,
          msg: `✓ ${stats.users_synced ?? 0} users, ${stats.buckets_synced ?? 0} buckets, ${stats.objects_synced ?? 0} objects synced${stats.users_skipped_unregistered ? ` · ${stats.users_skipped_unregistered} unregistered skipped` : ''}${stats.objects_discovered ? ` · ${stats.objects_discovered} discovered` : ''}${stats.object_sync_errors ? ` · ${stats.object_sync_errors} object errors` : ''}`,
        },
      }))
      await load()
    } catch (err: unknown) {
      setRefreshResults(prev => ({
        ...prev,
        [code]: { ok: false, msg: `✗ ${getApiError(err, 'Sync failed')}` },
      }))
    } finally {
      setRefreshingCode(null)
    }
  }

  function draftFor(row: AdminTenantActivation) {
    return mappingDrafts[row.structure] ?? defaultDraft(row)
  }

  function updateDraft(row: AdminTenantActivation, patch: Partial<MappingDraft>) {
    setMappingDrafts(prev => ({
      ...prev,
      [row.structure]: { ...(prev[row.structure] ?? defaultDraft(row)), ...patch },
    }))
  }

  async function handleAddMapping(event: FormEvent, row: AdminTenantActivation) {
    event.preventDefault()
    if (!row.tenant_id) return
    const draft = draftFor(row)
    const role = row.access_model === 'rgwsquared_synced' ? 'rw' : draft.role
    const group = row.required_group_name || draft.group.trim()
    if (!group.trim()) return
    setMappingLoading(row.structure)
    setError(null)
    try {
      await adminAPI.addGroupMapping(group.trim(), row.tenant_id, role)
      setMappingDrafts(prev => {
        const next = { ...prev }
        delete next[row.structure]
        return next
      })
      await load()
    } catch (err: unknown) {
      setError(getApiError(err, 'Failed to add group mapping'))
    } finally {
      setMappingLoading(null)
    }
  }

  async function handleDeleteMapping(id: number) {
    setDeletingMappingId(id)
    try {
      await adminAPI.deleteGroupMapping(id)
      await load()
    } catch (err: unknown) {
      setError(getApiError(err, 'Delete failed'))
    } finally {
      setDeletingMappingId(null)
    }
  }

  if (loading) return <div className="admin-loading">Loading tenant activation...</div>

  return (
    <div>
      <div className="admin-page-header">
        <h1>Tenants</h1>
        <button className="btn btn-secondary btn-sm" onClick={load} disabled={loading}>
          Reload Status
        </button>
      </div>
      {error && <div className="error-message" onClick={() => setError(null)}>{error}</div>}

      {activating && (
        <div className="modal-overlay" onClick={() => !activateLoading && !activateResult && setActivating(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h3>Activate Tenant: {activating}</h3>

            {activateResult ? (
              <div>
                <div style={{
                  padding: '0.75rem 1rem', borderRadius: '6px', marginBottom: '1rem',
                  background: activateResult.sync_error ? '#fef2f2' : '#f0fdf4',
                  border: `1px solid ${activateResult.sync_error ? '#fecaca' : '#bbf7d0'}`,
                  color: activateResult.sync_error ? '#991b1b' : '#166534',
                }}>
                  <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>✓ Tenant activated</div>
                  {activateResult.sync_stats && !activateResult.sync_error && (
                    <div style={{ fontSize: '0.875rem' }}>
                      Synced from RGWSquared: {activateResult.sync_stats.users_synced ?? 0} users,{' '}
                      {activateResult.sync_stats.buckets_synced ?? 0} buckets,{' '}
                      {activateResult.sync_stats.permissions_synced ?? 0} permissions,{' '}
                      {activateResult.sync_stats.objects_synced ?? 0} objects
                    </div>
                  )}
                  {activateResult.sync_error && (
                    <div style={{ fontSize: '0.875rem' }}>
                      RGWSquared refresh failed: {activateResult.sync_error}
                      <br />
                      <span style={{ color: '#6b7280' }}>Use the tenant Refresh button after the upstream service is ready.</span>
                    </div>
                  )}
                  {!activateResult.sync_stats && !activateResult.sync_error && (
                    <div style={{ fontSize: '0.875rem', color: '#6b7280' }}>
                      No data returned from RGWSquared.
                    </div>
                  )}
                </div>
                <button className="btn btn-primary" onClick={() => setActivating(null)}>Done</button>
              </div>
            ) : (
              <div>
                <p style={{ color: '#6b7280', marginBottom: '1rem' }}>
                  Choose which system owns user enrollment and roles for <strong>{activating}</strong>.
                </p>
                <label style={{ display: 'grid', gap: '0.35rem', marginBottom: '1rem' }}>
                  <span style={{ fontWeight: 600, color: '#334155' }}>Access model</span>
                  <select
                    className="admin-select"
                    value={activationAccessModel}
                    onChange={(event) => setActivationAccessModel(event.target.value as typeof activationAccessModel)}
                    disabled={activating.toUpperCase() === 'NFFADI'}
                  >
                    <option value="">Select access model…</option>
                    <option value="rgwsquared_synced">RGWSquared-synced — upstream users and roles</option>
                    <option value="authentik_managed">Authentik-managed — users and roles at login</option>
                  </select>
                </label>
                {activateError && <div className="error-message" style={{ marginBottom: '0.75rem' }}>{activateError}</div>}
                <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
                  <button className="btn btn-primary" onClick={handleActivate} disabled={activateLoading || !activationAccessModel}>
                    {activateLoading ? 'Activating + refreshing…' : 'Activate tenant'}
                  </button>
                  <button className="btn btn-secondary" onClick={() => setActivating(null)} disabled={activateLoading}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gap: '1rem' }}>
        {rows.map((row) => {
          const draft = draftFor(row)
          const initialized = row.initialized === true
          const groupReady = row.group_mapping_ready ?? row.has_group_mapping
          const nffadiPolicy = Boolean(row.required_group_name)
          const tenantRefreshCode = row.tenant_code
          return (
            <section
              key={row.structure}
              style={{
                background: 'white',
                borderRadius: '8px',
                boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
                padding: '1rem',
                borderLeft: `4px solid ${row.fully_active ? '#10b981' : '#f59e0b'}`,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', alignItems: 'flex-start', flexWrap: 'wrap' }}>
                <div>
                  <h2 style={{ fontSize: '1.1rem', color: '#2c3e50', marginBottom: '0.25rem' }}>{row.structure}</h2>
                  <div style={{ color: '#64748b', fontSize: '0.85rem' }}>
                    {row.has_tenant ? `${row.member_count} members · ${row.bucket_count} buckets · ${formatSize(row.storage_bytes)} · ${row.access_model === 'rgwsquared_synced' ? 'RGWSquared-synced' : 'Authentik-managed'}` : 'No local Django tenant yet'}
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                  {statusBadge(row.fully_active, row.fully_active ? 'Fully active' : 'Incomplete')}
                  {row.has_tenant && (
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => handleRefreshTenant(tenantRefreshCode)}
                      disabled={refreshingCode === tenantRefreshCode}
                      title="Refresh Django's local users, buckets, and permissions from RGWSquared"
                    >
                      {refreshingCode === tenantRefreshCode ? 'Refreshing…' : 'Refresh'}
                    </button>
                  )}
                </div>
              </div>

              {refreshResults[tenantRefreshCode] && (
                <div style={{
                  marginTop: '0.75rem',
                  fontSize: '0.82rem',
                  color: refreshResults[tenantRefreshCode].ok ? '#166534' : '#991b1b',
                }}>
                  {refreshResults[tenantRefreshCode].msg}
                </div>
              )}

              <div style={{ display: 'grid', gap: '0.45rem', marginTop: '1rem' }}>
                {activationStep('RGWSquared structure initialized', initialized, row.initialized === null ? 'status unavailable' : `${row.buckets_auto} auto / ${row.buckets_manual} manual buckets`)}
                {activationStep('Local Django tenant record exists', row.has_tenant)}
                {activationStep(
                  row.access_model === 'rgwsquared_synced' ? 'Authentik eligibility mapping ready' : 'Authentik role mapping exists',
                  groupReady,
                  groupReady
                    ? `${row.group_mapping_count} mapping${row.group_mapping_count === 1 ? '' : 's'} · roles from ${row.role_source === 'rgwsquared' ? 'RGWSquared' : 'group mapping'}`
                    : row.group_mapping_issue || undefined
                )}
                {activationStep(
                  'UO coverage ready',
                  !row.requires_uo_sync || row.uo_ready,
                  row.requires_uo_sync
                    ? row.uo_ready
                      ? `${row.write_capable_member_count} write-capable memberships covered`
                      : `${row.missing_uo_count} write-capable memberships missing UO code`
                    : 'not required for this tenant'
                )}
              </div>

              {row.group_mappings.length > 0 && (
                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap', marginTop: '0.9rem' }}>
                  {row.group_mappings.map(mapping => (
                    <span key={mapping.id} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35rem', background: '#f1f5f9', borderRadius: '4px', padding: '0.25rem 0.5rem', fontSize: '0.8rem' }}>
                      <span className={`permission-badge permission-${mapping.role}`}>{mapping.role.toUpperCase()}</span>
                      <span>{mapping.authentik_group}</span>
                      <button
                        type="button"
                        className="button-delete-small"
                        onClick={() => handleDeleteMapping(mapping.id)}
                        disabled={deletingMappingId === mapping.id}
                        title="Remove group mapping"
                      >
                        Remove
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {row.has_tenant && !(row.access_model === 'rgwsquared_synced' && row.group_mapping_count > 0) && (
                <form onSubmit={(event) => handleAddMapping(event, row)} className="admin-add-form" style={{ marginTop: '0.9rem', marginBottom: 0, flexWrap: 'wrap' }}>
                  {row.access_model === 'rgwsquared_synced' ? (
                    <span style={{ display: 'inline-flex', alignItems: 'center', minHeight: '38px', color: '#475569', fontSize: '0.85rem' }}>
                      One eligibility group gates login; RO/RW comes from RGWSquared.
                    </span>
                  ) : (
                    <select
                      value={draft.role}
                      onChange={(event) => {
                        const role = event.target.value as 'rw' | 'ro'
                        updateDraft(row, {
                          role,
                          group: role === 'rw'
                            ? (row.suggested_rw_group || suggestGroupName(row.structure, role))
                            : (row.suggested_ro_group || suggestGroupName(row.structure, role)),
                        })
                      }}
                      className="admin-select"
                      aria-label={`Access role for ${row.structure} group mapping`}
                    >
                      <option value="rw">Read-write</option>
                      <option value="ro">Read-only</option>
                    </select>
                  )}
                  <input
                    type="text"
                    value={row.required_group_name || draft.group}
                    onChange={(event) => updateDraft(row, { group: event.target.value })}
                    placeholder="Authentik group name"
                    className="admin-filter-input"
                    style={{ minWidth: '260px' }}
                    disabled={nffadiPolicy}
                  />
                  <button
                    type="submit"
                    className="button-primary"
                    disabled={mappingLoading === row.structure || !draft.group.trim()}
                  >
                    {mappingLoading === row.structure ? 'Adding…' : 'Add mapping'}
                  </button>
                </form>
              )}

              <div style={{ marginTop: '0.9rem', color: '#475569', fontSize: '0.875rem' }}>
                {!row.available_in_rgwsquared ? (
                  <span>Next: ask the RGWSquared operator why this local tenant is not listed as a structure.</span>
                ) : !initialized ? (
                  <span>Next: verify the RGWSquared structure is initialized before activating this tenant.</span>
                ) : !row.has_tenant ? (
                  <button className="btn btn-primary btn-sm" onClick={() => openActivate(row.structure)}>
                    Activate tenant
                  </button>
                ) : !groupReady ? (
                  <span>{row.group_mapping_issue || 'Next: add the Authentik group that should grant this tenant access.'}</span>
                ) : row.requires_uo_sync && !row.uo_ready ? (
                  <span>
                    Next: update UO source data in <Link to="/admin/sync">Sync</Link>, then use this tenant's Refresh button to pull RGWSquared data into Django.
                  </span>
                ) : (
                  <span>Ready for users.</span>
                )}
              </div>
            </section>
          )
        })}
        {rows.length === 0 && <div className="admin-empty">No RGWSquared structures found</div>}
      </div>
    </div>
  )
}

export default TenantsView
