"""Sync orchestration between RGWSquared and Django DB.

Main operation:
    refresh_local_cache: Pull users/buckets/permissions from RGWSquared into Django DB

The admin sync pipeline asks RGWSquared to update its structure, then refreshes
this local cache from RGWSquared's current JSON state.
"""

import logging

from django.conf import settings

from storage.models import (
    TenantMembership,
    Bucket,
    BucketPermission,
    FileUploadRecord,
    User,
)
from storage.services.rgw_squared import RGWSquaredClient
from storage.services.s3_ops import (
    get_mgmt_s3_client,
    list_objects,
    parse_rgwsquared_bucket_name,
)
from storage.access import is_rgwsquared_synced, is_write_capable

logger = logging.getLogger(__name__)


def _get_client():
    """Create RGWSquaredClient from Django settings."""
    return RGWSquaredClient(
        base_url=settings.RGWSQUARED_URL,
        username=settings.RGWSQUARED_USERNAME,
        password=settings.RGWSQUARED_PASSWORD,
    )


def refresh_local_cache(tenant, client=None):
    """Sync RGWSquared state into Django DB.

    Fetches user list and bucket list from RGWSquared, then:
    - Creates/updates TenantMembership with role
    - Creates/updates Bucket records for proposal buckets
    - Creates/updates BucketPermission records for RGWSquared auto buckets

    Returns summary dict with counts.
    """
    if not tenant.rgwsquared_structure:
        return {"error": f"Tenant {tenant.code} has no rgwsquared_structure configured"}

    if client is None:
        client = _get_client()

    structure = tenant.rgwsquared_structure
    stats = {
        "users_synced": 0,
        "users_skipped_unregistered": 0,
        "buckets_synced": 0,
        "permissions_synced": 0,
        "orphan_buckets_seen": 0,
        "orphan_permissions_cleared": 0,
        "objects_synced": 0,
        "objects_discovered": 0,
        "object_records_removed": 0,
        "object_sync_errors": 0,
    }
    synced_ceph_usernames = set()
    proposal_bucket_ids_by_name = {}

    try:
        structure_info = client.get_structure_info(structure)
    except Exception as e:
        logger.warning(f"Could not fetch structureInfo for {tenant.code}: {e}")
        structure_info = {}

    stats["initialized"] = bool(structure_info.get("initialized"))

    try:
        rgw_buckets = client.list_buckets(structure)
    except Exception as e:
        logger.warning(f"Could not list RGWSquared buckets for {tenant.code}: {e}")
        rgw_buckets = []

    for item in rgw_buckets:
        if isinstance(item, str):
            bucket_name = item
            is_auto = True
            is_manual = False
        else:
            bucket_name = item.get("name") or item.get("id")
            is_auto = bool(item.get("auto"))
            is_manual = bool(item.get("manual"))
        if not bucket_name:
            continue

        bare_name = parse_rgwsquared_bucket_name(str(bucket_name), tenant.code)
        bucket_type = Bucket.LOCAL if is_manual and not is_auto else Bucket.PROPOSAL
        bucket, created = Bucket.objects.get_or_create(
            name=bare_name,
            tenant=tenant,
            defaults={
                "bucket_type": bucket_type,
                "is_deletable": bucket_type == Bucket.LOCAL,
                "display_name": bare_name,
            },
        )
        if not created:
            update_fields = []
            if bucket.display_name != bare_name and bucket.bucket_type == Bucket.PROPOSAL:
                bucket.display_name = bare_name
                update_fields.append("display_name")
            if bucket.bucket_type == Bucket.PROPOSAL and bucket.is_deletable:
                bucket.is_deletable = False
                update_fields.append("is_deletable")
            if update_fields:
                bucket.save(update_fields=update_fields)

        if bucket.bucket_type == Bucket.PROPOSAL:
            proposal_bucket_ids_by_name[bare_name] = bucket.id
        elif isinstance(item, dict):
            cleared = _cleanup_orphan_manual_bucket(bucket)
            stats["orphan_buckets_seen"] += 1
            if cleared:
                stats["orphan_permissions_cleared"] += cleared
        stats["buckets_synced"] += 1

    rgwsquared_authoritative = is_rgwsquared_synced(tenant)
    ms_users = client.list_users(structure)
    for username in ms_users:
        synced_ceph_usernames.add(username)
        if rgwsquared_authoritative:
            user = _resolve_user_for_identity(username, tenant, structure, stats)
            membership = None
        else:
            membership = _registered_membership_for_identity(username, tenant)
            if membership is None:
                stats["users_skipped_unregistered"] += 1
                continue
            user = membership.user

        try:
            user_info = client.get_user_info(structure, username)
        except Exception as e:
            logger.warning(
                f"Could not fetch userInfo for {username} in {structure}: {e}"
            )
            stats["user_errors"] = stats.get("user_errors", 0) + 1
            continue
        if not user_info:
            logger.warning(f"Empty userInfo for {username} in {structure}")
            continue

        user_perms = {
            "rw": _bucket_ids_from_user_info(
                user_info.get("RWBuckets", []), proposal_bucket_ids_by_name, tenant
            ),
            "ro": _bucket_ids_from_user_info(
                user_info.get("ROBuckets", []), proposal_bucket_ids_by_name, tenant
            ),
        }
        role = "rw" if user_perms["rw"] else "ro"

        if rgwsquared_authoritative:
            membership, _ = TenantMembership.objects.update_or_create(
                user=user,
                tenant=tenant,
                defaults={
                    "ceph_username": username,
                    "role": role,
                    "is_active": True,
                },
            )
        else:
            update_fields = []
            if membership.ceph_username != username:
                membership.ceph_username = username
                update_fields.append("ceph_username")
            if not membership.is_active:
                membership.is_active = True
                update_fields.append("is_active")
            if update_fields:
                membership.save(update_fields=update_fields)

        # UO codes are only for write-capable users; RO users must not carry them.
        if not is_write_capable(membership.role):
            if membership.uo_code:
                membership.uo_code = ""
                membership.save(update_fields=["uo_code"])
                stats["uo_codes_cleared"] = stats.get("uo_codes_cleared", 0) + 1
                logger.info(
                    f"Cleared uo_code for read-only user {username} in {tenant.code} during sync"
                )
        elif user.institution and not membership.uo_code:
            from storage.models import UOMapping

            uo = UOMapping.objects.filter(
                tenant=tenant,
                institution_name__icontains=user.institution,
            ).first()
            if uo:
                membership.uo_code = uo.uo_code
                membership.save(update_fields=["uo_code"])
                stats["uo_codes_updated"] = stats.get("uo_codes_updated", 0) + 1
                logger.info(
                    f"Set uo_code={uo.uo_code} for {username} in {tenant.code} during sync"
                )

        stats["users_synced"] += 1
        synced_count = _sync_user_permissions(
            user, tenant, user_perms["rw"], user_perms["ro"]
        )
        stats["permissions_synced"] += synced_count

    # Only RGWSquared-authoritative tenants deactivate memberships when an
    # upstream user disappears. Authentik-managed membership truth comes from OIDC.
    if rgwsquared_authoritative:
        stale_memberships = (
            TenantMembership.objects.filter(
                tenant=tenant,
                is_active=True,
            )
            .exclude(ceph_username__in=synced_ceph_usernames)
            .exclude(ceph_username="")
        )

        deactivated = 0
        for membership in stale_memberships:
            perms_deleted, _ = BucketPermission.objects.filter(
                user=membership.user,
                bucket__tenant=tenant,
                source="rgwsquared",
            ).delete()
            membership.is_active = False
            membership.save(update_fields=["is_active"])
            logger.info(
                "Deactivated stale membership: %s in %s (%s perms removed)",
                membership.ceph_username,
                tenant.code,
                perms_deleted,
            )
            deactivated += 1

        if deactivated:
            stats["users_deactivated"] = deactivated

    _sync_authorized_bucket_objects(tenant, stats, client)
    return stats



