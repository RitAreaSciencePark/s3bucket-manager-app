// Federation fields mirror the backend user payload.
export interface User {
  id: number;
  username: string;
  display_name?: string;
  email: string;
  first_name: string;
  last_name: string;
  external_id: string;
  idp_source: string;
  institution: string;
  department: string;
  affiliation_status: string;
  orcid: string;
  profile_picture_url: string;
  is_approved: boolean;
  is_staff: boolean;
  date_joined: string;
  tenants?: TenantInfo[];
}

export interface TenantInfo {
  id: number;
  code: string;
  name: string;
  role: 'ro' | 'rw' | 'admin';
  uo_code?: string;
  uo_name?: string;
  document?: { tab_name: string; is_visible: boolean } | null;
}

export interface NexusDetectResponse {
  is_nexus: boolean;
  size: number;
  filename: string;
}

export interface AuthTokens {
  access: string;
  refresh: string;
}

export interface TokenExchangeResponse {
  user: User;
  tokens: AuthTokens;
  tenants: TenantInfo[];
  active_tenant: TenantInfo | null;
  tenant_selection_required: boolean;
}

export interface Bucket {
  id: number;
  name: string;
  display_name: string;
  description: string;
  bucket_type: 'proposal' | 'local';
  is_deletable: boolean;
  owner: number | null;
  owner_name: string | null;
  permission: 'ro' | 'rw' | 'owner' | null;
  shared_with_count: number;
  size_bytes: number;
  num_objects: number;
  created_at: string;
  updated_at: string;
}

export interface BucketFile {
  key: string;
  size: number;
  last_modified: string;
  uploaded_by?: string | null;
}

export interface BucketDetail extends Bucket {
  files: BucketFile[];
}

export interface BucketShare {
  id: number;
  user_id: number;
  username: string;
  display_name?: string;
  email: string;
  permission: 'ro' | 'rw';
  granted_at: string;
}

export interface BucketAccessEntry {
  user_id: number;
  display_name: string;
  email?: string;
  permission: 'ro' | 'rw' | 'owner';
  source: 'local' | 'rgwsquared';
}

export interface BucketAccessList {
  owner_label: string;
  is_owner: boolean;
  access: BucketAccessEntry[];
}

export interface AdminPermission {
  id: number;
  username: string;
  ceph_username: string;
  email: string;
  bucket_name: string;
  bucket_ceph_name: string;
  tenant_code: string | null;
  permission: string;
  source: string;
  granted_at: string;
  file_count: number;
  storage_bytes: number;
}

export interface AdminBucket {
  id: number;
  name: string;
  display_name: string;
  tenant_code: string | null;
  bucket_type: 'proposal' | 'local';
  is_orphan: boolean;
  owner_name: string | null;
  is_deletable: boolean;
  shares_count: number;
  size_bytes: number;
  num_objects: number;
  created_at: string;
}

export interface AdminUser {
  id: number;
  membership_id: number;
  user_id: number;
  tenant_id: number;
  ceph_username: string;
  display_name: string;
  email: string;
  tenant_code: string;
  tenant_name: string;
  role: string;
  uo_code: string;
  access_status: 'authorized' | 'revoked' | 'not_configured';
  access_revoked_at: string | null;
  access_revocation_reason: '' | 'mapping_removed' | 'claim_missing';
  access_revoked_group: string;
  last_login: string | null;
  file_count: number;
  total_file_size: number;
}

export interface AdminTenant {
  id: number;
  code: string;
  name: string;
  member_count: number;
  bucket_count: number;
  storage_bytes: number;
  initialized: boolean | null;
  buckets_auto: number;
  buckets_manual: number;
}

export interface AdminGroupMapping {
  id: number;
  authentik_group: string;
  tenant_code: string;
  tenant_id: number;
  role: 'rw' | 'ro';
}

export interface AdminTenantActivation {
  structure: string;
  available_in_rgwsquared: boolean;
  tenant_id: number | null;
  tenant_code: string;
  tenant_name: string;
  has_tenant: boolean;
  initialized: boolean | null;
  buckets_auto: number;
  buckets_manual: number;
  member_count: number;
  bucket_count: number;
  storage_bytes: number;
  group_mappings: AdminGroupMapping[];
  group_mapping_count: number;
  has_group_mapping: boolean;
  group_mapping_ready: boolean;
  group_mapping_issue: string;
  required_group_name: string | null;
  access_model: 'rgwsquared_synced' | 'authentik_managed' | null;
  role_source: 'authentik_group' | 'rgwsquared';
  suggested_rw_group: string;
  suggested_ro_group: string;
  has_rw_mapping: boolean;
  has_ro_mapping: boolean;
  requires_uo_sync: boolean;
  uo_ready: boolean;
  missing_uo_count: number;
  write_capable_member_count: number;
  fully_active: boolean;
}

export interface AdminUOMapping {
  id: number;
  uo_code: string;
  institution_name: string;
  tenant_code: string;
}

export interface AdminAvailableTenant {
  structure: string;
  has_tenant: boolean;
  tenant_id: number | null;
  initialized: boolean | null;  // null = structureInfo unavailable
  buckets_auto: number;
  buckets_manual: number;
}

export interface CreateTenantPayload {
  structure: string;
  name: string;
  bucket_name_prefix?: string;
  access_model: 'rgwsquared_synced' | 'authentik_managed';
}

export interface AdminUserFile {
  file_key: string;
  bucket_name: string;
  tenant_code: string;
  file_size: number;
  uploaded_at: string;
}

export interface FileNameRule {
  id: number;
  tenant_code: string;
  substring: string;
}

export interface FileDeviation {
  user_id: number;
  ceph_username: string;
  display_name: string;
  deviation_count: number;
  files: { file_key: string; bucket_name: string }[];
}

export interface TenantDocument {
  tab_name: string;
  content: string;
  is_visible: boolean;
  updated_at: string;
}

export interface FileFormatEntry {
  extension: string;
  count: number;
  size_bytes: number;
  proposal_count: number;
  local_count: number;
  proposal_size: number;
  local_size: number;
}

export interface FileFormatsResponse {
  total_files: number;
  total_size_bytes: number;
  formats: FileFormatEntry[];
}
