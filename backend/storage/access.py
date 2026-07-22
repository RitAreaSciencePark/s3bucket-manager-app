"""Shared tenant access policy helpers."""

WRITE_CAPABLE_ROLES = {"rw", "admin"}
NFFADI_STRUCTURE = "NFFADI"
NFFADI_AUTHENTIK_GROUP = "nffa-di-users"


def is_write_capable(role):
    return role in WRITE_CAPABLE_ROLES


def structure_name(tenant):
    return tenant.rgwsquared_structure or tenant.code


def is_nffadi_tenant(tenant):
    return structure_name(tenant).upper() == NFFADI_STRUCTURE


def is_rgwsquared_synced(tenant):
    from storage.models import Tenant

    return tenant.access_model == Tenant.RGWSQUARED_SYNCED


def suggested_group_name(structure, role):
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in structure)
    slug = "-".join(part for part in slug.split("-") if part)
    if structure.upper() == NFFADI_STRUCTURE:
        return NFFADI_AUTHENTIK_GROUP
    return f"{slug}-users" if role == "rw" else f"{slug}-ext"


def is_valid_nffadi_mapping(mapping):
    return (
        is_nffadi_tenant(mapping.tenant)
        and mapping.role == "rw"
        and mapping.authentik_group == NFFADI_AUTHENTIK_GROUP
    )


def tenant_has_login_mapping(tenant):
    """Return whether a tenant has a complete Authentik login gate."""
    mappings = list(tenant.group_mappings.all())
    if is_nffadi_tenant(tenant):
        return len(mappings) == 1 and is_valid_nffadi_mapping(mappings[0])
    if is_rgwsquared_synced(tenant):
        return len(mappings) == 1
    return bool(mappings)


def configured_tenant_ids():
    from storage.models import Tenant

    return {
        tenant.id
        for tenant in Tenant.objects.filter(is_active=True).prefetch_related(
            "group_mappings"
        )
        if tenant_has_login_mapping(tenant)
    }


def membership_has_access(membership):
    return bool(
        membership.is_active
        and membership.user.is_active
        and membership.tenant.is_active
        and membership.access_revoked_at is None
        and tenant_has_login_mapping(membership.tenant)
    )


def current_authentik_groups(user):
    """Read claims only from the social association for the user's OIDC subject."""
    from social_django.models import UserSocialAuth

    association = UserSocialAuth.objects.filter(
        provider="authentik",
        uid=user.external_id,
    ).first()
    if not association:
        return set()
    groups = (association.extra_data or {}).get("groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    return set(groups)
