import { useAutoError } from '../hooks/useAutoMessage'
/**
 * BucketDetail — Permission-aware file browser.
 *
 * - RO users: can download only (no upload/delete buttons)
 * - RW users: can upload, delete own files
 * - Owner: can upload, delete any file, manage shares
 *
 * Shows who has access to the bucket (all users, not just owner).
 * Supports inline file viewing for images, text, PDF via FileViewer modal.
 */

import { useState, useEffect, useLayoutEffect, useRef } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { authAPI, bucketAPI, getApiError } from '../services/api'
import Navbar from './Navbar'
import ShareModal from './ShareModal'
import FileViewer from './FileViewer'
import { formatDateTime, formatSize } from '../utils/format'
import type { User, BucketDetail as BucketDetailType, BucketAccessList } from '../types'


type FileActionsMenuProps = {
  fileKey: string
  bucketId: string
  isNexus: boolean
  isViewable: boolean
  canUpload: boolean
  isOpen: boolean
  onToggle: (e: React.MouseEvent) => void
  onClose: () => void
  onDownload: () => void
  onView: () => void
  onDelete: () => void
}

function FileActionsMenu({
  fileKey,
  bucketId,
  isNexus,
  isViewable,
  canUpload,
  isOpen,
  onToggle,
  onClose,
  onDownload,
  onView,
  onDelete,
}: FileActionsMenuProps) {
  const triggerRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const [placement, setPlacement] = useState<'above' | 'below'>('below')

  useLayoutEffect(() => {
    if (!isOpen || !triggerRef.current || !dropdownRef.current) return
    const trigger = triggerRef.current.getBoundingClientRect()
    const menuHeight = dropdownRef.current.offsetHeight
    const spaceBelow = window.innerHeight - trigger.bottom
    const spaceAbove = trigger.top
    setPlacement(spaceBelow < menuHeight && spaceAbove > spaceBelow ? 'above' : 'below')
  }, [isOpen, isNexus, isViewable, canUpload])

  return (
    <div className="file-actions-menu">
      <button
        ref={triggerRef}
        type="button"
        className="file-actions-trigger"
        aria-expanded={isOpen}
        onClick={onToggle}
      >
        Show
      </button>
      {isOpen && (
        <div
          ref={dropdownRef}
          className="file-actions-dropdown"
          data-placement={placement}
          onClick={(e) => e.stopPropagation()}
        >
          <button type="button" className="file-actions-item" onClick={onDownload}>
            Download
          </button>
          {isNexus && (
            <Link
              to={`/buckets/${bucketId}/nexus?file=${bucketAPI.encodeFileKey(fileKey)}`}
              className="file-actions-item"
              onClick={onClose}
            >
              View
            </Link>
          )}
          {isViewable && (
            <button type="button" className="file-actions-item" onClick={onView}>
              View
            </button>
          )}
          {canUpload && (
            <button
              type="button"
              className="file-actions-item file-actions-item-danger"
              onClick={onDelete}
            >
              Delete
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function BucketDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [user, setUser] = useState<User | null>(null)
  const [bucket, setBucket] = useState<BucketDetailType | null>(null)
  const [accessList, setAccessList] = useState<BucketAccessList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useAutoError()
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<{ current: number; total: number } | null>(null)
  const [showShareModal, setShowShareModal] = useState(false)
  const [showAccess, setShowAccess] = useState(false)
  const [viewingFile, setViewingFile] = useState<{ key: string; size: number } | null>(null)
  const [showNamingRules, setShowNamingRules] = useState(false)
  const [downloadingArchive, setDownloadingArchive] = useState(false)
  const [openActionsKey, setOpenActionsKey] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const ac = new AbortController()
    loadData(ac.signal)
    return () => ac.abort()
  }, [id])

  useEffect(() => {
    if (!openActionsKey) return
    const closeMenu = () => setOpenActionsKey(null)
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeMenu()
    }
    document.addEventListener('click', closeMenu)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('click', closeMenu)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [openActionsKey])

  // signal is only set by the route-change effect; post-upload/delete reloads
  // run uncancelled because they're user-driven, not navigation-driven.
  const loadData = async (signal?: AbortSignal) => {
    if (!id) return
    const aborted = () => signal?.aborted === true
    try {
      setLoading(true)
      const bucketId = parseInt(id)
      const [userData, bucketData] = await Promise.all([
        authAPI.getCurrentUser({ signal }),
        bucketAPI.get(bucketId, { signal }),
      ])
      if (aborted()) return
      setUser(userData)
      setBucket(bucketData)

      // Access lists are supplementary; bucket contents should still load if this fails.
      try {
        const access = await bucketAPI.getAccessList(bucketId, { signal })
        if (aborted()) return
        setAccessList(access)
      } catch {
      }
    } catch (err) {
      // Aborted requests surface as CanceledError; treat as no-op, not a failure.
      if (aborted() || (err as Error)?.name === 'CanceledError') return
      console.error('Failed to load bucket:', err)
      setError('Failed to load bucket details.')
    } finally {
      if (!aborted()) setLoading(false)
    }
  }

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || files.length === 0 || !id) return

    const fileArray = Array.from(files)
    const bucketIdNum = parseInt(id)

    try {
      setUploading(true)
      setError(null)

      if (fileArray.length === 1) {
        await bucketAPI.uploadFile(bucketIdNum, fileArray[0])
      } else {
        setUploadProgress({ current: 0, total: fileArray.length })
        const errors: string[] = []

        for (let i = 0; i < fileArray.length; i++) {
          setUploadProgress({ current: i + 1, total: fileArray.length })
          try {
            await bucketAPI.uploadFile(bucketIdNum, fileArray[i])
          } catch (err: unknown) {
            errors.push(`${fileArray[i].name}: ${getApiError(err, 'Upload failed')}`)
          }
        }

        if (errors.length > 0) {
          setError(`${errors.length} file(s) failed:\n${errors.join('\n')}`)
        }
      }

      await loadData()
    } catch (err: unknown) {
      setError(getApiError(err, 'Upload failed'))
    } finally {
      setUploading(false)
      setUploadProgress(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleDeleteFile = async (fileKey: string) => {
    if (!id || !confirm(`Delete "${fileKey}"?`)) return
    try {
      await bucketAPI.deleteFile(parseInt(id), fileKey)
      await loadData()
    } catch (err: unknown) {
      setError(getApiError(err, 'Failed to delete file'))
    }
  }

  const handleDownload = async (fileKey: string) => {
    if (!id) return
    try {
      await bucketAPI.downloadFile(parseInt(id), fileKey)
    } catch (err) {
      console.error('Download failed:', err)
      setError('Failed to download file.')
    }
  }

  const handleDownloadArchive = async () => {
    if (!id || !bucket) return
    try {
      setDownloadingArchive(true)
      await bucketAPI.downloadArchive(parseInt(id), bucket.display_name || bucket.name)
    } catch (err) {
      console.error('Archive download failed:', err)
      setError(getApiError(err, 'Failed to download archive.'))
    } finally {
      setDownloadingArchive(false)
    }
  }

  const handleLeaveBucket = async () => {
    if (!id || !bucket) return
    if (!confirm(`Leave bucket "${bucket.display_name || bucket.name}"? You will lose access.`)) return
    try {
      await bucketAPI.leaveBucket(parseInt(id))
      navigate('/dashboard')
    } catch (err: unknown) {
      setError(getApiError(err, 'Failed to leave bucket'))
    }
  }

  // Check user's UO code for NFFADI local bucket upload gating
  const activeTenant = authAPI.getActiveTenant()
  const freshTenant = user?.tenants?.find(t => t.id === activeTenant?.id)
  const myUoCode = freshTenant?.uo_code || ''
  const nffadiLocalNeedsUo = activeTenant?.code === 'NFFADI'
    && bucket?.bucket_type === 'local'
    && !myUoCode

  const canUpload = !nffadiLocalNeedsUo && (bucket?.permission === 'rw' || bucket?.permission === 'owner')
  const isOwner = bucket?.permission === 'owner'
  const canLeave = bucket?.permission !== 'owner' && bucket?.bucket_type === 'local'

  // Personal file summary (computed from existing file list, no extra API call)
  const myDisplayName = user?.display_name
  const myFiles = bucket?.files?.filter(f => f.uploaded_by === myDisplayName) ?? []
  const myFileCount = myFiles.length
  const myTotalSize = myFiles.reduce((sum, f) => sum + (f.size || 0), 0)
  const totalSize = bucket?.files?.reduce((sum, f) => sum + (f.size || 0), 0) ?? 0

  if (loading) {
    return (
      <div className="page-container">
        <Navbar user={null} />
        <div className="loading-container"><h2>Loading...</h2></div>
      </div>
    )
  }

  if (!bucket) {
    return (
      <div className="page-container">
        <Navbar user={user} />
        <div className="error-container">
          <h2>Bucket not found</h2>
          <Link to="/dashboard" className="button-primary mt-2">Back to Dashboard</Link>
        </div>
      </div>
    )
  }

  const permLabel = (p: string) => p === 'owner' ? 'Owner' : p === 'rw' ? 'Read-Write' : 'Read-Only'
  const permColor = (p: string) => p === 'owner' ? '#2563eb' : p === 'rw' ? '#059669' : '#6b7280'

  const tenantCode = (activeTenant?.code || '').toUpperCase()
  const isNffadi = tenantCode === 'NFFADI'
  const isProposal = bucket.bucket_type === 'proposal'
  const bucketDisplay = bucket.display_name || bucket.name
  const tenantSlug = tenantCode.toLowerCase()
  const namingRuleText = isNffadi && isProposal
    ? `${tenantSlug}-${bucketDisplay}-{uo-code}-{your-filename}`
    : isNffadi && !isProposal
      ? `${tenantSlug}-{uo-code}-${bucketDisplay}-{your-filename}`
      : `${tenantSlug}-${bucketDisplay}-{your-filename}`

  return (
    <div className="page-container">
      <Navbar user={user} />

      <div className="bucket-detail-container">
        <Link to="/dashboard" className="breadcrumb">&larr; Back to Dashboard</Link>

        <div className="bucket-detail-header">
          <div>
            <h1>{bucket.display_name || bucket.name}</h1>
            {bucket.description && <p style={{ color: '#666' }}>{bucket.description}</p>}
            <div style={{ marginTop: '0.5rem', display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
              <span className={`permission-badge permission-${bucket.permission}`}>
                {bucket.permission === 'owner' ? 'Owner' : bucket.permission === 'rw' ? 'RW' : 'RO'}
              </span>
              <span style={{ fontSize: '0.85rem', color: '#888' }}>
                {bucket.bucket_type === 'proposal' ? 'Project Bucket' : 'Local Bucket'}
              </span>
            </div>
          </div>
          <div className="toolbar-cluster">
            {canLeave && (
              <button className="button-delete-small btn-compact" onClick={handleLeaveBucket}>
                Leave Bucket
              </button>
            )}
            {isOwner && bucket.bucket_type === 'local' && (
              <button className="button-secondary btn-compact" onClick={() => setShowShareModal(true)}>
                Share
              </button>
            )}
            {bucket.files.length > 0 && (
              <button
                className="button-secondary btn-compact"
                onClick={handleDownloadArchive}
                disabled={downloadingArchive}
              >
                {downloadingArchive ? 'Preparing...' : 'Download all'}
              </button>
            )}
            {canUpload && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  onChange={handleUpload}
                  style={{ display: 'none' }}
                  id="file-upload"
                />
                <label htmlFor="file-upload" className="button-primary btn-compact" style={{ cursor: uploading ? 'default' : 'pointer' }}>
                  {uploading
                    ? uploadProgress
                      ? `Uploading ${uploadProgress.current}/${uploadProgress.total}...`
                      : 'Uploading...'
                    : 'Upload Files'}
                </label>
              </>
            )}
          </div>
        </div>

        {nffadiLocalNeedsUo && (bucket.permission === 'rw' || bucket.permission === 'owner') && (
          <div style={{
            background: '#fef3c7', border: '1px solid #fcd34d',
            borderRadius: '6px', padding: '0.5rem 0.75rem',
            fontSize: '0.8rem', color: '#92400e', marginBottom: '1rem',
          }}>
            Your account has not been fully registered yet. Contact your administrator to have your UO code assigned.
          </div>
        )}

        {canUpload && (
          <div className="naming-rules-panel" style={{ marginBottom: '1rem' }}>
            <button
              type="button"
              className="naming-rules-toggle"
              onClick={() => setShowNamingRules((prev) => !prev)}
            >
              <span>Filename policy</span>
              <span>{showNamingRules ? 'Hide' : 'Show'}</span>
            </button>
            {showNamingRules && (
              <div className="naming-rules-body">
                Uploaded files are renamed to{' '}
                <code style={{ fontFamily: 'monospace', background: '#e0f2fe', padding: '0 0.3em', borderRadius: '3px' }}>
                  {namingRuleText}
                </code>
              </div>
            )}
          </div>
        )}

        {accessList && accessList.access.length > 0 && (
          <div style={{
            marginBottom: '1rem',
            border: '1px solid #e2e8f0',
            borderRadius: '8px',
            overflow: 'hidden',
          }}>
            <button
              onClick={() => setShowAccess(!showAccess)}
              style={{
                width: '100%',
                padding: '0.6rem 1rem',
                background: '#f8fafc',
                border: 'none',
                cursor: 'pointer',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                fontSize: '0.9rem',
                fontWeight: 500,
                color: '#334155',
              }}
            >
              <span>
                {bucket.bucket_type === 'proposal'
                  ? `Owned by: ${accessList.owner_label}`
                  : `Owner: ${accessList.owner_label}`
                }
                {' '}&middot;{' '}
                {accessList.access.length} user{accessList.access.length !== 1 ? 's' : ''} with access
              </span>
              <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                {showAccess ? '▲ Hide' : '▼ Show'}
              </span>
            </button>
            {showAccess && (
              <div style={{ padding: '0.5rem 1rem' }}>
                {accessList.access.map((entry) => (
                  <div
                    key={entry.user_id}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '0.4rem 0',
                      borderBottom: '1px solid #f1f5f9',
                    }}
                  >
                    <div>
                      <span style={{ fontWeight: entry.permission === 'owner' ? 600 : 400 }}>
                        {entry.display_name}
                      </span>
                      {entry.email && (
                        <span style={{ color: '#94a3b8', fontSize: '0.75rem', marginLeft: '0.4rem' }}>
                          {entry.email}
                        </span>
                      )}
                    </div>
                    <span style={{
                      fontSize: '0.75rem',
                      fontWeight: 500,
                      color: permColor(entry.permission),
                      padding: '0.15rem 0.5rem',
                      borderRadius: '4px',
                      background: entry.permission === 'owner' ? '#eff6ff' : entry.permission === 'rw' ? '#ecfdf5' : '#f9fafb',
                    }}>
                      {permLabel(entry.permission)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <div className="error-message" onClick={() => setError(null)} style={{ cursor: 'pointer' }}>
            {error} (click to dismiss)
          </div>
        )}

        {bucket.files.length === 0 ? (
          <div className="empty-state">
            <h2>No files yet</h2>
            {canUpload ? (
              <p className="mt-2">Upload a file to get started.</p>
            ) : (
              <p className="mt-2">This bucket is empty.</p>
            )}
          </div>
        ) : (
          <>
          {myFileCount > 0 && (
            <div style={{
              padding: '0.5rem 1rem', background: '#f0fdf4',
              border: '1px solid #bbf7d0', borderRadius: '6px',
              fontSize: '0.8rem', color: '#166534', marginBottom: '0.5rem',
              display: 'flex', gap: '1.5rem',
            }}>
              <span>Your files: <strong>{myFileCount}</strong> · <strong>{formatSize(myTotalSize)}</strong></span>
              <span style={{ color: '#9ca3af' }}>Total: <strong>{bucket.files.length}</strong> files · <strong>{formatSize(totalSize)}</strong></span>
            </div>
          )}
          <div className="files-table-container files-table-container--detail">
            <div className="files-table-scroll--detail">
            <table className="files-table files-table--detail">
              <thead>
                <tr>
                  <th>Name</th>
                  <th className="files-col-size">Size</th>
                  <th className="files-col-uploader">Uploaded By</th>
                  <th className="files-col-modified">Last Modified</th>
                  <th className="files-col-actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {bucket.files.map((file) => {
                  const isNexus = bucketAPI.isLikelyNexusFile(file.key)
                  const isViewable = !isNexus && bucketAPI.isViewableFile(file.key)
                  const menuOpen = openActionsKey === file.key
                  return (
                  <tr key={file.key}>
                    <td className="file-name" title={file.key}>{file.key}</td>
                    <td className="files-col-size">{formatSize(file.size)}</td>
                    <td className="files-col-uploader" style={{ color: '#888', fontSize: '0.85rem' }}>
                      {file.uploaded_by || '—'}
                    </td>
                    <td className="files-col-modified">{formatDateTime(file.last_modified)}</td>
                    <td className="files-col-actions">
                      <FileActionsMenu
                        fileKey={file.key}
                        bucketId={id!}
                        isNexus={isNexus}
                        isViewable={Boolean(isViewable)}
                        canUpload={canUpload}
                        isOpen={menuOpen}
                        onToggle={(e) => {
                          e.stopPropagation()
                          setOpenActionsKey(menuOpen ? null : file.key)
                        }}
                        onClose={() => setOpenActionsKey(null)}
                        onDownload={() => {
                          setOpenActionsKey(null)
                          handleDownload(file.key)
                        }}
                        onView={() => {
                          setOpenActionsKey(null)
                          setViewingFile({ key: file.key, size: file.size })
                        }}
                        onDelete={() => {
                          setOpenActionsKey(null)
                          handleDeleteFile(file.key)
                        }}
                      />
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
            </div>
            <p className="file-count" style={{ padding: '0.5rem 1rem' }}>
              {bucket.files.length} file{bucket.files.length !== 1 ? 's' : ''}
            </p>
          </div>
          </>
        )}
      </div>

      {showShareModal && bucket && (
        <ShareModal
          bucketId={bucket.id}
          bucketName={bucket.display_name || bucket.name}
          onClose={() => setShowShareModal(false)}
        />
      )}

      {viewingFile && bucket && (
        <FileViewer
          bucketId={bucket.id}
          fileKey={viewingFile.key}
          fileSize={viewingFile.size}
          onClose={() => setViewingFile(null)}
        />
      )}
    </div>
  )
}

export default BucketDetail
