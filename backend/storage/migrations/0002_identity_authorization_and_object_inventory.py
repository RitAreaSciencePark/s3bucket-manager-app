from django.db import migrations, models


def preserve_existing_memberships(apps, schema_editor):
    TenantMembership = apps.get_model("storage", "TenantMembership")
    TenantMembership.objects.update(idp_authorized=True)


class Migration(migrations.Migration):
    dependencies = [("storage", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(
                help_text=(
                    "Email address from IdP (OAuth2 'email' claim). Not an identity "
                    "key: distinct OIDC subjects may share an email address."
                ),
                max_length=254,
            ),
        ),
        migrations.AddField(
            model_name="tenantmembership",
            name="idp_authorized",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Whether the user's latest OIDC groups authorize this tenant. "
                    "RGWSquared synchronization must not grant this flag."
                ),
            ),
        ),
        migrations.RunPython(preserve_existing_memberships, migrations.RunPython.noop),
        migrations.AddField(
            model_name="fileuploadrecord",
            name="object_etag",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fileuploadrecord",
            name="object_last_modified",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fileuploadrecord",
            name="origin",
            field=models.CharField(
                choices=[
                    ("app", "Uploaded through Buckets Explorer"),
                    ("discovered", "Discovered in object storage"),
                ],
                default="app",
                max_length=12,
            ),
        ),
    ]