def _registered_membership_for_identity(identity, tenant):
    """Resolve only users who already registered through a successful OIDC login."""
    username = str(identity).strip()
    if not username:
        return None
    return (
        TenantMembership.objects.filter(
            tenant=tenant,
            ceph_username=username,
            user__external_id__isnull=False,
        )
        .exclude(user__external_id__startswith="ms:")
        .select_related("user")
        .first()
    )

def _resolve_user_for_identity(identity, tenant, structure, stats):
    """Map a RGWSquared username or email to a Django User."""
    username = str(identity).strip()
    if not username:
        return None

    membership = (
        TenantMembership.objects.filter(tenant=tenant, ceph_username=username)
        .select_related("user")
        .first()
    )
    if membership:
        return membership.user

    user = User.objects.filter(username=username).first()

    if not user:
        from social_django.models import UserSocialAuth

        associations = list(
            UserSocialAuth.objects.filter(
                provider="authentik",
                extra_data__preferred_username=username,
            )
            .select_related("user")
            .order_by("id")[:2]
        )
        if len(associations) == 1:
            user = associations[0].user

    if not user and "@" in username:
        email_matches = list(User.objects.filter(email__iexact=username).order_by("id")[:2])
        if len(email_matches) == 1:
            user = email_matches[0]
        elif len(email_matches) > 1:
            logger.warning(
                "Cannot resolve RGWSquared identity %s by non-unique email in %s",
                username,
                structure,
            )

    if not user:
        email = username if "@" in username else f"{username}@placeholder.local"
        user = User.objects.create(
            username=username,
            email=email,
            external_id=f"ms:{structure}:{username}",
            is_active=True,
            is_approved=True,
        )
        logger.info("Created placeholder user for %s in %s", username, structure)
        stats["users_created"] = stats.get("users_created", 0) + 1

    return user


