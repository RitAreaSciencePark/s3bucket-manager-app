"""URL configuration for multi-tenant Buckets Explorer."""

from django.http import HttpResponse
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from storage.views import (
    exchange_token,
    current_user,
    select_tenant,
    health_check,
    tenant_document,
    BucketViewSet,
    admin_exchange_token,
    admin_permissions,
    admin_buckets,
    admin_bucket_detail,
    admin_delete_file,
    admin_users,
    admin_tenants,
    admin_tenant_activation,
    admin_group_mappings,
    admin_group_mapping_delete,
    admin_available_tenants,
    admin_uo_mappings,
    admin_sync_refresh,
    admin_sync_upload_csv,
    admin_sync_update_structure,
    admin_create_tenant,
    admin_membership_files,
    admin_file_name_rules,
    admin_file_name_rule_detail,
    admin_file_deviations,
    admin_tenant_document,
    admin_file_formats,
)

router = DefaultRouter()
router.register(r"buckets", BucketViewSet, basename="bucket")

urlpatterns = [
    path("favicon.ico", lambda r: HttpResponse(status=204)),
    path("api/health/", health_check, name="health_check"),
    # API documentation — admin-only via SPECTACULAR_SETTINGS['SERVE_PERMISSIONS'] (is_staff).
    # Sign in at /admin/login via Authentik, then open /api/docs/ in the same browser.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # OAuth callback URLs are provided by social-auth-app-django.
    path("api/oauth/", include("social_django.urls", namespace="social")),
    path("api/auth/token/", exchange_token, name="exchange_token"),
    path("api/auth/user/", current_user, name="current_user"),
    path("api/auth/select-tenant/", select_tenant, name="select_tenant"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/auth/tenant-document/", tenant_document, name="tenant_document"),
    path("api/admin/auth/token/", admin_exchange_token, name="admin_exchange_token"),
    path("api/admin/permissions/", admin_permissions, name="admin_permissions"),
    path("api/admin/buckets/", admin_buckets, name="admin_buckets"),
    path(
        "api/admin/buckets/<int:bucket_id>/",
        admin_bucket_detail,
        name="admin_bucket_detail",
    ),
    path(
        "api/admin/buckets/<int:bucket_id>/files/<path:file_key>/",
        admin_delete_file,
        name="admin_delete_file",
    ),
    path("api/admin/users/", admin_users, name="admin_users"),
    path("api/admin/tenants/", admin_tenants, name="admin_tenants"),
    path("api/admin/tenant-activation/", admin_tenant_activation, name="admin_tenant_activation"),
    path("api/admin/tenants/create/", admin_create_tenant, name="admin_create_tenant"),
    path(
        "api/admin/group-mappings/", admin_group_mappings, name="admin_group_mappings"
    ),
    path(
        "api/admin/group-mappings/<int:mapping_id>/",
        admin_group_mapping_delete,
        name="admin_group_mapping_delete",
    ),
    path(
        "api/admin/available-tenants/",
        admin_available_tenants,
        name="admin_available_tenants",
    ),
    path("api/admin/uo-mappings/", admin_uo_mappings, name="admin_uo_mappings"),
    path("api/admin/sync/refresh/", admin_sync_refresh, name="admin_sync_refresh"),
    path(
        "api/admin/sync/upload-csv/",
        admin_sync_upload_csv,
        name="admin_sync_upload_csv",
    ),
    path(
        "api/admin/sync/update-structure/",
        admin_sync_update_structure,
        name="admin_sync_update_structure",
    ),
    path("api/admin/memberships/<int:membership_id>/files/", admin_membership_files, name="admin_membership_files"),
    path("api/admin/file-name-rules/", admin_file_name_rules, name="admin_file_name_rules"),
    path(
        "api/admin/file-name-rules/<int:rule_id>/",
        admin_file_name_rule_detail,
        name="admin_file_name_rule_detail",
    ),
    path("api/admin/file-deviations/", admin_file_deviations, name="admin_file_deviations"),
    path(
        "api/admin/tenant-documents/<str:tenant_code>/",
        admin_tenant_document,
        name="admin_tenant_document",
    ),
    path("api/admin/file-formats/", admin_file_formats, name="admin_file_formats"),
    path("api/", include(router.urls)),
]
