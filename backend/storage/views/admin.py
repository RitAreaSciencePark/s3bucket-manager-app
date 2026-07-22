"""Admin API views — purpose-built React admin backend.

All endpoints require AdminPanelPermission (except admin OAuth token exchange).
"""

import base64
import csv
import io
import logging

from django.contrib.auth import login as django_login
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from storage.auth_permissions import AdminPanelPermission
from storage.tokens import AdminRefreshToken

from storage.models import (
    Tenant,
    TenantMembership,
    Bucket,
    BucketPermission,
    UOMapping,
    GroupTenantMapping,
    FileUploadRecord,
    FileNameRule,
    TenantDocument,
)
from storage.services.s3_ops import (
    ensure_structure_initialized,
    get_mgmt_s3_client,
    delete_object,
    get_bucket_stats_for_tenant,
)
from storage.services.sync_service import refresh_local_cache
from storage.services.rgw_squared import (
    RGWSquaredClient,
    RGWSquaredError,
    delete_bucket_via_rgw,
)
from storage.access import (
    NFFADI_AUTHENTIK_GROUP,
    configured_tenant_ids,
    current_authentik_groups,
    is_nffadi_tenant,
    is_rgwsquared_synced,
    suggested_group_name,
    WRITE_CAPABLE_ROLES,
)

logger = logging.getLogger(__name__)


class AdminExchangeThrottle(SimpleRateThrottle):
    scope = "admin_exchange"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


def _client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _get_sync_client():
    """Create RGWSquaredClient from Django settings."""
    from django.conf import settings

    return RGWSquaredClient(
        base_url=settings.RGWSQUARED_URL,
        username=settings.RGWSQUARED_USERNAME,
        password=settings.RGWSQUARED_PASSWORD,
    )


def _structure_name(tenant):
    return tenant.rgwsquared_structure or tenant.code


def _error_response(message, response_status=status.HTTP_500_INTERNAL_SERVER_ERROR):
    return Response({"error": str(message)}, status=response_status)


