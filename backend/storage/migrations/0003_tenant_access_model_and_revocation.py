from django.db import migrations, models
from django.utils import timezone


def migrate_access_state(apps, schema_editor):
    Tenant = apps.get_model("storage", "Tenant")
    TenantMembership = apps.get_model("storage", "TenantMembership")

    Tenant.objects.filter(code__iexact="NFFADI").update(
        access_model="rgwsquared_synced"
    )

    now = timezone.now()
    memberships = TenantMembership.objects.select_related("tenant", "user").filter(
        idp_authorized=False
    )
    for membership in memberships:
        if membership.tenant.access_model == "rgwsquared_synced":
            continue
        if membership.user.external_id.startswith("ms:"):
            membership.is_active = False
            membership.save(update_fields=["is_active"])
            continue
        membership.access_revoked_at = now
        membership.access_revocation_reason = "claim_missing"
        membership.save(
            update_fields=["access_revoked_at", "access_revocation_reason"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("storage", "0002_identity_authorization_and_object_inventory"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="access_model",
            field=models.CharField(
                choices=[
                    ("rgwsquared_synced", "RGWSquared-synced"),
                    ("authentik_managed", "Authentik-managed"),
                ],
                default="authentik_managed",
                help_text=(
                    "RGWSquared-synced tenants import users and roles from RGWSquared; "
                    "Authentik-managed tenants register users and roles at OIDC login."
                ),
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="tenantmembership",
            name="access_revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantmembership",
            name="access_revocation_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("mapping_removed", "Authentik group mapping removed"),
                    ("claim_missing", "Mapped Authentik group missing at login"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="tenantmembership",
            name="access_revoked_group",
            field=models.CharField(
                blank=True,
                help_text="Mapped Authentik group whose loss caused the revocation.",
                max_length=200,
            ),
        ),
        migrations.RunPython(migrate_access_state, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="tenantmembership",
            name="idp_authorized",
        ),
    ]
