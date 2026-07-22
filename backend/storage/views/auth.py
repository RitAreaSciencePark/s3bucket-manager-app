"""Auth views: OAuth2 session→JWT bridge + tenant selection."""

import logging

from django.contrib.auth import logout as auth_logout
from drf_spectacular.utils import extend_schema, OpenApiResponse, inline_serializer
from rest_framework import status, serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from storage.models import TenantMembership, UOMapping, TenantDocument
from storage.serializers import UserSerializer
from storage.access import configured_tenant_ids

logger = logging.getLogger(__name__)


def _tenants_with_groups():
    """Return tenant IDs whose Authentik mapping is login-ready."""
    return configured_tenant_ids()


def _tenant_doc_info(tenant):
    """Return {tab_name, is_visible} for a tenant's document, or None if absent."""
    try:
        doc = tenant.document
        return {"tab_name": doc.tab_name, "is_visible": doc.is_visible and bool(doc.content.strip())}
    except TenantDocument.DoesNotExist:
        return None


@extend_schema(
    summary="Exchange OAuth2 session for JWT tokens",
    description=(
        "Called by the frontend immediately after the OIDC callback completes. "
        "Converts the server-side OAuth2 session into a pair of JWT tokens and destroys "
        "the session. Subsequent requests use `Authorization: Bearer <access>` — no cookies.\n\n"
        "Returns the user profile, all tenant memberships, and the pre-selected active tenant "
        "(auto-selected when the user belongs to exactly one tenant)."
    ),
    responses={
        200: OpenApiResponse(description="JWT tokens + user profile + tenant list"),
        401: OpenApiResponse(description="No active OAuth2 session — start login via /api/oauth/login/authentik/"),
        403: OpenApiResponse(description="Account pending approval"),
    },
    tags=["Auth"],
)
@api_view(["GET"])
@permission_classes([AllowAny])
def exchange_token(request):
    """Exchange OAuth2 session for JWT tokens.

    After OAuth2 completes, React calls this to get JWT tokens.
    Now includes tenant membership info for multi-tenant routing.
    """
    if not request.user.is_authenticated:
        return Response(
            {"error": "No active session. Please login via OAuth2 first."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    user = request.user
    user.refresh_from_db()

    if not user.can_access_system:
        return Response(
            {"error": "Account pending approval."},
            status=status.HTTP_403_FORBIDDEN,
        )
    memberships = TenantMembership.objects.filter(
        user=user, is_active=True, access_revoked_at__isnull=True
    ).select_related("tenant", "tenant__document")

    # Only expose tenants that have at least one Authentik group registered in GroupTenantMapping.
    # A tenant without a group mapping is not fully configured — the user has no valid login path.
    active_tenant_ids = _tenants_with_groups()

    # The frontend stores one active tenant and sends it back as X-Tenant-ID.
    tenants = [
        {
            "id": m.tenant.id,
            "code": m.tenant.code,
            "name": m.tenant.name,
            "role": m.role,
            "document": _tenant_doc_info(m.tenant),
        }
        for m in memberships
        if m.tenant_id in active_tenant_ids
    ]

    if not tenants:
        if user.is_staff:
            return Response(
                {
                    "error": (
                        "This account has admin access only. "
                        "Use /admin/login to access the admin panel."
                    ),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {"error": "No fully activated tenant is available for this account."},
            status=status.HTTP_403_FORBIDDEN,
        )

    active_tenant = None
    if len(tenants) == 1:
        active_tenant = tenants[0]

    refresh = RefreshToken.for_user(user)
    user_data = UserSerializer(user).data

    # JWTs become the API credential; the OAuth2 session ends after handoff.
    auth_logout(request)

    logger.info(
        f"JWT issued for user={user.username}, tenants={[t['code'] for t in tenants]}"
    )

    return Response(
        {
            "user": user_data,
            "tokens": {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            "tenants": tenants,
            "active_tenant": active_tenant,
            "tenant_selection_required": len(tenants) > 1,
        }
    )


@extend_schema(
    summary="Select active tenant for this session",
    description=(
        "Multi-tenant users must call this after login to set the working tenant context. "
        "The returned tenant ID should be included as `X-Tenant-ID` on all `/api/buckets/*` calls.\n\n"
        "Single-tenant users have this done automatically by `exchange_token`."
    ),
    request=inline_serializer("SelectTenantRequest", fields={"tenant_id": serializers.IntegerField()}),
    responses={
        200: OpenApiResponse(description="Active tenant info + membership details"),
        403: OpenApiResponse(description="User is not a member of the requested tenant"),
    },
    tags=["Auth"],
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def select_tenant(request):
    """Select active tenant for multi-tenant users.

    POST /api/auth/select-tenant/
    Body: {"tenant_id": 1}

    Returns updated user info with selected tenant context.
    """
    tenant_id = request.data.get("tenant_id")
    if not tenant_id:
        return Response(
            {"error": "tenant_id is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        membership = TenantMembership.objects.select_related("tenant", "tenant__document").get(
            user=request.user,
            tenant_id=tenant_id,
            is_active=True,
            access_revoked_at__isnull=True,
        )
    except TenantMembership.DoesNotExist:
        return Response(
            {"error": "You do not have access to this tenant"},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Require the tenant to have a registered Authentik group before allowing selection.
    if membership.tenant_id not in _tenants_with_groups():
        return Response(
            {"error": "This tenant has not been fully activated — no group mapping configured."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return Response(
        {
            "active_tenant": {
                "id": membership.tenant.id,
                "code": membership.tenant.code,
                "name": membership.tenant.name,
                "role": membership.role,
                "document": _tenant_doc_info(membership.tenant),
            },
            "membership": {
                "ceph_username": membership.ceph_username,
                "uo_code": membership.uo_code,
            },
        }
    )


@extend_schema(
    summary="Get current user profile with tenant memberships",
    description=(
        "Returns the authenticated user's profile and all active tenant memberships. "
        "The `tenants` array includes each tenant's document visibility state, used by "
        "the frontend to conditionally show tenant-specific navigation tabs."
    ),
    responses={200: OpenApiResponse(description="User profile with tenants array")},
    tags=["Auth"],
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_user(request):
    """Get current user info with tenant memberships."""
    user = request.user
    memberships = TenantMembership.objects.filter(
        user=user, is_active=True, access_revoked_at__isnull=True
    ).select_related("tenant", "tenant__document")

    user_data = UserSerializer(user).data

    uo_codes = [m.uo_code for m in memberships if m.uo_code]
    uo_name_map = {}
    if uo_codes:
        uo_name_map = {
            (um.tenant_id, um.uo_code): um.institution_name
            for um in UOMapping.objects.filter(
                tenant__in=[m.tenant_id for m in memberships],
                uo_code__in=uo_codes,
            )
        }

    active_tenant_ids = _tenants_with_groups()
    user_data["tenants"] = [
        {
            "id": m.tenant.id,
            "code": m.tenant.code,
            "name": m.tenant.name,
            "role": m.role,
            "uo_code": m.uo_code,
            "uo_name": uo_name_map.get((m.tenant_id, m.uo_code), ""),
            "document": _tenant_doc_info(m.tenant),
        }
        for m in memberships
        if m.tenant_id in active_tenant_ids
    ]
    return Response(user_data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def tenant_document(request):
    """Return the active tenant's markdown document (only when visible and non-empty).

    Reads tenant from X-Tenant-ID header (same pattern as bucket endpoints).
    Returns 404 when no document, not visible, or content is empty.
    """
    tenant_id = request.headers.get("X-Tenant-ID") or request.query_params.get("tenant_id")
    if not tenant_id:
        return Response(
            {"error": "X-Tenant-ID header or tenant_id param required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        membership = TenantMembership.objects.select_related(
            "tenant", "tenant__document"
        ).get(
            user=request.user,
            tenant_id=tenant_id,
            is_active=True,
            access_revoked_at__isnull=True,
        )
    except TenantMembership.DoesNotExist:
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    if membership.tenant_id not in _tenants_with_groups():
        return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        doc = membership.tenant.document
    except TenantDocument.DoesNotExist:
        return Response({"error": "No document"}, status=status.HTTP_404_NOT_FOUND)

    if not doc.is_visible or not doc.content.strip():
        return Response({"error": "No document"}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        "tab_name": doc.tab_name,
        "content": doc.content,
        "updated_at": doc.updated_at,
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    """Health check for Kubernetes probes."""
    return Response({"status": "ok"})
