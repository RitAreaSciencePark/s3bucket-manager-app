"""Custom social-auth pipeline for federated user provisioning."""

import logging
from django.db import IntegrityError
from django.utils import timezone
from social_core.exceptions import AuthForbidden

from storage.access import (
    is_nffadi_tenant,
    is_rgwsquared_synced,
    is_valid_nffadi_mapping,
    is_write_capable,
    structure_name,
)

logger = logging.getLogger(__name__)


def validate_required_claims(backend, details, response, *args, **kwargs):
    """Require stable federation keys before provisioning a local account."""
    sub = response.get("sub")
    if not sub:
        logger.error("OAuth2 response missing 'sub' claim. Response: %s", response)
        raise AuthForbidden(
            backend,
            "Identity provider did not provide user ID ('sub' claim). "
            "Please check your OAuth2 provider configuration.",
        )

    email = response.get("email") or details.get("email")
    if not email:
        from storage.middleware import AuthMissingEmailClaim
        preferred_username = response.get("preferred_username", "<unknown>")
        logger.error(
            "Email claim missing or empty for Authentik user '%s' (sub=%s). "
            "Admin must set the email address field in their Authentik user profile.",
            preferred_username,
            sub,
        )
        raise AuthMissingEmailClaim(
            backend,
            f"User '{preferred_username}' has no email configured in Authentik. "
            "Admin must set the email field in their Authentik user profile.",
        )

    details["email"] = email
    logger.info("OAuth2 claims validated. sub=%s, email=%s", sub, email)
    return {"details": details}


def extract_external_id(backend, details, response, user=None, *args, **kwargs):
    """Store OAuth2 subject as the stable local federation key."""
    external_id = response.get("sub")
    idp_source = backend.name

    logger.info(f"Extracted external_id={external_id}, idp_source={idp_source}")

    details["external_id"] = external_id
    details["idp_source"] = idp_source

    return {"details": details}


def extract_federation_fields(backend, details, response, user=None, *args, **kwargs):
    """Normalize optional identity claims from Authentik/federated sources."""
    institution = (
        response.get("institution")
        or response.get("organization")
        or response.get("o")  # short SAML organization claim
        or ""
    )

    department = response.get("department") or response.get("dept") or ""

    affiliation_raw = (
        response.get("affiliation") or response.get("eduPersonAffiliation") or ""
    )

    affiliation_mapping = {
        "faculty": "faculty",
        "staff": "staff",
        "student": "student",
        "affiliate": "affiliate",
        "member": "affiliate",
        "alum": "affiliate",
        "employee": "staff",
    }
    affiliation_status = affiliation_mapping.get(affiliation_raw.lower(), "")

    # ORCID is unique; use NULL so multiple users can omit it safely.
    orcid = response.get("orcid") or None

    profile_picture_url = response.get("picture") or ""

    details.update(
        {
            "institution": institution,
            "department": department,
            "affiliation_status": affiliation_status,
            "orcid": orcid,
            "profile_picture_url": profile_picture_url,
        }
    )

    logger.info(
        f"Extracted federation fields: institution={institution}, "
        f"department={department}, affiliation={affiliation_status}"
    )

    return {"details": details}


def associate_by_ceph_username(backend, details, response, user=None, *args, **kwargs):
    """Match login to sync-created user via preferred_username == ceph_username.

    When RGWSquared sync creates a placeholder user, it stores the upstream
    username as ceph_username in TenantMembership. Authentik sends the same value
    as preferred_username. This step bridges the two so the pipeline updates the
    existing user instead of creating a duplicate.
    """
    if user:
        return {"user": user}

    preferred_username = response.get("preferred_username")
    if not preferred_username:
        return None

    from storage.models import TenantMembership

    membership = (
        TenantMembership.objects.filter(
            ceph_username=preferred_username,
            is_active=True,
            tenant__access_model="rgwsquared_synced",
            user__external_id__startswith="ms:",
        )
        .select_related("user")
        .first()
    )

    if membership:
        logger.info(
            f"Matched sync user by ceph_username={preferred_username}: "
            f"user={membership.user.username} (id={membership.user.id})"
        )
        return {"user": membership.user, "is_new": False}

    return None