def _sync_authorized_bucket_objects(tenant, stats, rgwsquared_client):
    """Reconcile object metadata for buckets with trusted application access."""
    buckets = list(
        Bucket.objects.filter(tenant=tenant, permissions__isnull=False)
        .distinct()
        .order_by("id")
    )
    if not buckets:
        return

    try:
        s3 = get_mgmt_s3_client(tenant, client=rgwsquared_client)
    except Exception as exc:
        logger.warning("Could not create S3 client for %s object sync: %s", tenant.code, exc)
        stats["object_sync_errors"] += len(buckets)
        return

    for bucket in buckets:
        try:
            objects = list_objects(s3, bucket.name, raise_errors=True)
        except Exception as exc:
            logger.warning(
                "Could not inventory objects in %s/%s: %s",
                tenant.code,
                bucket.name,
                exc,
            )
            stats["object_sync_errors"] += 1
            continue

        existing = {
            record.file_key: record
            for record in FileUploadRecord.objects.filter(bucket=bucket)
        }
        seen_keys = set()
        to_create = []
        to_update = []

        for item in objects:
            key = item["key"]
            etag = item.get("etag", "")
            seen_keys.add(key)
            record = existing.get(key)
            if record is None:
                to_create.append(
                    FileUploadRecord(
                        bucket=bucket,
                        file_key=key,
                        uploaded_by=None,
                        file_size=item["size"],
                        origin=FileUploadRecord.DISCOVERED,
                        object_etag=etag,
                        object_last_modified=item["last_modified"],
                    )
                )
                stats["objects_discovered"] += 1
                continue

            changed_outside_app = bool(record.object_etag and etag) and (
                record.object_etag != etag
            )
            if changed_outside_app and record.origin == FileUploadRecord.APP:
                record.uploaded_by = None
                record.origin = FileUploadRecord.DISCOVERED
                stats["objects_discovered"] += 1
            record.file_size = item["size"]
            record.object_etag = etag
            record.object_last_modified = item["last_modified"]
            to_update.append(record)

        if to_create:
            FileUploadRecord.objects.bulk_create(to_create)
        if to_update:
            FileUploadRecord.objects.bulk_update(
                to_update,
                [
                    "uploaded_by",
                    "file_size",
                    "origin",
                    "object_etag",
                    "object_last_modified",
                ],
            )

        stale_ids = [
            record.id for key, record in existing.items() if key not in seen_keys
        ]
        if stale_ids:
            removed, _ = FileUploadRecord.objects.filter(id__in=stale_ids).delete()
            stats["object_records_removed"] += removed
        stats["objects_synced"] += len(objects)


def _cleanup_orphan_manual_bucket(bucket):
    """Keep manual buckets visible to admins only; strip mistaken user permissions."""
    has_local_owner = BucketPermission.objects.filter(
        bucket=bucket,
        permission="owner",
        source="local",
    ).exists()
    if has_local_owner:
        return 0
    deleted, _ = BucketPermission.objects.filter(bucket=bucket).delete()
    if deleted:
        logger.info(
            "Cleared %s Django permission(s) from orphan bucket %s",
            deleted,
            bucket.name,
        )
    return deleted


def _bucket_ids_from_user_info(bucket_names, bucket_ids_by_name, tenant):
    """Resolve RGWSquared userInfo bucket references to proposal bucket IDs."""
    bucket_ids = set()
    for name in bucket_names or []:
        bare_name = parse_rgwsquared_bucket_name(str(name), tenant.code)
        bucket_id = bucket_ids_by_name.get(bare_name)
        if bucket_id:
            bucket_ids.add(bucket_id)
    return bucket_ids


def _sync_user_permissions(user, tenant, rw_bucket_ids, ro_bucket_ids):
    """Update BucketPermission records for a user from RGWSquared data.

    Uses update_or_create to avoid race conditions with local sharing.
    Removes stale RGWSquared permissions that are no longer in the source.
    """
    synced_bucket_ids = set()

    for bucket_id in rw_bucket_ids:
        BucketPermission.objects.update_or_create(
            bucket_id=bucket_id,
            user=user,
            source="rgwsquared",
            defaults={"permission": "rw"},
        )
        synced_bucket_ids.add(bucket_id)

    # RW wins if RGWSquared returns the same bucket in both lists.
    for bucket_id in ro_bucket_ids:
        if bucket_id not in synced_bucket_ids:
            BucketPermission.objects.update_or_create(
                bucket_id=bucket_id,
                user=user,
                source="rgwsquared",
                defaults={"permission": "ro"},
            )
            synced_bucket_ids.add(bucket_id)

    BucketPermission.objects.filter(
        user=user,
        bucket__tenant=tenant,
        bucket__bucket_type=Bucket.PROPOSAL,
        source="rgwsquared",
    ).exclude(bucket_id__in=synced_bucket_ids).delete()
    return len(synced_bucket_ids)
