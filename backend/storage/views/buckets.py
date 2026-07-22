"""Bucket CRUD + file operations — tenant-scoped, permission-aware."""

import io
import logging
import re
import zipfile

from django.http import HttpResponse
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from botocore.exceptions import ClientError

from storage.access import membership_has_access
from storage.models import (
    Bucket,
    BucketPermission,
    TenantMembership,
    FileUploadRecord,
)
from storage.serializers import (
    BucketSerializer,
    BucketCreateSerializer,
    FileSerializer,
    FileUploadSerializer,
)
from storage.services import permissions as perms
from storage.services.s3_ops import (
    ensure_structure_initialized,
    list_objects,
    upload_object,
    delete_object,
    download_object,
    get_mgmt_s3_client,
    get_bucket_stats_for_tenant,
)
from storage.services.rgw_squared import (
    RGWSquaredClient,
    RGWSquaredError,
    delete_bucket_via_rgw,
)

logger = logging.getLogger(__name__)

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$")


def _get_membership(request):
    """Extract active tenant membership from request header or query param."""
    tenant_id = request.headers.get("X-Tenant-ID") or request.query_params.get(
        "tenant_id"
    )
    if not tenant_id:
        return None
    try:
        membership = TenantMembership.objects.select_related("user", "tenant").get(
            user=request.user,
            tenant_id=tenant_id,
            is_active=True,
            access_revoked_at__isnull=True,
        )
    except TenantMembership.DoesNotExist:
        return None
    return membership if membership_has_access(membership) else None


def _get_rgw_client():
    from django.conf import settings

    return RGWSquaredClient(
        settings.RGWSQUARED_URL,
        settings.RGWSQUARED_USERNAME,
        settings.RGWSQUARED_PASSWORD,
    )


def _structure_name(tenant):
    return tenant.rgwsquared_structure or tenant.code


def _error_response(message, response_status=status.HTTP_500_INTERNAL_SERVER_ERROR):
    return Response({"error": str(message)}, status=response_status)


def _looks_like_duplicate_bucket_error(message, bucket_name):
    text = str(message).lower()
    return (
        "duplicate" in text
        or "already exist" in text
        or ("exist" in text and ("bucket" in text or bucket_name.lower() in text))
    )


def _apply_filename_policy(bucket, requesting_user, original_key):
    """Rename uploaded file keys according to the tenant/bucket-type policy.

    Policy:
      NFFADI + proposal: {tenant}-{display_name}-{uo_of_uploader}-{filename}
      NFFADI + local:    {tenant}-{uo_of_uploader}-{display_name}-{filename}
      other  + proposal: {tenant}-{display_name}-{filename}
      other  + local:    {tenant}-{display_name}-{filename}

    - {tenant} = tenant code lowercased
    - {display_name} = bucket.display_name (proposal ID or project name)
    - {uo_of_uploader} = uploading user's TenantMembership.uo_code (NFFADI only)
      For local NFFADI buckets: the uploader's UO, not the owner's — since all RW
      users have UO codes and shared uploads need per-uploader file governance.
      uo_code dots translated to dashes: cnr-iom.ts → cnr-iom-ts
    - {filename} = original file key (last path component if nested)
    If uo_code is empty, the {uo} segment is omitted.
    """
    tenant_slug = re.sub(r"[^a-z0-9-]", "-", bucket.tenant.code.lower()).strip("-")

    # Sanitize the original filename: preserve extension, replace non-safe chars with dashes.
    # This prevents S3 key issues from spaces, accented characters, parentheses, etc.
    raw_filename = original_key.split("/")[-1] if "/" in original_key else original_key
    if "." in raw_filename:
        raw_base, raw_ext = raw_filename.rsplit(".", 1)
        safe_base = re.sub(r"[^a-z0-9._-]", "-", raw_base.lower())
        safe_base = re.sub(r"-{2,}", "-", safe_base).strip("-") or "file"
        safe_ext = re.sub(r"[^a-z0-9]", "", raw_ext.lower())
        filename = f"{safe_base}.{safe_ext}" if safe_ext else safe_base
    else:
        safe_base = re.sub(r"[^a-z0-9._-]", "-", raw_filename.lower())
        filename = re.sub(r"-{2,}", "-", safe_base).strip("-") or "file"

    display = re.sub(r"[^a-z0-9-]", "-", (bucket.display_name or bucket.name).lower()).strip("-")

    uo = ""
    if bucket.tenant.code == "NFFADI":
        # Always use the uploader's UO code for NFFADI (proposal and local).
        # Dots in uo_code (e.g. cnr-iom.ts) translate to dashes for valid filenames.
        m = TenantMembership.objects.filter(
            user=requesting_user,
            tenant=bucket.tenant,
            is_active=True,
            access_revoked_at__isnull=True,
        ).first()
        raw_uo = (m.uo_code or "").strip() if m else ""
        uo = raw_uo.replace(".", "-")  # cnr-iom.ts → cnr-iom-ts

    if uo:
        if bucket.bucket_type == Bucket.PROPOSAL:
            new_key = f"{tenant_slug}-{display}-{uo}-{filename}"
        else:
            new_key = f"{tenant_slug}-{uo}-{display}-{filename}"
    else:
        new_key = f"{tenant_slug}-{display}-{filename}"

    # Sanitize: collapse multiple dashes, strip leading/trailing dashes (keep filename ext intact)
    base, _, ext = new_key.rpartition(".")
    if ext:
        base = re.sub(r"-{2,}", "-", base).strip("-")
        new_key = f"{base}.{ext}"
    else:
        new_key = re.sub(r"-{2,}", "-", new_key).strip("-")

    return new_key