def generate_username_with_fallback(
    backend, details, response, user=None, *args, **kwargs
):
    """Generate a stable local username from OIDC claims without collisions."""
    from django.contrib.auth import get_user_model

    User = get_user_model()

    if user:
        return None

    username = response.get("preferred_username") or response.get("username")

    if not username:
        email = details.get("email", "")
        if email and "@" in email:
            username = email.split("@")[0]

    if not username:
        external_id = details.get("external_id", "")
        username = external_id[:150] if external_id else "user"

    username = username.replace(" ", "_").replace(".", "_").lower()

    original_username = username
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{original_username}_{counter}"
        counter += 1

        if counter > 1000:
            import uuid

            username = f"user_{uuid.uuid4().hex[:8]}"
            break

    details["username"] = username
    logger.info(f"Generated username: {username}")

    return {"details": details}


def create_or_update_user(backend, details, response, user=None, *args, **kwargs):
    """Fill federation fields after social-auth creates or finds the user."""
    if not user:
        logger.warning(
            "create_or_update_user called but user is None - pipeline issue?"
        )
        return None

    external_id = details.get("external_id")
    idp_source = details.get("idp_source", "authentik")
    institution = details.get("institution", "")
    department = details.get("department", "")
    affiliation_status = details.get("affiliation_status", "")
    orcid = details.get("orcid", "")
    profile_picture_url = details.get("profile_picture_url", "")

    # Preserve curated profile fields; only fill blanks from the identity provider.
    given_name = response.get("given_name", "")
    family_name = response.get("family_name", "")
    if not user.first_name and given_name:
        user.first_name = given_name
    if not user.last_name and family_name:
        user.last_name = family_name
    if not user.first_name and not user.last_name:
        name = response.get("name", "")
        if name:
            parts = name.split(" ", 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ""

    # A secondary social association must not rewrite the account's primary
    # OIDC subject. Sync-created placeholders are claimed by the first real
    # Authentik account that logs in with their Ceph username.
    if not user.external_id or user.external_id.startswith("ms:"):
        user.external_id = external_id
    user.idp_source = idp_source

    if not user.institution and institution:
        user.institution = institution
    if not user.department and department:
        user.department = department
    if not user.affiliation_status and affiliation_status:
        user.affiliation_status = affiliation_status
    if not user.profile_picture_url and profile_picture_url:
        user.profile_picture_url = profile_picture_url

    if not user.orcid and orcid:
        user.orcid = orcid
    elif not user.orcid and orcid is None:
        user.orcid = None

    # Placeholder users from RGWSquared sync get their real OAuth email on first login.
    new_email = details.get("email")
    if new_email and (not user.email or user.email.endswith("@placeholder.local")):
        try:
            user.email = new_email
            user.save()
            logger.info(f"Updated email for user {user.username}: {new_email}")
        except IntegrityError as e:
            logger.warning(f"Cannot set email for {user.username} to {new_email}: {e}")
            user.refresh_from_db()
    else:
        user.save()

    # Display usernames stay stable after first login even if email later changes.
    if (
        not user.display_username
        and user.email
        and "@" in user.email
        and not user.email.endswith("@placeholder.local")
    ):
        from storage.models import User as UserModel

        base = user.email.split("@")[0].lower()
        candidate = base
        counter = 2
        while (
            UserModel.objects.filter(display_username=candidate)
            .exclude(id=user.id)
            .exists()
        ):
            candidate = f"{base}-{counter}"
            counter += 1
        user.display_username = candidate
        user.save(update_fields=["display_username"])
        logger.info(f"Set display_username={candidate} for {user.username}")

    logger.info(
        f"User provisioned successfully: username={user.username}, "
        f"external_id={user.external_id}, institution={user.institution}"
    )

    return {"user": user}


def extract_tenant_info(backend, details, response, user=None, *args, **kwargs):
    """Resolve tenant memberships from Authentik groups and RGWSquared state."""
    if not user:
        return None

    from django.conf import settings
    from storage.models import GroupTenantMapping, TenantMembership

    # preferred_username is the Ceph subuser name used by RGWSquared.
    ceph_username = response.get("preferred_username") or user.username
    groups = response.get("groups", []) or []

    admin_group = settings.AUTHENTIK_ADMIN_GROUP
    is_app_admin = bool(admin_group) and admin_group in groups
    user.is_staff = is_app_admin
    user.save(update_fields=["is_staff"])

    mappings = list(
        GroupTenantMapping.objects.select_related("tenant").filter(
            authentik_group__in=groups,
            tenant__is_active=True,
        )
    )
    if not mappings:
        _reconcile_access(user, set())
        if is_app_admin:
            logger.info(
                "Admin-only OAuth login for %s (no tenant group mappings)",
                user.username,
            )
            return {"user": user}
        logger.warning(
            "Login rejected for %s: Authentik groups %r have no matching GroupTenantMapping",
            user.username,
            groups,
        )
        raise AuthForbidden(
            backend,
            "No GroupTenantMapping matched Authentik groups %r — access denied." % (groups,),
        )

    # Collapse to one mapping per tenant: rw takes precedence over ro.
    # unique_together=(tenant, role) guarantees at most two entries per tenant.
    _best: dict[int, GroupTenantMapping] = {}
    for _m in mappings:
        _tid = _m.tenant_id
        if _tid not in _best or (_m.role == "rw" and _best[_tid].role != "rw"):
            _best[_tid] = _m
    mappings = list(_best.values())

    matched_tenants = []
    matched_tenant_ids = set()
    for mapping in mappings:
        tenant = mapping.tenant

        if is_rgwsquared_synced(tenant):
            if is_nffadi_tenant(tenant) and not is_valid_nffadi_mapping(mapping):
                logger.warning(
                    "Ignoring invalid NFFADI mapping group=%s role=%s for user=%s",
                    mapping.authentik_group,
                    mapping.role,
                    user.username,
                )
                continue
            if _sync_rgwsquared_user_access(user, ceph_username, tenant, required=True):
                matched_tenants.append(tenant.code)
                matched_tenant_ids.add(tenant.id)
            continue

        try:
            membership, created = TenantMembership.objects.get_or_create(
                user=user,
                tenant=tenant,
                defaults={
                    "ceph_username": ceph_username,
                    "role": mapping.role,
                    "is_active": True,
                },
            )
        except IntegrityError:
            logger.warning(
                "Ignoring tenant %s for %s: Ceph username %s is already assigned "
                "to another account",
                tenant.code,
                user.username,
                ceph_username,
            )
            continue
        update_fields = []
        if membership.ceph_username != ceph_username:
            membership.ceph_username = ceph_username
            update_fields.append("ceph_username")
        if membership.role != mapping.role:
            membership.role = mapping.role
            update_fields.append("role")
        if not membership.is_active:
            membership.is_active = True
            update_fields.append("is_active")
        if update_fields:
            membership.save(update_fields=update_fields)
        if created:
            logger.info(
                "Created TenantMembership from group: %s -> %s (role=%s)",
                user.username,
                tenant.code,
                mapping.role,
            )

        matched_tenants.append(tenant.code)
        matched_tenant_ids.add(tenant.id)
        _sync_rgwsquared_user_access(user, ceph_username, tenant, required=False)

    _reconcile_access(user, matched_tenant_ids)
    _sync_uo_codes_for_user(user)

    if not matched_tenants:
        if is_app_admin:
            logger.info(
                "Admin OAuth login for %s with no activated tenant memberships",
                user.username,
            )
            return {"user": user}
        logger.warning(
            "Login rejected for %s: group mappings matched but no activated tenant remained eligible",
            user.username,
        )
        raise AuthForbidden(
            backend,
            "Group mappings found for %s but no tenant remained eligible after processing." % (user.username,),
        )

    logger.info("User %s matched tenants: %s", user.username, matched_tenants)
    return None


def _reconcile_access(user, authorized_tenant_ids):
    """Clear revocation for matched tenants and revoke lost group access."""
    from storage.models import TenantMembership

    memberships = TenantMembership.objects.select_related("tenant").filter(user=user)
    memberships.filter(tenant_id__in=authorized_tenant_ids).update(
        access_revoked_at=None,
        access_revocation_reason="",
        access_revoked_group="",
    )

    for membership in memberships.exclude(tenant_id__in=authorized_tenant_ids):
        if membership.access_revoked_at is not None:
            continue
        mapped_groups = list(
            membership.tenant.group_mappings.values_list(
                "authentik_group", flat=True
            )
        )
        membership.access_revoked_at = timezone.now()
        membership.access_revocation_reason = "claim_missing"
        membership.access_revoked_group = ",".join(mapped_groups)
        membership.save(
            update_fields=[
                "access_revoked_at",
                "access_revocation_reason",
                "access_revoked_group",
            ]
        )


def _sync_rgwsquared_user_access(user, ceph_username, tenant, required=False):
    """Sync one user's RGWSquared role/buckets for a tenant.

    Returns True when RGWSquared confirms the user belongs to the structure.
    For optional tenants, failures only skip bucket refinement; the group mapping
    remains the login authority. RGWSquared-synced tenants pass required=True.
    """
    from django.conf import settings
    from storage.models import TenantMembership
    from storage.services.rgw_squared import RGWSquaredClient, RGWSquaredError

    structure = structure_name(tenant)
    if not structure:
        return False
    if not settings.RGWSQUARED_URL or not settings.RGWSQUARED_USERNAME:
        logger.warning("RGWSquared not configured, skipping user access sync")
        return False

    try:
        client = RGWSquaredClient(
            settings.RGWSQUARED_URL,
            settings.RGWSQUARED_USERNAME,
            settings.RGWSQUARED_PASSWORD,
        )
        users = client.list_users(structure)
        if ceph_username not in users:
            if not required:
                # Non-NFFADI: auto-provision the Ceph account on first login.
                # GroupTenantMapping grants access; we just need the account to exist.
                logger.info(
                    "Auto-provisioning RGWSquared user %s in %s", ceph_username, structure
                )
                try:
                    client.create_user(structure, ceph_username)
                except RGWSquaredError as create_err:
                    err_lower = str(create_err).lower()
                    if "already" in err_lower or "exist" in err_lower:
                        # Race between list_users and create_user — already exists, fine.
                        logger.info(
                            "RGWSquared user %s already present in %s (concurrent provision handled)",
                            ceph_username,
                            structure,
                        )
                    else:
                        logger.warning(
                            "Could not auto-provision %s in %s: %s — login will succeed "
                            "but S3 ops may fail until the account is created",
                            ceph_username,
                            structure,
                            create_err,
                        )
                        return False
                # Newly provisioned user has no buckets yet.
                # Skip get_user_info + update_or_create: empty RWBuckets would set
                # role="ro" and override the GroupTenantMapping role already written
                # by get_or_create in extract_tenant_info.
                return True
            else:
                logger.warning(
                    "RGWSquared user %s not found in structure %s", ceph_username, structure
                )
                return False

        info = client.get_user_info(structure, ceph_username)
        ro_buckets = info.get("ROBuckets", [])
        rw_buckets = info.get("RWBuckets", [])
        role = "rw" if rw_buckets else "ro"

        bucket_items = client.list_buckets(structure)
        _sync_user_buckets_on_login(
            user,
            tenant,
            ro_buckets,
            rw_buckets,
            bucket_items=bucket_items,
        )

        # For RGWSquared-synced tenants, role comes from upstream bucket assignments.
        # For Authentik-managed tenants, role comes from GroupTenantMapping;
        # extract_tenant_info already set it correctly — do not override.
        uoc_defaults = {"ceph_username": ceph_username, "is_active": True}
        if required:
            uoc_defaults["role"] = role
        membership, _ = TenantMembership.objects.update_or_create(
            user=user,
            tenant=tenant,
            defaults=uoc_defaults,
        )
        if not is_write_capable(membership.role) and membership.uo_code:
            membership.uo_code = ""
            membership.save(update_fields=["uo_code"])
        return True
    except Exception as e:
        if required:
            logger.warning(
                "Required RGWSquared access sync failed for %s/%s: %s",
                structure,
                ceph_username,
                e,
            )
        else:
            logger.warning(
                "Optional RGWSquared access sync failed for %s/%s: %s",
                structure,
                ceph_username,
                e,
            )
        return False


def _sync_uo_codes_for_user(user):
    """Assign UO codes only to write-capable memberships and clear stale RO UO."""
    from storage.models import TenantMembership, UOMapping

    for membership in TenantMembership.objects.select_related("tenant").filter(
        user=user,
        is_active=True,
    ):
        if not is_write_capable(membership.role):
            if membership.uo_code:
                membership.uo_code = ""
                membership.save(update_fields=["uo_code"])
                logger.info(
                    "Cleared uo_code for read-only membership %s in %s",
                    user.username,
                    membership.tenant.code,
                )
            continue

        if user.institution and not membership.uo_code:
            uo = UOMapping.objects.filter(
                tenant=membership.tenant,
                institution_name__icontains=user.institution,
            ).first()
            if uo:
                membership.uo_code = uo.uo_code
                membership.save(update_fields=["uo_code"])
                logger.info(
                    "Set uo_code=%s for %s in %s",
                    uo.uo_code,
                    user.username,
                    membership.tenant.code,
                )


def _sync_user_buckets_on_login(
    user, tenant, ro_bucket_names, rw_bucket_names, bucket_items=None
):
    """Create Bucket and BucketPermission records for a user's RGWSquared buckets.

    Called during login to ensure proposal buckets are visible immediately.
    Without this, user logs in but sees empty dashboard because no Django DB records exist.
    """
    from storage.models import Bucket, BucketPermission
    from storage.services.s3_ops import parse_rgwsquared_bucket_name

    auto_bucket_names = None
    if bucket_items is not None:
        auto_bucket_names = set()
        for item in bucket_items:
            if isinstance(item, str):
                auto_bucket_names.add(parse_rgwsquared_bucket_name(item, tenant.code))
            elif item.get("auto"):
                name = item.get("name") or item.get("id")
                if name:
                    auto_bucket_names.add(
                        parse_rgwsquared_bucket_name(str(name), tenant.code)
                    )

    all_buckets = [(name, "rw") for name in rw_bucket_names] + [
        (name, "ro") for name in ro_bucket_names
    ]

    for ms_name, perm in all_buckets:
        bare_name = parse_rgwsquared_bucket_name(ms_name, tenant.code)
        if auto_bucket_names is not None and bare_name not in auto_bucket_names:
            continue

        # Proposal buckets come from RGWSquared and are never locally deletable.
        bucket, created = Bucket.objects.get_or_create(
            name=bare_name,
            tenant=tenant,
            defaults={
                "bucket_type": Bucket.PROPOSAL,
                "is_deletable": False,
                "display_name": bare_name,
            },
        )
        if created:
            logger.info("Created proposal Bucket: %s/%s", tenant.code, bare_name)

        # Never downgrade RW if RGWSquared also reports the bucket as RO.
        existing = BucketPermission.objects.filter(bucket=bucket, user=user).first()
        if not existing:
            BucketPermission.objects.create(
                bucket=bucket,
                user=user,
                permission=perm,
                source="rgwsquared",
            )
        elif existing.permission == "ro" and perm == "rw":
            existing.permission = "rw"
            existing.save(update_fields=["permission"])


def persist_authentik_groups(
    backend, details, response, user=None, social=None, *args, **kwargs
):
    """Store claims on the social association used by this login."""
    if not user:
        return None

    from social_django.models import UserSocialAuth

    groups = response.get("groups", []) or []
    if isinstance(groups, str):
        groups = [groups]

    if social is None:
        social = UserSocialAuth.objects.filter(
            user=user,
            provider=backend.name,
            uid=response.get("sub"),
        ).first()
    if social is None:
        logger.warning("No UserSocialAuth row to persist groups for %s", user.username)
        return None

    extra = dict(social.extra_data or {})
    extra["groups"] = list(groups)
    extra["preferred_username"] = response.get("preferred_username") or user.username
    social.extra_data = extra
    social.save(update_fields=["extra_data"])
    logger.info("Persisted Authentik groups for %s: %s", user.username, groups)
    return None


def log_user_login(backend, details, response, user=None, *args, **kwargs):
    """
    Log user login for audit trail.

    This is useful for:
    - Security auditing (who logged in when)
    - Debugging OAuth2 issues
    - Compliance requirements

    Args:
        backend: Social auth backend
        details: User details
        response: OAuth2 response
        user: User instance

    Returns:
        None
    """
    if user:
        groups = response.get("groups", []) or []
        logger.info(
            "OAuth2 login successful: user=%s external_id=%s institution=%s "
            "email=%s idp_source=%s groups=%r is_staff=%s",
            user.username,
            user.external_id,
            user.institution,
            user.email,
            user.idp_source,
            groups,
            user.is_staff,
        )
    else:
        logger.warning("OAuth2 pipeline completed but user is None")

    return None