@extend_schema(
    summary="Exchange OAuth2 session for admin panel JWT",
    description=(
        "Called by the admin frontend after Authentik OAuth completes with "
        "`?next=/admin/auth/callback`. Requires membership in AUTHENTIK_ADMIN_GROUP."
    ),
    responses={
        200: OpenApiResponse(description="Admin JWT tokens"),
        401: OpenApiResponse(description="No active OAuth2 session"),
        403: OpenApiResponse(description="User is not in the Authentik admin group"),
    },
    tags=["Admin"],
)
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([AdminExchangeThrottle])
def admin_exchange_token(request):
    """Exchange OAuth2 session for admin-panel JWT tokens."""
    client_ip = _client_ip(request)

    if not request.user.is_authenticated:
        return Response(
            {"error": "No active session. Please login via Authentik first."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    user = request.user
    user.refresh_from_db()

    if not user.can_access_system:
        logger.warning(
            "Admin exchange rejected username=%s ip=%s reason=pending_approval",
            user.username,
            client_ip,
        )
        return Response(
            {"error": "Account pending approval."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not user.is_staff:
        logger.warning(
            "Admin exchange rejected username=%s ip=%s reason=not_staff",
            user.username,
            client_ip,
        )
        return Response(
            {"error": "Account is not authorized for admin panel access."},
            status=status.HTTP_403_FORBIDDEN,
        )

    logger.info(
        "Admin exchange succeeded username=%s ip=%s is_staff=%s",
        user.username,
        client_ip,
        user.is_staff,
    )
    django_login(request, user)
    request.session.set_expiry(86400)  # 24h: docs access outlives OAuth handshake sessions (300s)
    refresh = AdminRefreshToken.for_admin_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "username": user.username,
            "display_name": user.display_name,
        }
    )


def _clean_email(email):
    """Return empty string for placeholder emails."""
    return "" if email.endswith("@placeholder.local") else email


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_permissions(request):
    """All bucket permissions — used by BucketsView expanded access."""
    perms = BucketPermission.objects.select_related(
        "user", "bucket", "bucket__tenant"
    ).order_by("-granted_at")

    # (user_id, tenant_id) → ceph_username
    membership_map = {
        (m["user_id"], m["tenant_id"]): m["ceph_username"]
        for m in TenantMembership.objects.filter(is_active=True).values(
            "user_id", "tenant_id", "ceph_username"
        )
    }

    # Per-(user, bucket) file stats — one aggregated query
    file_stats = {
        (row["uploaded_by_id"], row["bucket_id"]): (row["file_count"], row["storage"] or 0)
        for row in FileUploadRecord.objects.values("uploaded_by_id", "bucket_id")
        .annotate(file_count=Count("id"), storage=Sum("file_size"))
    }

    return Response(
        [
            {
                "id": p.id,
                "username": p.user.username,
                "ceph_username": membership_map.get(
                    (p.user_id, p.bucket.tenant_id), p.user.username
                ),
                "email": _clean_email(p.user.email),
                "bucket_name": p.bucket.display_name or p.bucket.name,
                "bucket_ceph_name": p.bucket.name,
                "tenant_code": p.bucket.tenant.code if p.bucket.tenant else None,
                "permission": p.permission,
                "source": p.source,
                "granted_at": p.granted_at,
                "file_count": file_stats.get((p.user_id, p.bucket_id), (0, 0))[0],
                "storage_bytes": file_stats.get((p.user_id, p.bucket_id), (0, 0))[1],
            }
            for p in perms
        ]
    )


def _bucket_is_orphan(bucket):
    """True when bucket exists in RGW but was not created via the webapp."""
    if bucket.bucket_type == Bucket.PROPOSAL:
        return False
    return not BucketPermission.objects.filter(
        bucket=bucket,
        permission="owner",
        source="local",
    ).exists()


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_buckets(request):
    """All buckets with storage stats from RGW."""
    try:
        buckets = list(
            Bucket.objects.select_related("tenant", "owner")
            .annotate(
                shares_count=Count(
                    "permissions",
                    filter=~Q(permissions__permission="owner"),
                )
            )
            .order_by("tenant__code", "name")
        )

        stats = {}
        for tenant in Tenant.objects.filter(is_active=True):
            names = [b.name for b in buckets if b.tenant_id == tenant.id]
            stats.update(get_bucket_stats_for_tenant(tenant, names))

        return Response(
            [
                {
                    "id": b.id,
                    "name": b.name,
                    "display_name": b.display_name,
                    "tenant_code": b.tenant.code if b.tenant else None,
                    "bucket_type": b.bucket_type,
                    "is_orphan": _bucket_is_orphan(b),
                    "owner_name": b.owner.display_name
                    if b.owner
                    else (b.tenant.code if b.tenant else None),
                    "is_deletable": b.is_deletable,
                    "shares_count": b.shares_count,
                    "size_bytes": stats.get(b.name, {}).get("size_bytes", 0),
                    "num_objects": stats.get(b.name, {}).get("num_objects", 0),
                    "created_at": b.created_at,
                }
                for b in buckets
            ]
        )
    except Exception as e:
        logger.error(f"admin_buckets failed: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET", "DELETE"])
@permission_classes([AdminPanelPermission])
def admin_bucket_detail(request, bucket_id):
    """Bucket detail or delete. DELETE only if is_deletable=True."""
    try:
        bucket = Bucket.objects.select_related("tenant", "owner").get(id=bucket_id)
    except Bucket.DoesNotExist:
        return Response({"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        if not bucket.is_deletable:
            return Response(
                {"error": "This bucket cannot be deleted"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            client = _get_sync_client()
            ensure_structure_initialized(bucket.tenant, client=client)
            delete_bucket_via_rgw(
                client, _structure_name(bucket.tenant), bucket.name
            )
        except RGWSquaredError as e:
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
        bucket.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    from storage.services.s3_ops import list_objects

    files = []
    try:
        s3 = get_mgmt_s3_client(bucket.tenant)
        files = list_objects(s3, bucket.name)
    except Exception as e:
        logger.warning(f"Could not list objects in {bucket.name}: {e}")

    return Response(
        {
            "id": bucket.id,
            "name": bucket.name,
            "display_name": bucket.display_name,
            "tenant_code": bucket.tenant.code if bucket.tenant else None,
            "bucket_type": bucket.bucket_type,
            "owner_name": bucket.owner.display_name
            if bucket.owner
            else (bucket.tenant.code if bucket.tenant else None),
            "is_deletable": bucket.is_deletable,
            "created_at": bucket.created_at,
            "files": files,
        }
    )


@api_view(["DELETE"])
@permission_classes([AdminPanelPermission])
def admin_delete_file(request, bucket_id, file_key):
    """Delete a file from any bucket (admin only)."""
    try:
        bucket = Bucket.objects.select_related("tenant").get(id=bucket_id)
    except Bucket.DoesNotExist:
        return Response({"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        ensure_structure_initialized(bucket.tenant)
        s3 = get_mgmt_s3_client(bucket.tenant)
        delete_object(s3, bucket.name, file_key)
    except RGWSquaredError as e:
        return _error_response(e, status.HTTP_400_BAD_REQUEST)
    except RuntimeError as e:
        return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:
        return Response(
            {"error": f"Delete failed: {e}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    from storage.models import FileUploadRecord

    FileUploadRecord.objects.filter(bucket=bucket, file_key=file_key).delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_users(request):
    """All users with tenant membership info and file upload summary."""
    try:
        tenant_code = request.query_params.get("tenant_code", "").strip()
        memberships = (
            TenantMembership.objects.select_related("user", "tenant")
            .filter(is_active=True)
        )
        if tenant_code:
            memberships = memberships.filter(tenant__code=tenant_code)
        memberships = memberships.order_by("tenant__code", "user__username")

        # Aggregate file counts per admin account row: one user in one tenant.
        file_aggs = (
            FileUploadRecord.objects.filter(uploaded_by__isnull=False)
            .values("uploaded_by_id", "bucket__tenant_id")
            .annotate(file_count=Count("id"), total_file_size=Sum("file_size"))
        )
        file_stats = {
            (row["uploaded_by_id"], row["bucket__tenant_id"]): (
                row["file_count"],
                row["total_file_size"] or 0,
            )
            for row in file_aggs
        }

        configured_ids = configured_tenant_ids()

        def access_status(membership):
            if membership.access_revoked_at is not None:
                return "revoked"
            if membership.tenant_id not in configured_ids:
                return "not_configured"
            return "authorized"

        return Response(
            [
                {
                    "id": m.id,
                    "membership_id": m.id,
                    "user_id": m.user.id,
                    "tenant_id": m.tenant.id,
                    "ceph_username": m.ceph_username,
                    "display_name": m.user.display_name,
                    "email": _clean_email(m.user.email),
                    "tenant_code": m.tenant.code,
                    "tenant_name": m.tenant.name,
                    "role": m.role,
                    "uo_code": m.uo_code,
                    "access_status": access_status(m),
                    "access_revoked_at": m.access_revoked_at,
                    "access_revocation_reason": m.access_revocation_reason,
                    "access_revoked_group": m.access_revoked_group,
                    "last_login": m.user.last_login,
                    "file_count": file_stats.get((m.user.id, m.tenant.id), (0, 0))[0],
                    "total_file_size": file_stats.get(
                        (m.user.id, m.tenant.id), (0, 0)
                    )[1],
                }
                for m in memberships
            ]
        )
    except Exception as e:
        logger.error(f"admin_users failed: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_tenants(request):
    """All tenants with member/bucket counts and storage."""
    try:
        tenants = (
            Tenant.objects.filter(is_active=True)
            .annotate(
                member_count=Count(
                    "memberships", filter=Q(memberships__is_active=True), distinct=True
                ),
                bucket_count=Count("buckets", distinct=True),
            )
            .order_by("code")
        )

        tenant_storage = {}
        structure_status = {}
        client = _get_sync_client()
        # Fetch all structure info upfront (one call per structure) to avoid
        # redundant RGWSquared calls inside get_bucket_stats_for_tenant.
        for tenant in tenants:
            tenant_buckets = list(
                Bucket.objects.filter(tenant=tenant).values_list("name", flat=True)
            )
            stats = get_bucket_stats_for_tenant(tenant, tenant_buckets)
            tenant_storage[tenant.code] = sum(
                bucket_stats.get("size_bytes", 0) for bucket_stats in stats.values()
            )
            try:
                info = client.get_structure_info(_structure_name(tenant))
            except Exception as e:
                logger.warning(f"Could not fetch structureInfo for {tenant.code}: {e}")
                info = {}
            structure_status[tenant.code] = {
                "initialized": info.get("initialized"),  # None if unavailable
                "buckets_auto": info.get("bucketsAuto", 0),
                "buckets_manual": info.get("bucketsManual", 0),
            }

        return Response(
            [
                {
                    "id": t.id,
                    "code": t.code,
                    "name": t.name,
                    "member_count": t.member_count,
                    "bucket_count": t.bucket_count,
                    "storage_bytes": tenant_storage.get(t.code, 0),
                    **structure_status.get(t.code, {}),
                }
                for t in tenants
            ]
        )
    except Exception as e:
        logger.error(f"admin_tenants failed: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_tenant_activation(request):
    """Operator activation status for every RGWSquared structure.

    This is the admin panel's single readiness model: RGWSquared owns the
    structure state, Django owns local activation metadata, group mappings, and
    UO coverage for tenants that require it.
    """
    try:
        client = _get_sync_client()
        try:
            structures = [str(s).strip() for s in client.list_structures() if str(s).strip()]
        except Exception as e:
            return Response(
                {"error": f"RGWSquared error: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        tenant_rows = (
            Tenant.objects.filter(is_active=True)
            .annotate(
                member_count=Count(
                    "memberships", filter=Q(memberships__is_active=True), distinct=True
                ),
                bucket_count=Count("buckets", distinct=True),
            )
            .order_by("code")
        )
        tenants = {_structure_name(t): t for t in tenant_rows}
        all_codes = sorted(set(structures) | set(tenants.keys()))
        structures_set = set(structures)

        structure_status = {}
        for code in structures:
            try:
                info = client.get_structure_info(code)
            except Exception as e:
                logger.warning(f"Could not fetch structureInfo for {code}: {e}")
                info = {}
            structure_status[code] = {
                "initialized": info.get("initialized"),
                "buckets_auto": info.get("bucketsAuto", 0),
                "buckets_manual": info.get("bucketsManual", 0),
            }

        group_mappings_by_tenant = {}
        for mapping in GroupTenantMapping.objects.select_related("tenant").filter(
            tenant__is_active=True
        ).order_by("tenant__code", "role", "authentik_group"):
            group_mappings_by_tenant.setdefault(mapping.tenant_id, []).append(
                {
                    "id": mapping.id,
                    "authentik_group": mapping.authentik_group,
                    "tenant_id": mapping.tenant_id,
                    "tenant_code": mapping.tenant.code,
                    "role": mapping.role,
                }
            )

        uo_required_tenant_ids = set(
            UOMapping.objects.values_list("tenant_id", flat=True).distinct()
        )
        write_counts = {
            row["tenant_id"]: row["count"]
            for row in TenantMembership.objects.filter(
                is_active=True,
                role__in=WRITE_CAPABLE_ROLES,
            )
            .values("tenant_id")
            .annotate(count=Count("id"))
        }
        missing_uo_counts = {
            row["tenant_id"]: row["count"]
            for row in TenantMembership.objects.filter(
                Q(uo_code="") | Q(uo_code__isnull=True),
                is_active=True,
                role__in=WRITE_CAPABLE_ROLES,
                tenant_id__in=uo_required_tenant_ids,
            )
            .values("tenant_id")
            .annotate(count=Count("id"))
        }
        storage_by_tenant = {
            row["bucket__tenant_id"]: row["storage"] or 0
            for row in FileUploadRecord.objects.filter(bucket__tenant__is_active=True)
            .values("bucket__tenant_id")
            .annotate(storage=Sum("file_size"))
        }

        rows = []
        for code in all_codes:
            tenant = tenants.get(code)
            status_info = structure_status.get(
                code,
                {"initialized": None, "buckets_auto": 0, "buckets_manual": 0},
            )
            mappings = group_mappings_by_tenant.get(tenant.id, []) if tenant else []
            requires_uo_sync = bool(tenant and tenant.id in uo_required_tenant_ids)
            missing_uo_count = missing_uo_counts.get(tenant.id, 0) if tenant else 0
            uo_ready = not requires_uo_sync or missing_uo_count == 0
            has_tenant = tenant is not None
            has_group_mapping = bool(mappings)
            initialized = status_info.get("initialized")
            nffadi_policy = bool(tenant and is_nffadi_tenant(tenant)) or code.upper() == "NFFADI"
            rgwsquared_policy = bool(tenant and is_rgwsquared_synced(tenant))
            required_group_name = NFFADI_AUTHENTIK_GROUP if nffadi_policy else None
            role_source = "rgwsquared" if rgwsquared_policy else "authentik_group"
            group_mapping_ready = has_group_mapping
            group_mapping_issue = ""
            if nffadi_policy:
                valid_nffadi = [m for m in mappings if m["authentik_group"] == NFFADI_AUTHENTIK_GROUP and m["role"] == "rw"]
                group_mapping_ready = len(valid_nffadi) == 1 and len(mappings) == 1
                if not mappings:
                    group_mapping_issue = f"Add exactly one mapping: {NFFADI_AUTHENTIK_GROUP}"
                elif not group_mapping_ready:
                    group_mapping_issue = f"NFFADI must have exactly one eligibility mapping named {NFFADI_AUTHENTIK_GROUP}"
            elif rgwsquared_policy:
                group_mapping_ready = len(mappings) == 1
                if not mappings:
                    group_mapping_issue = "Add exactly one Authentik eligibility mapping"
                elif not group_mapping_ready:
                    group_mapping_issue = "RGWSquared-synced tenants require exactly one eligibility mapping"

            fully_active = bool(
                initialized is True
                and has_tenant
                and group_mapping_ready
                and uo_ready
            )

            rows.append(
                {
                    "structure": code,
                    "available_in_rgwsquared": code in structures_set,
                    "tenant_id": tenant.id if tenant else None,
                    "tenant_code": tenant.code if tenant else code,
                    "tenant_name": tenant.name if tenant else code,
                    "has_tenant": has_tenant,
                    "initialized": initialized,
                    "buckets_auto": status_info.get("buckets_auto", 0),
                    "buckets_manual": status_info.get("buckets_manual", 0),
                    "member_count": getattr(tenant, "member_count", 0) if tenant else 0,
                    "bucket_count": getattr(tenant, "bucket_count", 0) if tenant else 0,
                    "storage_bytes": storage_by_tenant.get(tenant.id, 0) if tenant else 0,
                    "group_mappings": mappings,
                    "group_mapping_count": len(mappings),
                    "has_group_mapping": has_group_mapping,
                    "group_mapping_ready": group_mapping_ready,
                    "group_mapping_issue": group_mapping_issue,
                    "required_group_name": required_group_name,
                    "access_model": tenant.access_model if tenant else None,
                    "role_source": role_source,
                    "suggested_rw_group": suggested_group_name(code, "rw"),
                    "suggested_ro_group": suggested_group_name(code, "ro"),
                    "has_rw_mapping": any(m["role"] == "rw" for m in mappings),
                    "has_ro_mapping": any(m["role"] == "ro" for m in mappings),
                    "requires_uo_sync": requires_uo_sync,
                    "uo_ready": uo_ready,
                    "missing_uo_count": missing_uo_count,
                    "write_capable_member_count": write_counts.get(tenant.id, 0) if tenant else 0,
                    "fully_active": fully_active,
                }
            )

        return Response(rows)
    except Exception as e:
        logger.error(f"admin_tenant_activation failed: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET", "POST"])
@permission_classes([AdminPanelPermission])
def admin_group_mappings(request):
    """List or create group-tenant mappings."""
    if request.method == "GET":
        mappings = GroupTenantMapping.objects.select_related("tenant").order_by(
            "tenant__code", "role"
        )
        return Response(
            [
                {
                    "id": m.id,
                    "authentik_group": m.authentik_group,
                    "tenant_code": m.tenant.code,
                    "tenant_id": m.tenant.id,
                    "role": m.role,
                }
                for m in mappings
            ]
        )

    group = request.data.get("authentik_group", "").strip()
    tenant_id = request.data.get("tenant_id")
    role = request.data.get("role", "rw")

    if not group or not tenant_id:
        return Response(
            {"error": "authentik_group and tenant_id required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if role not in ("rw", "ro"):
        return Response(
            {"error": "role must be 'rw' or 'ro'"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    if GroupTenantMapping.objects.filter(authentik_group=group).exists():
        return Response(
            {"error": f"Group '{group}' is already mapped to a tenant."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if is_rgwsquared_synced(tenant):
        if is_nffadi_tenant(tenant) and group != NFFADI_AUTHENTIK_GROUP:
            return Response(
                {"error": f"NFFADI must use Authentik group '{NFFADI_AUTHENTIK_GROUP}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if role != "rw":
            return Response(
                {"error": "RGWSquared-synced mappings are eligibility gates; roles come from RGWSquared."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        existing = GroupTenantMapping.objects.filter(tenant=tenant).first()
        if existing:
            return Response(
                {
                    "error": (
                        f'Tenant {tenant.code} already has eligibility mapping '
                        f'"{existing.authentik_group}". Remove it first.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        existing = GroupTenantMapping.objects.filter(tenant=tenant, role=role).first()
        if existing:
            return Response(
                {
                    "error": (
                        f'Tenant {tenant.code} already has a {role} group mapping '
                        f'"{existing.authentik_group}". Remove it first.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    mapping = GroupTenantMapping.objects.create(
        authentik_group=group, tenant=tenant, role=role
    )

    return Response(
        {
            "id": mapping.id,
            "authentik_group": mapping.authentik_group,
            "tenant_code": tenant.code,
            "tenant_id": tenant.id,
            "role": mapping.role,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["DELETE"])
@permission_classes([AdminPanelPermission])
def admin_group_mapping_delete(request, mapping_id):
    """Delete a group mapping and revoke only access derived from it."""
    with transaction.atomic():
        try:
            mapping = (
                GroupTenantMapping.objects.select_for_update()
                .select_related("tenant")
                .get(id=mapping_id)
            )
        except GroupTenantMapping.DoesNotExist:
            return Response(
                {"error": "Mapping not found"}, status=status.HTTP_404_NOT_FOUND
            )

        tenant = mapping.tenant
        removed_group = mapping.authentik_group
        mapping.delete()
        remaining = list(GroupTenantMapping.objects.filter(tenant=tenant))
        revoked = 0
        downgraded = 0
        now = timezone.now()

        memberships = (
            TenantMembership.objects.select_for_update()
            .select_related("user")
            .filter(
                tenant=tenant,
                is_active=True,
                access_revoked_at__isnull=True,
            )
        )
        for membership in memberships:
            if is_rgwsquared_synced(tenant) or not remaining:
                should_revoke = True
                fallback = None
            else:
                groups = current_authentik_groups(membership.user)
                if removed_group not in groups:
                    continue
                matches = [m for m in remaining if m.authentik_group in groups]
                fallback = next((m for m in matches if m.role == "rw"), None)
                if fallback is None and matches:
                    fallback = matches[0]
                should_revoke = fallback is None

            if should_revoke:
                membership.access_revoked_at = now
                membership.access_revocation_reason = "mapping_removed"
                membership.access_revoked_group = removed_group
                membership.save(
                    update_fields=[
                        "access_revoked_at",
                        "access_revocation_reason",
                        "access_revoked_group",
                    ]
                )
                revoked += 1
            elif membership.role != fallback.role:
                membership.role = fallback.role
                update_fields = ["role"]
                if fallback.role == "ro" and membership.uo_code:
                    membership.uo_code = ""
                    update_fields.append("uo_code")
                membership.save(update_fields=update_fields)
                downgraded += 1

    logger.info(
        "Removed group mapping %s -> %s; revoked=%s role_changed=%s",
        removed_group,
        tenant.code,
        revoked,
        downgraded,
    )
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_available_tenants(request):
    """Structures from RGWSquared, cross-referenced with existing tenants."""
    from django.conf import settings

    try:
        client = RGWSquaredClient(
            base_url=settings.RGWSQUARED_URL,
            username=settings.RGWSQUARED_USERNAME,
            password=settings.RGWSQUARED_PASSWORD,
        )
        structures = client.list_structures()
    except Exception as e:
        return Response(
            {"error": f"RGWSquared error: {e}"}, status=status.HTTP_502_BAD_GATEWAY
        )

    existing = set(Tenant.objects.values_list("code", flat=True))

    data = []
    for s in structures:
        try:
            info = client.get_structure_info(s)
        except Exception as e:
            logger.warning(f"Could not fetch structureInfo for {s}: {e}")
            info = {}
        data.append(
            {
                "structure": s,
                "has_tenant": s in existing,
                "tenant_id": Tenant.objects.filter(code=s)
                .values_list("id", flat=True)
                .first(),
                "initialized": info.get("initialized"),  # None if structureInfo failed
                "buckets_auto": info.get("bucketsAuto", 0),
                "buckets_manual": info.get("bucketsManual", 0),
            }
        )
    return Response(data)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_uo_mappings(request):
    """Read-only UO mapping list."""
    mappings = UOMapping.objects.select_related("tenant").order_by(
        "tenant__code", "uo_code"
    )
    return Response(
        [
            {
                "id": m.id,
                "uo_code": m.uo_code,
                "institution_name": m.institution_name,
                "tenant_code": m.tenant.code,
            }
            for m in mappings
        ]
    )


@api_view(["POST"])
@permission_classes([AdminPanelPermission])
def admin_sync_refresh(request):
    """Refresh local cache for a tenant. Accepts tenant_id or structure_code."""
    structure_code = request.data.get("structure_code")
    tenant_id = request.data.get("tenant_id")

    if not structure_code and not tenant_id:
        return Response(
            {"error": "structure_code or tenant_id required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        if structure_code:
            tenant = Tenant.objects.get(code=structure_code)
        else:
            tenant = Tenant.objects.get(id=tenant_id)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        stats = refresh_local_cache(tenant)
        if "error" in stats:
            return Response(
                {"error": stats["error"]}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(stats)
    except Exception as e:
        logger.error(f"Sync refresh failed for {tenant.code}: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([AdminPanelPermission])
def admin_sync_upload_csv(request):
    """Upload instruments CSV to RGWSquared (csvUpload)."""
    csv_file = request.FILES.get("file")
    if not csv_file:
        return Response(
            {"error": "CSV file required"}, status=status.HTTP_400_BAD_REQUEST
        )

    MAX_CSV_BYTES = 10 * 1024 * 1024
    if csv_file.size > MAX_CSV_BYTES:
        return Response(
            {"error": f"CSV file too large (max {MAX_CSV_BYTES // (1024 * 1024)} MB)"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    csv_bytes = csv_file.read()
    csv_base64 = base64.b64encode(csv_bytes).decode()

    try:
        client = _get_sync_client()
        result = client.upload_csv(csv_base64)
    except Exception as e:
        logger.error(f"CSV upload failed: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Parse the CSV locally to update UO codes immediately.
    # CSV column header is "institution" (lowercase) — verified from the actual NFFADI CSV.
    # Multi-strategy matching: try ceph_username, email-style usernames, and instrument_scientist_email.
    instruments_uploaded = 0
    uo_updated = 0
    uo_cleared = 0
    try:
        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
        # user_institution: all known key forms → institution name (whitespace-normalized)
        user_institution: dict = {}

        for row in reader:
            instruments_uploaded += 1
            username = (row.get("instrument_scientist_username") or "").strip()
            email_col = (row.get("instrument_scientist_email") or "").strip().lower()
            # Normalize whitespace (CSV has double spaces in some institution names)
            institution = " ".join((row.get("institution") or "").split())

            if not institution:
                continue

            if username:
                user_institution[username] = institution
                if "@" in username:
                    # email-style username: also register the prefix
                    user_institution[username.split("@")[0]] = institution

            if email_col:
                user_institution[email_col] = institution
                user_institution[email_col.split("@")[0]] = institution

        if user_institution:
            tenant = Tenant.objects.filter(code="NFFADI").first()
            if tenant:
                # Match by ceph_username (direct match)
                memberships_ceph = list(
                    TenantMembership.objects.filter(
                        tenant=tenant,
                        ceph_username__in=user_institution.keys(),
                        role__in=WRITE_CAPABLE_ROLES,
                    ).select_related("user")
                )

                # Also match by user.email (for users whose ceph_username ≠ CSV key but email matches)
                email_keys = {k for k in user_institution if "@" in k}
                matched_ceph_users = {m.user_id for m in memberships_ceph}
                memberships_email = list(
                    TenantMembership.objects.filter(
                        tenant=tenant,
                        user__email__in=email_keys,
                        role__in=WRITE_CAPABLE_ROLES,
                    )
                    .select_related("user")
                    .exclude(user_id__in=matched_ceph_users)
                ) if email_keys else []

                for m in memberships_ceph + memberships_email:
                    user_email = (m.user.email or "").lower() if m.user else ""
                    institution_val = (
                        user_institution.get(m.ceph_username)
                        or user_institution.get(user_email)
                        or user_institution.get(user_email.split("@")[0] if "@" in user_email else "")
                        or ""
                    )
                    if not institution_val:
                        continue

                    uo = UOMapping.objects.filter(
                        tenant=tenant,
                        institution_name__icontains=institution_val,
                    ).first()
                    if uo and m.uo_code != uo.uo_code:
                        m.uo_code = uo.uo_code
                        m.save(update_fields=["uo_code"])
                        uo_updated += 1

                uo_cleared = TenantMembership.objects.filter(
                    tenant=tenant,
                ).exclude(
                    role__in=WRITE_CAPABLE_ROLES,
                ).exclude(
                    uo_code="",
                ).update(uo_code="")
    except Exception as e:
        logger.warning(f"CSV UO code parsing failed (non-fatal): {e}")

    result["instruments_uploaded"] = instruments_uploaded
    result["uo_codes_updated"] = uo_updated
    result["uo_codes_cleared"] = uo_cleared
    return Response(result)


@api_view(["POST"])
@permission_classes([AdminPanelPermission])
def admin_sync_update_structure(request):
    """Trigger RGWSquared structureUpdate. Ceph sync is internal to RGWSquared."""
    structure = request.data.get("structure", "NFFADI")
    update_from_ext = request.data.get("update_from_ext", True)

    try:
        client = _get_sync_client()
        result = client.update_structure(structure, update_from_ext=update_from_ext)
        return Response(result)
    except Exception as e:
        logger.error(f"Structure update failed for {structure}: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([AdminPanelPermission])
def admin_create_tenant(request):
    """Create a Django Tenant record from an existing RGWSquared structure.

    The structure must already exist in RGWSquared (visible via available-tenants).
    This endpoint only creates the local DB record — it does NOT create a new
    RGWSquared structure. Use RGWSquared CLI/API directly for that.
    """
    structure = request.data.get("structure", "").strip()
    name = request.data.get("name", "").strip()
    bucket_prefix = request.data.get("bucket_name_prefix", "").strip()
    access_model = request.data.get("access_model", "").strip()

    if not structure or not name:
        return Response(
            {"error": "structure and name are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if access_model not in dict(Tenant.ACCESS_MODEL_CHOICES):
        return Response(
            {"error": "access_model must be 'rgwsquared_synced' or 'authentik_managed'"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (
        structure.upper() == "NFFADI"
        and access_model != Tenant.RGWSQUARED_SYNCED
    ):
        return Response(
            {"error": "NFFADI requires the rgwsquared_synced access model"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if Tenant.objects.filter(code=structure).exists():
        return Response(
            {"error": f"Tenant '{structure}' already exists in the database."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    tenant = Tenant.objects.create(
        code=structure,
        name=name,
        rgwsquared_structure=structure,
        bucket_name_prefix=bucket_prefix,
        access_model=access_model,
        is_active=True,
    )
    logger.info(f"Created tenant: {tenant.code} ({tenant.name})")

    # Load static UO mapping fixtures for this tenant (idempotent — safe if no fixture exists).
    try:
        from django.core.management import call_command
        call_command("load_uo_mappings", tenant=structure, verbosity=0)
    except Exception as e:
        logger.warning(f"load_uo_mappings after activation failed (non-fatal): {e}")

    # Auto-populate from RGWSquared immediately after activation.
    # Non-fatal: the tenant record is created regardless; sync result/error are surfaced
    # in the response so the admin can see what was found without an extra manual sync step.
    sync_stats = None
    sync_error = None
    try:
        stats = refresh_local_cache(tenant)
        if stats and "error" in stats:
            sync_error = stats.pop("error")
            sync_stats = stats if stats else None
        else:
            sync_stats = stats
    except Exception as e:
        logger.warning(f"Auto-sync after activation failed for {tenant.code}: {e}")
        sync_error = str(e)

    return Response(
        {
            "id": tenant.id,
            "code": tenant.code,
            "name": tenant.name,
            "rgwsquared_structure": tenant.rgwsquared_structure,
            "bucket_name_prefix": tenant.bucket_name_prefix,
            "access_model": tenant.access_model,
            "sync_stats": sync_stats,
            "sync_error": sync_error,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_membership_files(request, membership_id):
    """File upload history for one tenant-scoped admin account row."""
    try:
        membership = TenantMembership.objects.select_related("user", "tenant").get(
            id=membership_id,
            is_active=True,
        )
    except TenantMembership.DoesNotExist:
        return Response({"error": "Membership not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        records = (
            FileUploadRecord.objects.filter(
                uploaded_by=membership.user,
                bucket__tenant=membership.tenant,
            )
            .select_related("bucket__tenant")
            .order_by("-uploaded_at")
        )
        return Response([
            {
                "file_key": r.file_key,
                "bucket_name": r.bucket.display_name or r.bucket.name,
                "tenant_code": r.bucket.tenant.code,
                "file_size": r.file_size,
                "uploaded_at": r.uploaded_at,
            }
            for r in records
        ])
    except Exception as e:
        logger.error(f"admin_membership_files failed for membership {membership_id}: {e}")
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET", "POST"])
@permission_classes([AdminPanelPermission])
def admin_file_name_rules(request):
    """List or create file naming rules for a tenant."""
    if request.method == "GET":
        tenant_code = request.query_params.get("tenant_code")
        qs = FileNameRule.objects.select_related("tenant").all()
        if tenant_code:
            qs = qs.filter(tenant__code=tenant_code)
        return Response([
            {"id": r.id, "tenant_code": r.tenant.code, "substring": r.substring}
            for r in qs.order_by("tenant__code", "substring")
        ])

    # POST
    tenant_code = request.data.get("tenant_code", "").strip()
    substring = request.data.get("substring", "").strip()
    if not tenant_code or not substring:
        return Response(
            {"error": "tenant_code and substring are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        tenant = Tenant.objects.get(code=tenant_code)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    rule, created = FileNameRule.objects.get_or_create(tenant=tenant, substring=substring)
    return Response(
        {"id": rule.id, "tenant_code": tenant.code, "substring": rule.substring},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["DELETE"])
@permission_classes([AdminPanelPermission])
def admin_file_name_rule_detail(request, rule_id):
    """Delete a single file naming rule."""
    try:
        FileNameRule.objects.filter(id=rule_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_file_deviations(request):
    """Users whose uploaded files violate the tenant's naming rules.

    Deviation = file_key contains NONE of the required substrings (OR logic).
    Returns no_rules=True if the tenant has no rules defined.
    """
    tenant_code = request.query_params.get("tenant_code", "").strip()
    if not tenant_code:
        return Response(
            {"error": "tenant_code query param required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        tenant = Tenant.objects.get(code=tenant_code)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    rules = list(FileNameRule.objects.filter(tenant=tenant).values_list("substring", flat=True))
    if not rules:
        return Response({"no_rules": True, "deviations": []})

    records = (
        FileUploadRecord.objects.filter(
            bucket__tenant=tenant,
            uploaded_by__isnull=False,
        )
        .select_related("uploaded_by", "bucket")
    )

    # Group by user; collect deviating files.
    from collections import defaultdict
    user_files: dict = defaultdict(list)
    user_info: dict = {}
    for r in records:
        fname = r.file_key.split("/")[-1] or r.file_key
        if not any(sub.lower() in fname.lower() for sub in rules):
            uid = r.uploaded_by_id
            user_files[uid].append({
                "file_key": r.file_key,
                "bucket_name": r.bucket.display_name or r.bucket.name,
            })
            user_info[uid] = {
                "user_id": uid,
                "ceph_username": r.uploaded_by.username,
                "display_name": r.uploaded_by.display_name,
            }

    deviations = [
        {**user_info[uid], "deviation_count": len(files), "files": files}
        for uid, files in user_files.items()
    ]
    deviations.sort(key=lambda d: d["deviation_count"], reverse=True)
    return Response({"no_rules": False, "deviations": deviations})


@api_view(["GET", "POST", "DELETE"])
@permission_classes([AdminPanelPermission])
def admin_tenant_document(request, tenant_code):
    """Read, upsert, or delete the markdown document for a tenant."""
    try:
        tenant = Tenant.objects.get(code=tenant_code)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "DELETE":
        TenantDocument.objects.filter(tenant=tenant).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == "GET":
        try:
            doc = tenant.document
            return Response({
                "tab_name": doc.tab_name,
                "content": doc.content,
                "is_visible": doc.is_visible,
                "updated_at": doc.updated_at,
            })
        except TenantDocument.DoesNotExist:
            return Response({"error": "No document"}, status=status.HTTP_404_NOT_FOUND)

    # POST — upsert
    tab_name = request.data.get("tab_name", "").strip() or request.POST.get("tab_name", "").strip()
    is_visible_raw = request.data.get("is_visible", request.POST.get("is_visible", None))
    uploaded_file = request.FILES.get("file")
    content_raw = request.data.get("content", request.POST.get("content", None))

    try:
        doc, _ = TenantDocument.objects.get_or_create(tenant=tenant)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if tab_name:
        doc.tab_name = tab_name
    if uploaded_file:
        try:
            doc.content = uploaded_file.read().decode("utf-8")
        except Exception as e:
            return Response({"error": f"Could not read file: {e}"}, status=status.HTTP_400_BAD_REQUEST)
    elif content_raw is not None:
        doc.content = content_raw

    if is_visible_raw is not None:
        if isinstance(is_visible_raw, bool):
            doc.is_visible = is_visible_raw
        else:
            doc.is_visible = str(is_visible_raw).lower() in ("true", "1", "yes")

    # Auto-hide if content is now empty.
    if not doc.content.strip():
        doc.is_visible = False

    doc.save()
    return Response({
        "tab_name": doc.tab_name,
        "content": doc.content,
        "is_visible": doc.is_visible,
        "updated_at": doc.updated_at,
    })


@api_view(["GET"])
@permission_classes([AdminPanelPermission])
def admin_file_formats(request):
    """Per-tenant file format distribution for storage auditing."""
    tenant_code = request.query_params.get("tenant_code", "").strip()
    if not tenant_code:
        return Response(
            {"error": "tenant_code query param required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        tenant = Tenant.objects.get(code=tenant_code)
    except Tenant.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    records = (
        FileUploadRecord.objects.filter(bucket__tenant=tenant)
        .select_related("bucket")
    )

    from collections import defaultdict

    def ext(file_key: str) -> str:
        fname = file_key.split("/")[-1]
        if "." in fname:
            return fname.rsplit(".", 1)[-1].lower()
        return "(unknown)"

    agg: dict = defaultdict(lambda: {
        "count": 0, "size_bytes": 0,
        "proposal_count": 0, "proposal_size": 0,
        "local_count": 0, "local_size": 0,
    })
    for r in records:
        e = ext(r.file_key)
        is_proposal = r.bucket.bucket_type == Bucket.PROPOSAL
        agg[e]["count"] += 1
        agg[e]["size_bytes"] += r.file_size
        if is_proposal:
            agg[e]["proposal_count"] += 1
            agg[e]["proposal_size"] += r.file_size
        else:
            agg[e]["local_count"] += 1
            agg[e]["local_size"] += r.file_size

    formats = [
        {"extension": k, **v}
        for k, v in sorted(agg.items(), key=lambda x: x[1]["count"], reverse=True)
    ]
    total_files = sum(f["count"] for f in formats)
    total_size = sum(f["size_bytes"] for f in formats)
    return Response({
        "total_files": total_files,
        "total_size_bytes": total_size,
        "formats": formats,
    })