def _block_if_storage_uninitialized(tenant):
    try:
        ensure_structure_initialized(tenant)
    except RGWSquaredError as e:
        return _error_response(e, status.HTTP_400_BAD_REQUEST)
    except RuntimeError as e:
        return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
    except Exception as e:
        return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
    return None


def _ensure_rgw_user(client, tenant, ceph_username):
    if not ceph_username:
        raise RuntimeError("Cannot determine RGWSquared username")
    structure = _structure_name(tenant)
    users = client.list_users(structure)
    if ceph_username not in users:
        client.create_user(structure, ceph_username)


def _sync_local_bucket_permissions(bucket, desired_permissions):
    """Push local bucket permission state to RGWSquared before DB mutation."""
    client = _get_rgw_client()
    ensure_structure_initialized(bucket.tenant, client=client)

    rw_permissions = set()
    ro_permissions = set()
    for user_id, permission in desired_permissions.items():
        membership = (
            TenantMembership.objects.filter(
                tenant=bucket.tenant,
                user_id=user_id,
                is_active=True,
                access_revoked_at__isnull=True,
            )
            .select_related("user")
            .first()
        )
        if not membership:
            continue
        _ensure_rgw_user(client, bucket.tenant, membership.ceph_username)
        if permission in ("owner", "rw"):
            rw_permissions.add(membership.ceph_username)
        elif permission == "ro":
            ro_permissions.add(membership.ceph_username)

    client.update_bucket(
        _structure_name(bucket.tenant),
        bucket.name,
        rw_permissions=sorted(rw_permissions),
        ro_permissions=sorted(ro_permissions),
    )


def _current_local_permission_map(bucket):
    return {
        p.user_id: p.permission
        for p in BucketPermission.objects.filter(bucket=bucket, source="local")
    }


TENANT_ID_PARAM = OpenApiParameter(
    "X-Tenant-ID",
    location=OpenApiParameter.HEADER,
    required=True,
    description="Active tenant ID. Obtain from the `tenants` array in `GET /api/auth/token/` then confirm with `POST /api/auth/select-tenant/`.",
)


@extend_schema(tags=["Buckets"])
class BucketViewSet(viewsets.ViewSet):
    """Tenant-scoped bucket operations.

    All requests must include X-Tenant-ID header or tenant_id query param.

    list:    GET  /api/buckets/?tenant_id=X  — user's accessible buckets in tenant
    create:  POST /api/buckets/              — create local research bucket
    retrieve:GET  /api/buckets/{id}/         — bucket detail + files
    destroy: DELETE /api/buckets/{id}/       — delete bucket (owner only, local only)
    upload:  POST /api/buckets/{id}/upload/  — upload file
    delete_file: DELETE /api/buckets/{id}/files/{key}/ — delete file
    download: GET /api/buckets/{id}/download/{key}/    — download file
    shares:  GET/POST/DELETE /api/buckets/{id}/shares/ — manage sharing
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List accessible buckets",
        description="Returns all buckets (proposal + local) where the user has a BucketPermission record, with S3 storage stats.",
        parameters=[TENANT_ID_PARAM],
        responses={200: OpenApiResponse(description="List of buckets with storage stats"), 400: OpenApiResponse(description="Missing X-Tenant-ID")},
    )
    def list(self, request):
        """List buckets the user can access in the active tenant."""
        membership = _get_membership(request)
        if not membership:
            return Response(
                {"error": "X-Tenant-ID header or tenant_id param required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant = membership.tenant
        blocked = _block_if_storage_uninitialized(tenant)
        if blocked:
            return blocked

        permitted_bucket_ids = BucketPermission.objects.filter(
            user=request.user,
            bucket__tenant=tenant,
        ).values_list("bucket_id", flat=True)

        buckets = Bucket.objects.filter(id__in=permitted_bucket_ids).select_related(
            "tenant", "owner"
        )

        bucket_stats = get_bucket_stats_for_tenant(
            tenant, list(buckets.values_list("name", flat=True))
        )

        serializer = BucketSerializer(
            buckets,
            many=True,
            context={"user": request.user, "bucket_stats": bucket_stats},
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Create a local research bucket",
        description=(
            "Creates a user-owned LOCAL bucket in RGWSquared and the local database. "
            "The internal S3 bucket name is derived from `{ceph_username}-{uo_code}-{project_id}` "
            "(NFFADI) or `{ceph_username}-{project_id}` (other tenants). "
            "The user becomes the `owner` and gets full permission immediately."
        ),
        parameters=[TENANT_ID_PARAM],
        responses={
            201: OpenApiResponse(description="Created bucket"),
            400: OpenApiResponse(description="Invalid project ID / missing UO code / name conflict"),
            403: OpenApiResponse(description="Read-only user cannot create buckets"),
        },
    )
    def create(self, request):
        """Create a local research bucket.

        Naming convention:
        - Internal name (S3): {ceph_username}_{uo}_{project_id} for NFFADI,
          {ceph_username}_{project_id} for others
        - Display name (UI): just project_id
        """
        membership = _get_membership(request)
        if not membership:
            return Response(
                {"error": "X-Tenant-ID header or tenant_id param required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not perms.can_create_bucket(membership):
            return Response(
                {"error": "Read-only users cannot create buckets"},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant = membership.tenant

        serializer = BucketCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        project_id = serializer.validated_data["name"]
        if not PROJECT_ID_RE.match(project_id):
            return Response(
                {
                    "error": "Project ID must be 2-50 chars: lowercase letters, numbers, hyphens. Cannot start/end with hyphen."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # NFFADI bucket names include the operational unit to preserve provenance.
        if tenant.code == "NFFADI" and not membership.uo_code:
            return Response(
                {
                    "error": "UO code not set for your account. Contact your administrator to set it in the admin panel."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # S3 bucket names are immutable, so derive them from stable display identity.
        raw_prefix = request.user.display_username or membership.ceph_username
        if not raw_prefix:
            return Response(
                {
                    "error": "Cannot determine username for bucket naming. Contact administrator."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        ceph_user = re.sub(r"[^a-z0-9-]", "-", raw_prefix.lower())
        ceph_user = re.sub(r"-{2,}", "-", ceph_user).strip("-")
        if membership.uo_code:
            uo_safe = re.sub(r"[^a-z0-9-]", "-", membership.uo_code.lower())
            uo_safe = re.sub(r"-{2,}", "-", uo_safe).strip("-")
            bare_name = f"{ceph_user}-{uo_safe}-{project_id}"
        else:
            bare_name = f"{ceph_user}-{project_id}"

        if len(bare_name) > 255:
            return Response(
                {"error": "Generated bucket name too long"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", bare_name)
            or len(bare_name) < 3
        ):
            return Response(
                {"error": f'Generated bucket name "{bare_name}" is invalid for S3'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if Bucket.objects.filter(name=bare_name, tenant=tenant).exists():
            return Response(
                {"error": f"Bucket '{project_id}' already exists"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = _get_rgw_client()
        try:
            ensure_structure_initialized(tenant, client=client)
            _ensure_rgw_user(client, tenant, membership.ceph_username)
            client.create_bucket(
                _structure_name(tenant),
                bare_name,
                rw_permissions=[membership.ceph_username],
                ro_permissions=[],
            )
        except RGWSquaredError as e:
            if _looks_like_duplicate_bucket_error(e, bare_name):
                return _error_response(
                    f"Bucket '{project_id}' already exists",
                    status.HTTP_400_BAD_REQUEST,
                )
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            with transaction.atomic():
                bucket = Bucket.objects.create(
                    name=bare_name,
                    tenant=tenant,
                    owner=request.user,
                    bucket_type=Bucket.LOCAL,
                    is_deletable=True,
                    description=serializer.validated_data.get("description", ""),
                    display_name=project_id,
                )
                BucketPermission.objects.create(
                    bucket=bucket,
                    user=request.user,
                    permission="owner",
                    source="local",
                )
        except Exception:
            # Keep RGWSquared and DB in sync if local metadata creation fails.
            try:
                client.delete_bucket(_structure_name(tenant), bare_name)
            except Exception:
                logger.error(
                    f"Orphan RGWSquared bucket: {bare_name} (manual cleanup needed)"
                )
            return Response(
                {"error": "Failed to create bucket record"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            BucketSerializer(bucket, context={"user": request.user}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Bucket detail with file listing",
        description="Returns bucket metadata plus the list of all objects in the S3 bucket. Each file includes `uploaded_by` from FileUploadRecord where available.",
        responses={200: OpenApiResponse(description="Bucket + files"), 403: OpenApiResponse(description="No permission"), 404: OpenApiResponse(description="Bucket not found")},
    )
    def retrieve(self, request, pk=None):
        """Bucket detail with file listing."""
        try:
            bucket = Bucket.objects.select_related("tenant", "owner").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_view_bucket(request.user, bucket):
            return Response(
                {"error": "Access denied"}, status=status.HTTP_403_FORBIDDEN
            )

        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked

        files = []
        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            files = list_objects(s3, bucket.name)
        except Exception as e:
            logger.warning(f"Could not list objects in {bucket.name}: {e}")

        # Upload records preserve per-file ownership in shared local buckets.
        upload_records = {
            r.file_key: r.uploaded_by.display_name if r.uploaded_by else None
            for r in FileUploadRecord.objects.filter(bucket=bucket).select_related(
                "uploaded_by"
            )
        }
        for f in files:
            f["uploaded_by"] = upload_records.get(f["key"])

        bucket_data = BucketSerializer(bucket, context={"user": request.user}).data
        bucket_data["files"] = FileSerializer(files, many=True).data

        return Response(bucket_data)

    @extend_schema(
        summary="Delete a local research bucket (owner only)",
        description="Deletes the bucket from RGWSquared and all local database records. Only applies to LOCAL buckets (`is_deletable=True`). Proposal buckets (from RGWSquared sync) cannot be deleted.",
        responses={204: None, 403: OpenApiResponse(description="Not owner / proposal bucket"), 404: OpenApiResponse(description="Not found")},
    )
    def destroy(self, request, pk=None):
        """Delete a local research bucket (owner only)."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_delete_bucket(request.user, bucket):
            if not bucket.is_deletable:
                return Response(
                    {"error": "Proposal buckets cannot be deleted"},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(
                {"error": "Only the owner can delete this bucket"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            client = _get_rgw_client()
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

    @action(detail=True, methods=["post"])
    def upload(self, request, pk=None):
        """Upload file to bucket."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_upload_file(request.user, bucket):
            return Response(
                {"error": "No write permission"}, status=status.HTTP_403_FORBIDDEN
            )

        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked

        serializer = FileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        file_obj = serializer.validated_data["file"]
        raw_key = serializer.validated_data.get("key", file_obj.name)

        # Apply filename policy: rename key according to tenant/bucket-type rules.
        file_key = _apply_filename_policy(bucket, request.user, raw_key)

        # Read once; uploaded file streams are consumed after .read().
        file_bytes = file_obj.read()
        content_type = file_obj.content_type or "application/octet-stream"

        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            upload_result = upload_object(
                s3, bucket.name, file_key, file_bytes, content_type=content_type
            )
        except Exception as e:
            logger.error(f"Upload failed for {bucket.name}/{file_key}: {e}")
            return Response(
                {"error": f"Upload failed: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        FileUploadRecord.objects.update_or_create(
            bucket=bucket,
            file_key=file_key,
            defaults={
                "uploaded_by": request.user,
                "file_size": len(file_bytes),
                "origin": FileUploadRecord.APP,
                "object_etag": str(upload_result.get("ETag", "")).strip('"'),
                "object_last_modified": timezone.now(),
            },
        )

        return Response(
            {
                "message": f"File '{file_key}' uploaded",
                "key": file_key,
                "original_key": raw_key,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["delete"], url_path="files/(?P<file_key>.+)")
    def delete_file(self, request, pk=None, file_key=None):
        """Delete file from bucket."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_delete_file(request.user, bucket, file_key):
            return Response(
                {"error": "Cannot delete this file (not owner or not your upload)"},
                status=status.HTTP_403_FORBIDDEN,
            )

        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked

        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            delete_object(s3, bucket.name, file_key)
        except Exception as e:
            logger.error(f"Delete failed for {bucket.name}/{file_key}: {e}")
            return Response(
                {"error": f"Delete failed: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        FileUploadRecord.objects.filter(bucket=bucket, file_key=file_key).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="download/(?P<file_key>.+)")
    def download(self, request, pk=None, file_key=None):
        """Download file from bucket."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_download_file(request.user, bucket):
            return Response(
                {"error": "Access denied"}, status=status.HTTP_403_FORBIDDEN
            )

        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked

        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            body, content_type = download_object(s3, bucket.name, file_key)
        except Exception as e:
            logger.error(f"Download failed for {bucket.name}/{file_key}: {e}")
            return Response(
                {"error": f"Download failed: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        safe_filename = (
            file_key.split("/")[-1]
            .replace('"', "_")
            .replace("\n", "_")
            .replace("\r", "_")
            or "download"
        )
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{safe_filename}"'
        response["Content-Length"] = len(body)
        return response

    @action(detail=True, methods=["get"], url_path="access-list")
    def access_list(self, request, pk=None):
        """Who has access to this bucket — visible to any user with permission."""
        try:
            bucket = Bucket.objects.select_related("tenant", "owner").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_view_bucket(request.user, bucket):
            return Response(
                {"error": "Access denied"}, status=status.HTTP_403_FORBIDDEN
            )

        permissions = (
            BucketPermission.objects.filter(
                bucket=bucket,
            )
            .select_related("user")
            .order_by("-permission", "user__username")
        )

        # Placeholder users may not have a useful display name until first login.
        ceph_map = {}
        if bucket.tenant_id:
            ceph_map = {
                m["user_id"]: m["ceph_username"]
                for m in TenantMembership.objects.filter(
                    tenant_id=bucket.tenant_id,
                    is_active=True,
                    access_revoked_at__isnull=True,
                ).values("user_id", "ceph_username")
            }

        def best_display(user):
            return user.display_username or ceph_map.get(user.id) or user.display_name

        # Proposal buckets are tenant-owned; local buckets are user-owned.
        if bucket.bucket_type == "local" and bucket.owner:
            owner_label = best_display(bucket.owner)
        else:
            owner_label = bucket.tenant.code if bucket.tenant else None

        user_perm = perms.get_user_permission(request.user, bucket)

        return Response(
            {
                "owner_label": owner_label,
                "is_owner": user_perm == "owner",
                "access": [
                    {
                        "user_id": p.user.id,
                        "display_name": best_display(p.user),
                        "email": ""
                        if p.user.email.endswith("@placeholder.local")
                        else p.user.email,
                        "permission": p.permission,
                        "source": p.source,
                    }
                    for p in permissions
                ],
            }
        )

    @action(detail=True, methods=["get", "post", "delete"], url_path="shares")
    def shares(self, request, pk=None):
        """Manage bucket sharing (local research buckets only)."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        # Proposal permissions come from RGWSquared and must not be modified locally.
        if bucket.bucket_type == "proposal":
            return Response(
                {"error": "Proposal buckets cannot be shared"},
                status=status.HTTP_403_FORBIDDEN,
            )

        user_perm = perms.get_user_permission(request.user, bucket)
        if user_perm != "owner":
            return Response(
                {"error": "Only bucket owner can manage shares"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Placeholder users may not have a useful display name until first login.
        ceph_map = {}
        if bucket.tenant_id:
            ceph_map = {
                m["user_id"]: m["ceph_username"]
                for m in TenantMembership.objects.filter(
                    tenant_id=bucket.tenant_id,
                    is_active=True,
                    access_revoked_at__isnull=True,
                ).values("user_id", "ceph_username")
            }

        def best_display(user):
            return user.display_username or ceph_map.get(user.id) or user.display_name

        if request.method == "GET":
            shares = (
                BucketPermission.objects.filter(
                    bucket=bucket,
                    source="local",
                )
                .select_related("user")
                .exclude(user=request.user)
            )
            return Response(
                [
                    {
                        "id": s.id,
                        "user_id": s.user.id,
                        "username": s.user.username,
                        "display_name": best_display(s.user),
                        "email": ""
                        if s.user.email.endswith("@placeholder.local")
                        else s.user.email,
                        "permission": s.permission,
                        "granted_at": s.granted_at,
                    }
                    for s in shares
                ]
            )

        if request.method == "POST":
            from storage.models import User

            identifier = request.data.get("username", "").strip()
            permission = request.data.get("permission", "ro")
            if permission not in ("ro", "rw"):
                return Response(
                    {"error": "Permission must be ro or rw"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not identifier:
                return Response(
                    {"error": "Username or email is required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Accept multiple identifier formats in order of most-natural to least:
            # 1. Full email (name.surname@example.com)
            # 2. Display username — email local-part set at first login (name.surname)
            # 3. Authentik username (internal; rarely typed by users, kept for compat)
            # 4. RGWSquared ceph_username (for instrument-scientist accounts)
            target_user = None
            if "@" in identifier:
                email_matches = list(
                    User.objects.filter(
                        email__iexact=identifier,
                        memberships__tenant=bucket.tenant,
                        memberships__is_active=True,
                        memberships__access_revoked_at__isnull=True,
                    )
                    .distinct()
                    .order_by("id")[:2]
                )
                if len(email_matches) > 1:
                    return Response(
                        {
                            "error": (
                                f'More than one tenant account uses "{identifier}". '
                                "Use the account's unique display username or Ceph username."
                            )
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                if email_matches:
                    target_user = email_matches[0]
            if not target_user:
                # display_username is the email local-part (e.g. "name.surname" from
                # "name.surname@example.com"), unique per user, collision-safe.
                target_user = User.objects.filter(display_username__iexact=identifier).first()
            if not target_user:
                target_user = User.objects.filter(username=identifier).first()
            if not target_user:
                membership = (
                    TenantMembership.objects.filter(
                        ceph_username=identifier,
                        tenant=bucket.tenant,
                        is_active=True,
                        access_revoked_at__isnull=True,
                    )
                    .select_related("user")
                    .first()
                )
                if membership:
                    target_user = membership.user
            if not target_user:
                return Response(
                    {"error": f'User "{identifier}" not found'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if target_user == request.user:
                return Response(
                    {"error": "Cannot share with yourself"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Sharing is tenant-local; cross-tenant grants would bypass isolation.
            target_membership = TenantMembership.objects.filter(
                user=target_user,
                tenant=bucket.tenant,
                is_active=True,
                access_revoked_at__isnull=True,
            ).first()
            if not target_membership:
                return Response(
                    {"error": f'User "{identifier}" is not a member of this tenant'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Governance: file naming requires a UO code from the uploader.
            # RW permission may only be granted to users with RW role (they have UO codes).
            if permission == "rw" and target_membership.role != "rw":
                return Response(
                    {"error": f'"{identifier}" has read-only access and cannot be granted write permission. Only RW users may receive write permissions.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            desired = _current_local_permission_map(bucket)
            desired[target_user.id] = permission
            try:
                _sync_local_bucket_permissions(bucket, desired)
            except RGWSquaredError as e:
                return _error_response(e, status.HTTP_400_BAD_REQUEST)
            except RuntimeError as e:
                return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
            except Exception as e:
                return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)

            share, created = BucketPermission.objects.update_or_create(
                bucket=bucket,
                user=target_user,
                defaults={"permission": permission, "source": "local"},
            )
            return Response(
                {
                    "id": share.id,
                    "username": target_user.username,
                    "display_name": best_display(target_user),
                    "permission": share.permission,
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )

        if request.method == "DELETE":
            share_id = request.data.get("share_id")
            share = (
                BucketPermission.objects.filter(
                    id=share_id,
                    bucket=bucket,
                    source="local",
                )
                .exclude(user=request.user)
                .first()
            )
            if not share:
                return Response(
                    {"error": "Share not found"}, status=status.HTTP_404_NOT_FOUND
                )
            desired = _current_local_permission_map(bucket)
            desired.pop(share.user_id, None)
            try:
                _sync_local_bucket_permissions(bucket, desired)
            except RGWSquaredError as e:
                return _error_response(e, status.HTTP_400_BAD_REQUEST)
            except RuntimeError as e:
                return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
            except Exception as e:
                return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
            deleted, _ = (
                BucketPermission.objects.filter(
                    id=share_id,
                    bucket=bucket,
                    source="local",
                )
                .exclude(user=request.user)
                .delete()
            )
            if not deleted:
                return Response(
                    {"error": "Share not found"}, status=status.HTTP_404_NOT_FOUND
                )
            return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["delete"], url_path="leave")
    def leave(self, request, pk=None):
        """Allow a non-owner recipient to leave a shared local bucket."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )
        if bucket.owner == request.user:
            return Response(
                {"error": "Owner cannot leave their own bucket"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        desired = _current_local_permission_map(bucket)
        desired.pop(request.user.id, None)
        try:
            _sync_local_bucket_permissions(bucket, desired)
        except RGWSquaredError as e:
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        except RuntimeError as e:
            return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as e:
            return _error_response(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
        deleted, _ = BucketPermission.objects.filter(
            bucket=bucket, user=request.user, source="local"
        ).delete()
        if not deleted:
            return Response(
                {"error": "You do not have access to this bucket"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="download-archive")
    def download_archive(self, request, pk=None):
        """Download all bucket files as a zip archive."""
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )

        if not perms.can_download_file(request.user, bucket):
            return Response(
                {"error": "Access denied"}, status=status.HTTP_403_FORBIDDEN
            )

        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked

        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            files = list_objects(s3, bucket.name)
        except Exception as e:
            logger.error(f"Archive listing failed for {bucket.name}: {e}")
            return Response(
                {"error": f"Archive listing failed: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not files:
            return Response(
                {"error": "Bucket is empty"},
                status=status.HTTP_404_NOT_FOUND,
            )

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in files:
                key = entry["key"]
                try:
                    body, _ = download_object(s3, bucket.name, key)
                except Exception as e:
                    logger.error(f"Archive download failed for {bucket.name}/{key}: {e}")
                    return Response(
                        {"error": f"Archive download failed for {key}: {e}"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
                zf.writestr(key, body)

        archive.seek(0)
        safe_name = (bucket.display_name or bucket.name).replace('"', "_")
        response = HttpResponse(archive.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.zip"'
        response["Content-Length"] = len(response.content)
        return response

    @action(detail=True, methods=["get"], url_path="nexus-detect/(?P<file_key>.+)")
    def nexus_detect(self, request, pk=None, file_key=None):
        """Detect if a file is a valid NeXus/HDF5 file by checking magic bytes.

        Uses a Range request to read only the first 8 bytes — no h5py needed.
        URL uses nexus-detect/ prefix to avoid conflict with files/ delete action.
        """
        try:
            bucket = Bucket.objects.select_related("tenant").get(id=pk)
        except Bucket.DoesNotExist:
            return Response(
                {"error": "Bucket not found"}, status=status.HTTP_404_NOT_FOUND
            )
        if not perms.can_download_file(request.user, bucket):
            return Response(
                {"error": "Access denied"}, status=status.HTTP_403_FORBIDDEN
            )
        blocked = _block_if_storage_uninitialized(bucket.tenant)
        if blocked:
            return blocked
        try:
            s3 = get_mgmt_s3_client(bucket.tenant)
            obj = s3.get_object(Bucket=bucket.name, Key=file_key, Range="bytes=0-7")
            header = obj["Body"].read()
            meta = s3.head_object(Bucket=bucket.name, Key=file_key)
            size = meta["ContentLength"]
        except ClientError:
            return Response(
                {"error": "File not found"}, status=status.HTTP_404_NOT_FOUND
            )
        HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
        filename = file_key.split("/")[-1] or file_key
        return Response(
            {"is_nexus": header == HDF5_MAGIC, "size": size, "filename": filename}
        )
