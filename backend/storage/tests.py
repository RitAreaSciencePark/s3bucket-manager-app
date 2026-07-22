import os
import time
from io import StringIO
from unittest.mock import patch

import requests
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import Resolver404, resolve
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from social_django.models import UserSocialAuth
from social_core.exceptions import AuthForbidden

from storage.middleware import OAuthExceptionRedirectMiddleware, OAuthNextValidationMiddleware
from storage.pipeline import (
    associate_by_ceph_username,
    extract_tenant_info,
    persist_authentik_groups,
)
from storage.views.auth import exchange_token
from storage.services import permissions as bucket_permissions
from storage.tokens import AdminRefreshToken
from storage.models import (
    Bucket,
    BucketPermission,
    FileUploadRecord,
    GroupTenantMapping,
    Tenant,
    TenantMembership,
    UOMapping,
    User,
)
from storage.services.rgw_squared import RGWSquaredClient, RGWSquaredError
from storage.services.s3_ops import fetch_mgmt_keys
from storage.services.sync_service import refresh_local_cache
from storage.views.admin import (
    AdminExchangeThrottle,
    admin_bucket_detail,
    admin_buckets,
    admin_create_tenant,
    admin_exchange_token,
    admin_group_mapping_delete,
    admin_group_mappings,
    admin_membership_files,
    admin_sync_refresh,
    admin_sync_upload_csv,
    admin_tenant_activation,
    admin_users,
)
from storage.views.buckets import BucketViewSet


ADMIN_GROUP = "buckets-explorer-admin"


def force_admin(request, user):
    refresh = AdminRefreshToken.for_admin_user(user)
    force_authenticate(request, user=user, token=refresh.access_token)


def attach_session(request):
    request.session = SessionStore()
    request.session.create()


def link_authentik_user(user, groups=None, uid=None):
    UserSocialAuth.objects.create(
        user=user,
        provider="authentik",
        uid=uid or user.external_id,
        extra_data={"groups": groups or [ADMIN_GROUP]},
    )


class FakeHTTPResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise requests.exceptions.JSONDecodeError("bad json", self.text, 0)
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.text)


class RGWSquaredClientTests(TestCase):
    def rgw_client(self):
        client = RGWSquaredClient("http://rgw", "admin", "secret")
        client._token = "token"
        client._token_expires_at = time.time() + 3600
        return client

    @patch("storage.services.rgw_squared.requests.post")
    def test_v2_paths_and_payloads(self, post):
        post.return_value = FakeHTTPResponse(json_data={"res": []})
        client = self.rgw_client()

        client.list_structures()
        client.upload_csv("dGVzdA==")
        client.update_structure("NFFADI", update_from_ext=True)

        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "http://rgw/s3struct/structureList",
                "http://rgw/s3structnffadi/csvUpload",
                "http://rgw/s3struct/structureUpdate",
            ],
        )
        self.assertEqual(post.call_args_list[0].kwargs["json"], {})
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"content": "dGVzdA=="})
        self.assertEqual(
            post.call_args_list[2].kwargs["json"],
            {"updateFromExt": True, "structure": "NFFADI"},
        )

    @patch("storage.services.rgw_squared.requests.post")
    def test_plain_text_500_body_is_preserved(self, post):
        post.return_value = FakeHTTPResponse(
            status_code=500,
            text='The call "bucketCreate" from webservice "s3struct" has failed.',
        )

        with self.assertRaises(RGWSquaredError) as ctx:
            self.rgw_client().create_bucket("NFFADI", "demo")

        self.assertIn("bucketCreate", str(ctx.exception))


class PurgeLocalStaffUsersCommandTests(TestCase):
    def test_deletes_local_staff_users(self):
        User.objects.create_user(
            username="legacy-admin",
            email="legacy@example.com",
            external_id="legacy-local",
            password="legacy-password",
            idp_source="local",
            is_staff=True,
            is_superuser=True,
            is_approved=True,
        )

        out = StringIO()
        call_command("purge_local_staff_users", stdout=out)

        self.assertIn("Deleted 1", out.getvalue())
        self.assertFalse(User.objects.filter(username="legacy-admin").exists())


@override_settings(AUTHENTIK_ADMIN_GROUP=ADMIN_GROUP)
class AdminAuthSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            external_id="sub-admin",
            password="unused",
            is_staff=True,
            is_approved=True,
            idp_source="authentik",
        )
        link_authentik_user(self.admin)

    def test_admin_exchange_returns_admin_jwt(self):
        request = self.factory.get("/api/admin/auth/token/")
        attach_session(request)
        force_authenticate(request, user=self.admin)
        response = admin_exchange_token(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_admin_exchange_succeeds_without_extra_data_groups(self):
        UserSocialAuth.objects.filter(user=self.admin).update(extra_data={})
        request = self.factory.get("/api/admin/auth/token/")
        attach_session(request)
        force_authenticate(request, user=self.admin)
        response = admin_exchange_token(request)
        self.assertEqual(response.status_code, 200, response.data)

    def test_admin_exchange_rejects_non_staff(self):
        user = User.objects.create_user(
            username="regular",
            email="regular@example.com",
            external_id="sub-regular",
            password="unused",
            is_staff=False,
            is_approved=True,
            idp_source="authentik",
        )
        link_authentik_user(user, groups=["tenant-users"])

        request = self.factory.get("/api/admin/auth/token/")
        force_authenticate(request, user=user)
        response = admin_exchange_token(request)

        self.assertEqual(response.status_code, 403, response.data)

    def test_admin_exchange_requires_session(self):
        request = self.factory.get("/api/admin/auth/token/")
        response = admin_exchange_token(request)
        self.assertEqual(response.status_code, 401, response.data)

    def test_admin_exchange_is_throttled(self):
        with patch.object(
            AdminExchangeThrottle, "THROTTLE_RATES", {"admin_exchange": "2/min"}
        ):
            for _ in range(2):
                request = self.factory.get(
                    "/api/admin/auth/token/",
                    REMOTE_ADDR="203.0.113.11",
                )
                attach_session(request)
                force_authenticate(request, user=self.admin)
                response = admin_exchange_token(request)
                self.assertEqual(response.status_code, 200, response.data)

            request = self.factory.get(
                "/api/admin/auth/token/",
                REMOTE_ADDR="203.0.113.11",
            )
            attach_session(request)
            force_authenticate(request, user=self.admin)
            response = admin_exchange_token(request)
            self.assertEqual(response.status_code, 429, response.data)

    def test_regular_user_jwt_cannot_access_admin_api(self):
        user = User.objects.create_user(
            username="tenant-user",
            email="tenant@example.com",
            external_id="sub-tenant",
            password="unused",
            is_staff=True,
            is_approved=True,
            idp_source="authentik",
        )
        from rest_framework_simplejwt.tokens import RefreshToken

        refresh = RefreshToken.for_user(user)
        request = self.factory.get("/api/admin/buckets/")
        force_authenticate(request, user=user, token=refresh.access_token)
        response = admin_buckets(request)
        self.assertEqual(response.status_code, 403)

    def test_admin_jwt_can_access_admin_api(self):
        request = self.factory.get("/api/admin/buckets/")
        force_admin(request, self.admin)
        with patch("storage.views.admin.get_bucket_stats_for_tenant", return_value={}):
            response = admin_buckets(request)
        self.assertEqual(response.status_code, 200)

    def test_staff_user_has_no_bucket_owner_bypass(self):
        user = User.objects.create_user(
            username="staff-no-bypass",
            email="staff@example.com",
            external_id="sub-staff",
            password="unused",
            is_staff=True,
            is_approved=True,
        )
        tenant = Tenant.objects.create(code="LAB", name="Lab", rgwsquared_structure="LAB")
        bucket = Bucket.objects.create(
            tenant=tenant,
            name="demo",
            bucket_type=Bucket.LOCAL,
        )
        self.assertIsNone(bucket_permissions.get_user_permission(user, bucket))

    def test_revoked_membership_blocks_existing_bucket_permission(self):
        user = User.objects.create_user(
            username="revoked-user",
            email="revoked@example.com",
            external_id="sub-revoked",
        )
        tenant = Tenant.objects.create(
            code="REVOKED",
            name="Revoked",
            rgwsquared_structure="REVOKED",
        )
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="revoked-users",
            role="rw",
        )
        membership = TenantMembership.objects.create(
            user=user,
            tenant=tenant,
            ceph_username="revoked-user",
            role="rw",
            access_revoked_at=timezone.now(),
            access_revocation_reason="mapping_removed",
        )
        bucket = Bucket.objects.create(
            tenant=tenant,
            name="project",
            bucket_type=Bucket.LOCAL,
        )
        BucketPermission.objects.create(
            user=user,
            bucket=bucket,
            permission="owner",
            source="local",
        )

        self.assertFalse(bucket_permissions.can_create_bucket(membership))
        self.assertIsNone(bucket_permissions.get_user_permission(user, bucket))

    def test_pipeline_syncs_and_revokes_staff_from_admin_group(self):
        user = User.objects.create_user(
            username="oauth-admin",
            email="oauth-admin@example.com",
            external_id="sub-oauth-admin",
            password="unused",
            is_staff=False,
            is_approved=True,
            idp_source="authentik",
        )
        backend = type("Backend", (), {"name": "authentik"})()

        extract_tenant_info(
            backend,
            {},
            {"groups": [ADMIN_GROUP], "preferred_username": "oauth-admin"},
            user=user,
        )
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

        tenant = Tenant.objects.create(code="LAB2", name="Lab 2", rgwsquared_structure="LAB2")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="tenant-users",
            role="rw",
        )
        with patch("storage.pipeline._sync_rgwsquared_user_access", return_value=True):
            extract_tenant_info(
                backend,
                {},
                {"groups": ["tenant-users"], "preferred_username": "oauth-admin"},
                user=user,
            )
        user.refresh_from_db()
        self.assertFalse(user.is_staff)

    def test_oauth_next_validation_rejects_external_redirect(self):
        middleware = OAuthNextValidationMiddleware(lambda req: None)
        request = self.factory.get(
            "/api/oauth/login/authentik/?next=https://evil.example/admin"
        )
        response = middleware(request)
        self.assertEqual(response.status_code, 400)

    def test_persist_authentik_groups_writes_extra_data(self):
        backend = type("Backend", (), {"name": "authentik"})()
        social = UserSocialAuth.objects.get(user=self.admin, provider="authentik")
        persist_authentik_groups(
            backend,
            {},
            {
                "sub": social.uid,
                "groups": [ADMIN_GROUP, "other-group"],
                "preferred_username": "admin",
            },
            user=self.admin,
            social=social,
        )
        social.refresh_from_db()
        self.assertEqual(social.extra_data.get("groups"), [ADMIN_GROUP, "other-group"])
        self.assertEqual(social.extra_data.get("preferred_username"), "admin")

    def test_persist_authentik_groups_updates_only_current_association(self):
        backend = type("Backend", (), {"name": "authentik"})()
        primary = UserSocialAuth.objects.get(user=self.admin, provider="authentik")
        secondary = UserSocialAuth.objects.create(
            user=self.admin,
            provider="authentik",
            uid="sub-admin-secondary",
            extra_data={"groups": ["tenant-old"]},
        )

        persist_authentik_groups(
            backend,
            {},
            {
                "sub": secondary.uid,
                "groups": [ADMIN_GROUP, "tenant-new"],
                "preferred_username": "admin-secondary",
            },
            user=self.admin,
            social=secondary,
        )

        primary.refresh_from_db()
        secondary.refresh_from_db()
        self.assertEqual(primary.extra_data["groups"], [ADMIN_GROUP])
        self.assertEqual(secondary.extra_data["groups"], [ADMIN_GROUP, "tenant-new"])

    def test_admin_only_user_gets_hint_on_user_exchange(self):
        request = self.factory.get("/api/auth/token/")
        attach_session(request)
        force_authenticate(request, user=self.admin)
        response = exchange_token(request)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("/admin/login", response.data["error"])

    def test_distinct_oidc_subjects_can_share_email(self):
        other = User.objects.create_user(
            username="admin-secondary",
            email=self.admin.email,
            external_id="sub-admin-secondary-email",
        )

        self.assertNotEqual(other.id, self.admin.id)
        self.assertEqual(User.objects.filter(email=self.admin.email).count(), 2)

    def test_ceph_username_bridge_claims_only_sync_placeholders(self):
        tenant = Tenant.objects.create(
            code="BRIDGE",
            name="Bridge",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )
        TenantMembership.objects.create(
            user=self.admin,
            tenant=tenant,
            ceph_username="shared-name",
            role="rw",
        )

        result = associate_by_ceph_username(
            None,
            {},
            {"preferred_username": "shared-name"},
        )

        self.assertIsNone(result)

        placeholder = User.objects.create_user(
            username="placeholder",
            email="placeholder@placeholder.local",
            external_id="ms:BRIDGE:placeholder",
        )
        TenantMembership.objects.create(
            user=placeholder,
            tenant=tenant,
            ceph_username="placeholder",
            role="ro",
        )
        result = associate_by_ceph_username(
            None,
            {},
            {"preferred_username": "placeholder"},
        )
        self.assertEqual(result["user"], placeholder)


@override_settings(
    RGWSQUARED_URL="http://rgw",
    RGWSQUARED_USERNAME="admin",
    RGWSQUARED_PASSWORD="secret",
)
class AdminBucketDeleteTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="admin-delete",
            email="admin-delete@example.com",
            external_id="sub-admin-delete",
            password="unused",
            is_staff=True,
            is_approved=True,
            idp_source="authentik",
        )
        link_authentik_user(self.admin)
        self.tenant = Tenant.objects.create(
            code="GENOME",
            name="Genome",
            rgwsquared_structure="GENOME",
        )
        self.bucket = Bucket.objects.create(
            name="orphan-bucket-01",
            tenant=self.tenant,
            bucket_type=Bucket.LOCAL,
            is_deletable=True,
        )

    def admin_delete(self, bucket_id, force=False):
        path = f"/api/admin/buckets/{bucket_id}/"
        if force:
            path += "?force=true"
        request = self.factory.delete(path)
        force_admin(request, self.admin)
        return admin_bucket_detail(request, bucket_id=bucket_id)

    def test_admin_delete_success_removes_metadata(self):
        rgw = RecordingRGWClient(users=[])
        with patch("storage.views.admin._get_sync_client", return_value=rgw):
            response = self.admin_delete(self.bucket.id)

        self.assertEqual(response.status_code, 204, response.data)
        self.assertEqual(
            rgw.calls,
            [("structureInfo", "GENOME"), ("bucketDelete", "GENOME", "orphan-bucket-01")],
        )
        self.assertFalse(Bucket.objects.filter(id=self.bucket.id).exists())

    def test_admin_delete_connectivity_error_keeps_metadata(self):
        class ConnectivityRGWClient(RecordingRGWClient):
            def delete_bucket(self, structure, bucket_name):
                super().delete_bucket(structure, bucket_name)
                raise RGWSquaredError(
                    "Inaccessible host: `10.128.2.28' at port `7480'"
                )

        rgw = ConnectivityRGWClient(users=[])
        with patch("storage.views.admin._get_sync_client", return_value=rgw):
            response = self.admin_delete(self.bucket.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(Bucket.objects.filter(id=self.bucket.id).exists())

    def test_admin_delete_not_found_still_removes_metadata(self):
        class MissingRGWClient(RecordingRGWClient):
            def delete_bucket(self, structure, bucket_name):
                super().delete_bucket(structure, bucket_name)
                raise RGWSquaredError("Bucket not found")

        rgw = MissingRGWClient(users=[])
        with patch("storage.views.admin._get_sync_client", return_value=rgw):
            response = self.admin_delete(self.bucket.id)

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(Bucket.objects.filter(id=self.bucket.id).exists())

    def test_admin_delete_ignores_force_query_param(self):
        class ConnectivityRGWClient(RecordingRGWClient):
            def delete_bucket(self, structure, bucket_name):
                super().delete_bucket(structure, bucket_name)
                raise RGWSquaredError(
                    "Inaccessible host: `10.128.2.28' at port `7480'"
                )

        rgw = ConnectivityRGWClient(users=[])
        with patch("storage.views.admin._get_sync_client", return_value=rgw):
            response = self.admin_delete(self.bucket.id, force=True)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(Bucket.objects.filter(id=self.bucket.id).exists())


class AdminMembershipUsersTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="admin-user",
            email="admin-user@example.com",
            external_id="sub-admin-user",
            password="admin-password",
            is_staff=True,
            is_superuser=True,
            is_approved=True,
        )
        self.tenant_a = Tenant.objects.create(
            code="TENANT_A",
            name="Tenant A",
            rgwsquared_structure="TENANT_A",
        )
        self.tenant_b = Tenant.objects.create(
            code="TENANT_B",
            name="Tenant B",
            rgwsquared_structure="TENANT_B",
        )
        self.user = User.objects.create_user(
            username="researcher",
            email="researcher@example.com",
            external_id="sub-researcher",
        )
        self.membership_a = TenantMembership.objects.create(
            user=self.user,
            tenant=self.tenant_a,
            ceph_username="researcher-a",
            role="rw",
        )
        self.membership_b = TenantMembership.objects.create(
            user=self.user,
            tenant=self.tenant_b,
            ceph_username="researcher-b",
            role="ro",
        )
        self.bucket_a = Bucket.objects.create(
            name="tenant-a-bucket",
            display_name="bucket-a",
            tenant=self.tenant_a,
            owner=self.user,
            bucket_type=Bucket.LOCAL,
        )
        self.bucket_b = Bucket.objects.create(
            name="tenant-b-bucket",
            display_name="bucket-b",
            tenant=self.tenant_b,
            owner=self.user,
            bucket_type=Bucket.LOCAL,
        )
        FileUploadRecord.objects.create(
            bucket=self.bucket_a,
            uploaded_by=self.user,
            file_key="a-one.txt",
            file_size=100,
        )
        FileUploadRecord.objects.create(
            bucket=self.bucket_b,
            uploaded_by=self.user,
            file_key="b-one.txt",
            file_size=300,
        )
        FileUploadRecord.objects.create(
            bucket=self.bucket_b,
            uploaded_by=self.user,
            file_key="b-two.txt",
            file_size=700,
        )

    def admin_request(self, path):
        request = self.factory.get(path)
        force_authenticate(request, user=self.admin)
        return request

    def test_admin_users_reports_storage_per_membership_tenant(self):
        request = self.admin_request("/api/admin/users/")
        response = admin_users(request)

        self.assertEqual(response.status_code, 200, response.data)
        rows = {
            row["tenant_code"]: row
            for row in response.data
            if row["user_id"] == self.user.id
        }
        self.assertEqual(rows["TENANT_A"]["membership_id"], self.membership_a.id)
        self.assertEqual(rows["TENANT_A"]["file_count"], 1)
        self.assertEqual(rows["TENANT_A"]["total_file_size"], 100)
        self.assertEqual(rows["TENANT_B"]["membership_id"], self.membership_b.id)
        self.assertEqual(rows["TENANT_B"]["file_count"], 2)
        self.assertEqual(rows["TENANT_B"]["total_file_size"], 1000)

    def test_admin_users_distinguishes_authorized_revoked_and_not_configured(self):
        GroupTenantMapping.objects.create(
            tenant=self.tenant_a,
            authentik_group="tenant-a-users",
            role="rw",
        )
        self.membership_b.access_revoked_at = timezone.now()
        self.membership_b.access_revocation_reason = "mapping_removed"
        self.membership_b.access_revoked_group = "tenant-b-users"
        self.membership_b.save(
            update_fields=[
                "access_revoked_at",
                "access_revocation_reason",
                "access_revoked_group",
            ]
        )
        tenant_c = Tenant.objects.create(
            code="TENANT_C",
            name="Tenant C",
            rgwsquared_structure="TENANT_C",
        )
        membership_c = TenantMembership.objects.create(
            user=self.user,
            tenant=tenant_c,
            ceph_username="researcher-c",
            role="ro",
        )

        response = admin_users(self.admin_request("/api/admin/users/"))

        rows = {row["tenant_code"]: row for row in response.data}
        self.assertEqual(rows["TENANT_A"]["access_status"], "authorized")
        self.assertEqual(rows["TENANT_B"]["access_status"], "revoked")
        self.assertEqual(rows["TENANT_B"]["access_revoked_group"], "tenant-b-users")
        self.assertEqual(rows["TENANT_C"]["access_status"], "not_configured")
        self.assertTrue(TenantMembership.objects.filter(id=membership_c.id).exists())

    def test_admin_users_tenant_filter_is_membership_scoped(self):
        request = self.admin_request("/api/admin/users/?tenant_code=TENANT_B")
        response = admin_users(request)

        self.assertEqual(response.status_code, 200, response.data)
        tenant_codes = {row["tenant_code"] for row in response.data}
        self.assertEqual(tenant_codes, {"TENANT_B"})

    def test_membership_files_do_not_leak_across_tenants(self):
        request = self.admin_request(
            f"/api/admin/memberships/{self.membership_a.id}/files/"
        )
        response = admin_membership_files(request, self.membership_a.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["file_key"] for row in response.data], ["a-one.txt"])
        self.assertEqual({row["tenant_code"] for row in response.data}, {"TENANT_A"})

    def test_old_user_files_route_is_removed(self):
        with self.assertRaises(Resolver404):
            resolve("/api/admin/users/1/files/")


class TenantActivationSummaryTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="activation-admin",
            email="activation-admin@example.com",
            external_id="sub-activation-admin",
            password="admin-password",
            is_staff=True,
            is_superuser=True,
            is_approved=True,
        )

    def admin_request(self):
        request = self.factory.get("/api/admin/tenant-activation/")
        force_authenticate(request, user=self.admin)
        return request

    def activation_rows(self, structures, structure_info):
        class ActivationClient:
            def list_structures(self):
                return structures

            def get_structure_info(self, structure):
                return structure_info.get(structure, {"initialized": True})

        with patch("storage.views.admin._get_sync_client", return_value=ActivationClient()):
            response = admin_tenant_activation(self.admin_request())

        self.assertEqual(response.status_code, 200, response.data)
        return {row["structure"]: row for row in response.data}

    def create_tenant(self, code="LAB"):
        return Tenant.objects.create(
            code=code,
            name=f"{code} Lab",
            rgwsquared_structure=code,
            access_model=(
                Tenant.RGWSQUARED_SYNCED
                if code == "NFFADI"
                else Tenant.AUTHENTIK_MANAGED
            ),
            is_active=True,
        )

    def create_member(self, tenant, username="researcher", role="rw", uo_code=""):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            external_id=f"sub-{username}",
        )
        return TenantMembership.objects.create(
            user=user,
            tenant=tenant,
            ceph_username=username,
            role=role,
            uo_code=uo_code,
        )

    def test_tenant_creation_requires_explicit_access_model(self):
        request = self.factory.post(
            "/api/admin/tenants/create/",
            {"structure": "LAB", "name": "Lab"},
            format="json",
        )
        force_authenticate(request, user=self.admin)

        response = admin_create_tenant(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("access_model", response.data["error"])

    def test_nffadi_rejects_authentik_managed_access_model(self):
        request = self.factory.post(
            "/api/admin/tenants/create/",
            {
                "structure": "NFFADI",
                "name": "NFFA-DI",
                "access_model": Tenant.AUTHENTIK_MANAGED,
            },
            format="json",
        )
        force_authenticate(request, user=self.admin)

        response = admin_create_tenant(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn(Tenant.RGWSQUARED_SYNCED, response.data["error"])

    def test_uninitialized_rgwsquared_structure_is_not_fully_active(self):
        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": False}})

        row = rows["LAB"]
        self.assertFalse(row["fully_active"])
        self.assertFalse(row["has_tenant"])
        self.assertFalse(row["initialized"])

    def test_initialized_structure_without_local_tenant_is_not_fully_active(self):
        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertFalse(row["fully_active"])
        self.assertFalse(row["has_tenant"])
        self.assertTrue(row["initialized"])

    def test_local_tenant_without_group_mapping_is_not_fully_active(self):
        tenant = self.create_tenant()
        self.create_member(tenant, role="rw")

        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertFalse(row["fully_active"])
        self.assertTrue(row["has_tenant"])
        self.assertFalse(row["has_group_mapping"])

    def test_activation_matches_tenant_by_rgwsquared_structure(self):
        tenant = self.create_tenant(code="LOCAL_CODE")
        tenant.rgwsquared_structure = "LAB"
        tenant.save(update_fields=["rgwsquared_structure"])

        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertTrue(row["has_tenant"])
        self.assertEqual(row["tenant_code"], "LOCAL_CODE")
        self.assertEqual(row["structure"], "LAB")

    def test_nffadi_requires_exact_single_group_mapping(self):
        tenant = self.create_tenant(code="NFFADI")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="nffadi-users",
            role="rw",
        )

        rows = self.activation_rows(["NFFADI"], {"NFFADI": {"initialized": True}})

        row = rows["NFFADI"]
        self.assertFalse(row["fully_active"])
        self.assertFalse(row["group_mapping_ready"])
        self.assertEqual(row["required_group_name"], "nffa-di-users")
        self.assertEqual(row["role_source"], "rgwsquared")

    def test_nffadi_exact_group_mapping_is_fully_active_without_uo_requirement(self):
        tenant = self.create_tenant(code="NFFADI")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="nffa-di-users",
            role="rw",
        )

        rows = self.activation_rows(["NFFADI"], {"NFFADI": {"initialized": True}})

        row = rows["NFFADI"]
        self.assertTrue(row["fully_active"])
        self.assertTrue(row["group_mapping_ready"])
        self.assertEqual(row["suggested_ro_group"], "nffa-di-users")

    def test_group_mapped_tenant_without_uo_requirement_is_fully_active(self):
        tenant = self.create_tenant()
        self.create_member(tenant, role="rw")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="lab-users",
            role="rw",
        )

        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertTrue(row["fully_active"])
        self.assertTrue(row["has_group_mapping"])
        self.assertFalse(row["requires_uo_sync"])
        self.assertTrue(row["uo_ready"])

    def test_uo_required_tenant_missing_write_capable_uo_is_not_fully_active(self):
        tenant = self.create_tenant()
        self.create_member(tenant, role="rw", uo_code="")
        self.create_member(tenant, username="reader", role="ro", uo_code="")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="lab-users",
            role="rw",
        )
        UOMapping.objects.create(
            tenant=tenant,
            uo_code="lab-unit",
            institution_name="Lab Unit",
        )

        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertFalse(row["fully_active"])
        self.assertTrue(row["requires_uo_sync"])
        self.assertFalse(row["uo_ready"])
        self.assertEqual(row["missing_uo_count"], 1)

    def test_admin_sync_refresh_calls_rgwsquared_cache_refresh(self):
        tenant = self.create_tenant()
        request = self.factory.post(
            "/api/admin/sync/refresh/",
            {"structure_code": tenant.code},
            format="json",
        )
        force_authenticate(request, user=self.admin)

        with patch(
            "storage.views.admin.refresh_local_cache",
            return_value={"users_synced": 1, "buckets_synced": 2},
        ) as refresh:
            response = admin_sync_refresh(request)

        self.assertEqual(response.status_code, 200, response.data)
        refresh.assert_called_once_with(tenant)
        self.assertEqual(response.data["users_synced"], 1)
        self.assertEqual(response.data["buckets_synced"], 2)

    def test_uo_required_tenant_with_write_capable_uo_is_fully_active(self):
        tenant = self.create_tenant()
        self.create_member(tenant, role="rw", uo_code="lab-unit")
        self.create_member(tenant, username="reader", role="ro", uo_code="")
        GroupTenantMapping.objects.create(
            tenant=tenant,
            authentik_group="lab-users",
            role="rw",
        )
        UOMapping.objects.create(
            tenant=tenant,
            uo_code="lab-unit",
            institution_name="Lab Unit",
        )

        rows = self.activation_rows(["LAB"], {"LAB": {"initialized": True}})

        row = rows["LAB"]
        self.assertTrue(row["fully_active"])
        self.assertTrue(row["requires_uo_sync"])
        self.assertTrue(row["uo_ready"])
        self.assertEqual(row["missing_uo_count"], 0)


class OAuthExceptionMiddlewareTests(TestCase):
    def test_auth_state_missing_redirects_to_login_retry(self):
        from social_core.exceptions import AuthStateMissing

        request = APIRequestFactory().get("/api/oauth/complete/authentik/")
        middleware = OAuthExceptionRedirectMiddleware(lambda req: None)
        response = middleware.process_exception(request, AuthStateMissing(None))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login?auth_error=oauth_state_missing")

    def test_auth_state_missing_redirects_to_admin_login_when_next_is_admin(self):
        from social_core.exceptions import AuthStateMissing

        request = APIRequestFactory().get("/api/oauth/complete/authentik/")
        attach_session(request)
        request.session["next"] = "/admin/auth/callback"
        middleware = OAuthExceptionRedirectMiddleware(lambda req: None)
        response = middleware.process_exception(request, AuthStateMissing(None))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/login?auth_error=oauth_state_missing")


class AdminGroupMappingPolicyTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="group-admin",
            email="group-admin@example.com",
            external_id="sub-group-admin",
            password="admin-password",
            is_staff=True,
            is_superuser=True,
            is_approved=True,
        )
        self.nffadi = Tenant.objects.create(
            code="NFFADI",
            name="NFFA-DI",
            rgwsquared_structure="NFFADI",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )
        self.orbit = Tenant.objects.create(
            code="ORBIT",
            name="Orbit",
            rgwsquared_structure="ORBIT",
        )

    def post_mapping(self, tenant, group, role="rw"):
        request = self.factory.post(
            "/api/admin/group-mappings/",
            {"tenant_id": tenant.id, "authentik_group": group, "role": role},
            format="json",
        )
        force_authenticate(request, user=self.admin)
        return admin_group_mappings(request)

    def test_nffadi_accepts_only_single_required_group(self):
        response = self.post_mapping(self.nffadi, "nffa-di-users", "rw")
        self.assertEqual(response.status_code, 201, response.data)

        response = self.post_mapping(self.nffadi, "nffa-di-readers", "ro")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("nffa-di-users", response.data["error"])

    def test_non_nffadi_allows_generic_rw_and_ro_groups(self):
        response = self.post_mapping(self.orbit, "orbit-users", "rw")
        self.assertEqual(response.status_code, 201, response.data)
        response = self.post_mapping(self.orbit, "orbit-ext", "ro")
        self.assertEqual(response.status_code, 201, response.data)

    def test_deleting_last_mapping_revokes_existing_memberships(self):
        mapping = GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )
        user = User.objects.create_user(
            username="orbit-user",
            email="orbit-user@example.com",
            external_id="sub-orbit-user",
        )
        membership = TenantMembership.objects.create(
            user=user,
            tenant=self.orbit,
            ceph_username="orbit-user",
            role="rw",
        )
        link_authentik_user(user, groups=["orbit-users"])
        request = self.factory.delete(f"/api/admin/group-mappings/{mapping.id}/")
        force_admin(request, self.admin)

        response = admin_group_mapping_delete(request, mapping.id)

        self.assertEqual(response.status_code, 204)
        membership.refresh_from_db()
        self.assertIsNotNone(membership.access_revoked_at)
        self.assertEqual(membership.access_revocation_reason, "mapping_removed")
        self.assertEqual(membership.access_revoked_group, "orbit-users")
        self.assertTrue(User.objects.filter(id=user.id).exists())


    def test_deleting_rw_mapping_falls_back_to_verified_ro_group(self):
        rw_mapping = GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )
        GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-ext",
            role="ro",
        )
        user = User.objects.create_user(
            username="dual-role",
            email="dual-role@example.com",
            external_id="sub-dual-role",
        )
        membership = TenantMembership.objects.create(
            user=user,
            tenant=self.orbit,
            ceph_username="dual-role",
            role="rw",
        )
        bucket = Bucket.objects.create(
            tenant=self.orbit,
            name="dual-role-project",
            bucket_type=Bucket.LOCAL,
        )
        BucketPermission.objects.create(
            user=user,
            bucket=bucket,
            permission="rw",
            source="rgwsquared",
        )
        link_authentik_user(user, groups=["orbit-users", "orbit-ext"])
        request = self.factory.delete(f"/api/admin/group-mappings/{rw_mapping.id}/")
        force_admin(request, self.admin)

        response = admin_group_mapping_delete(request, rw_mapping.id)

        self.assertEqual(response.status_code, 204)
        membership.refresh_from_db()
        self.assertIsNone(membership.access_revoked_at)
        self.assertEqual(membership.role, "ro")
        self.assertEqual(bucket_permissions.get_user_permission(user, bucket), "ro")
        self.assertFalse(bucket_permissions.can_upload_file(user, bucket))

    def test_recreated_mapping_restores_only_after_matching_login(self):
        mapping = GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )
        user = User.objects.create_user(
            username="returning-user",
            email="returning@example.com",
            external_id="sub-returning",
        )
        membership = TenantMembership.objects.create(
            user=user,
            tenant=self.orbit,
            ceph_username="returning-user",
            role="rw",
        )
        link_authentik_user(user, groups=["orbit-users"])
        request = self.factory.delete(f"/api/admin/group-mappings/{mapping.id}/")
        force_admin(request, self.admin)
        admin_group_mapping_delete(request, mapping.id)
        membership.refresh_from_db()
        self.assertIsNotNone(membership.access_revoked_at)

        GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-new-users",
            role="rw",
        )
        membership.refresh_from_db()
        self.assertIsNotNone(membership.access_revoked_at)

        with patch("storage.pipeline._sync_rgwsquared_user_access", return_value=True):
            extract_tenant_info(
                None,
                {},
                {
                    "preferred_username": "returning-user",
                    "groups": ["orbit-new-users"],
                },
                user=user,
            )
        membership.refresh_from_db()
        self.assertIsNone(membership.access_revoked_at)
        self.assertEqual(membership.access_revocation_reason, "")

@override_settings(
    RGWSQUARED_URL="http://rgw",
    RGWSQUARED_USERNAME="admin",
    RGWSQUARED_PASSWORD="secret",
)
class AuthPipelineTenantPolicyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            external_id="sub-alice",
            institution="CNR - Istituto Officina dei Materiali - Trieste",
        )
        self.nffadi = Tenant.objects.create(
            code="NFFADI",
            name="NFFA-DI",
            rgwsquared_structure="NFFADI",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )
        self.orbit = Tenant.objects.create(
            code="ORBIT",
            name="Orbit",
            rgwsquared_structure="ORBIT",
        )

    def test_nffadi_role_comes_from_rgwsquared_not_group_mapping(self):
        GroupTenantMapping.objects.create(
            tenant=self.nffadi,
            authentik_group="nffa-di-users",
            role="rw",
        )
        UOMapping.objects.create(
            tenant=self.nffadi,
            uo_code="cnr-iom.ts",
            institution_name="CNR - Istituto Officina dei Materiali - Trieste",
        )

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def list_users(self, structure):
                return ["alice"]

            def get_user_info(self, structure, username):
                return {"ROBuckets": ["NFFADI:proposal-1"], "RWBuckets": []}

            def list_buckets(self, structure):
                return [{"name": "NFFADI:proposal-1", "auto": True}]

        with patch("storage.services.rgw_squared.RGWSquaredClient", Client):
            extract_tenant_info(
                None,
                {},
                {"preferred_username": "alice", "groups": ["nffa-di-users"]},
                user=self.user,
            )

        membership = TenantMembership.objects.get(user=self.user, tenant=self.nffadi)
        self.assertEqual(membership.role, "ro")
        self.assertEqual(membership.uo_code, "")

    def test_partial_multi_tenant_login_keeps_valid_tenant_when_nffadi_missing_rgw_user(self):
        GroupTenantMapping.objects.create(
            tenant=self.nffadi,
            authentik_group="nffa-di-users",
            role="rw",
        )
        GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def list_users(self, structure):
                return [] if structure == "NFFADI" else ["alice"]

            def get_user_info(self, structure, username):
                return {"ROBuckets": [], "RWBuckets": ["ORBIT:work"]}

            def list_buckets(self, structure):
                return [{"name": "ORBIT:work", "auto": True}]

        with patch("storage.services.rgw_squared.RGWSquaredClient", Client):
            extract_tenant_info(
                None,
                {},
                {"preferred_username": "alice", "groups": ["nffa-di-users", "orbit-users"]},
                user=self.user,
            )

        self.assertFalse(TenantMembership.objects.filter(user=self.user, tenant=self.nffadi).exists())
        orbit_membership = TenantMembership.objects.get(user=self.user, tenant=self.orbit)
        self.assertEqual(orbit_membership.role, "rw")
        self.assertIsNone(orbit_membership.access_revoked_at)

    def test_current_groups_revoke_stale_tenant_without_blocking_valid_tenant(self):
        GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )
        stale = TenantMembership.objects.create(
            user=self.user,
            tenant=self.nffadi,
            ceph_username="alice",
            role="rw",
        )

        with patch("storage.pipeline._sync_rgwsquared_user_access", return_value=True):
            extract_tenant_info(
                None,
                {},
                {
                    "preferred_username": "alice",
                    "groups": ["orbit-users", "unrelated-group"],
                },
                user=self.user,
            )

        stale.refresh_from_db()
        orbit = TenantMembership.objects.get(user=self.user, tenant=self.orbit)
        self.assertIsNotNone(stale.access_revoked_at)
        self.assertIsNone(orbit.access_revoked_at)

    def test_duplicate_ceph_username_is_denied_without_server_error(self):
        GroupTenantMapping.objects.create(
            tenant=self.orbit,
            authentik_group="orbit-users",
            role="rw",
        )
        other = User.objects.create_user(
            username="other",
            email="other@example.com",
            external_id="sub-other",
        )
        TenantMembership.objects.create(
            user=other,
            tenant=self.orbit,
            ceph_username="alice",
            role="rw",
        )

        with self.assertRaises(AuthForbidden):
            extract_tenant_info(
                type("Backend", (), {"name": "authentik"})(),
                {},
                {"preferred_username": "alice", "groups": ["orbit-users"]},
                user=self.user,
            )

        self.assertFalse(
            TenantMembership.objects.filter(user=self.user, tenant=self.orbit).exists()
        )


class AdminCSVUOTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = User.objects.create_user(
            username="csv-admin",
            email="csv-admin@example.com",
            external_id="sub-csv-admin",
            password="admin-password",
            is_staff=True,
            is_superuser=True,
            is_approved=True,
        )
        self.tenant = Tenant.objects.create(
            code="NFFADI",
            name="NFFA-DI",
            rgwsquared_structure="NFFADI",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )
        UOMapping.objects.create(
            tenant=self.tenant,
            uo_code="cnr-iom.ts",
            institution_name="CNR - Istituto Officina dei Materiali - Trieste",
        )
        self.rw_user = User.objects.create_user(
            username="rwuser",
            email="rwuser@example.com",
            external_id="sub-rwuser",
        )
        self.ro_user = User.objects.create_user(
            username="rouser",
            email="rouser@example.com",
            external_id="sub-rouser",
        )
        self.rw_membership = TenantMembership.objects.create(
            user=self.rw_user,
            tenant=self.tenant,
            ceph_username="rwuser",
            role="rw",
        )
        self.ro_membership = TenantMembership.objects.create(
            user=self.ro_user,
            tenant=self.tenant,
            ceph_username="rouser",
            role="ro",
            uo_code="stale-code",
        )

    def test_csv_assigns_uo_only_to_write_capable_memberships_and_clears_ro(self):
        csv_body = (
            "instrument_scientist_username,instrument_scientist_email,institution\n"
            "rwuser,rwuser@example.com,CNR - Istituto Officina dei Materiali - Trieste\n"
            "rouser,rouser@example.com,CNR - Istituto Officina dei Materiali - Trieste\n"
        )
        upload = SimpleUploadedFile("uo.csv", csv_body.encode("utf-8"), content_type="text/csv")
        request = self.factory.post(
            "/api/admin/sync/upload-csv/",
            {"file": upload},
            format="multipart",
        )
        force_authenticate(request, user=self.admin)

        class Client:
            def upload_csv(self, content):
                return {"ok": True}

        with patch("storage.views.admin._get_sync_client", return_value=Client()):
            response = admin_sync_upload_csv(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.rw_membership.refresh_from_db()
        self.ro_membership.refresh_from_db()
        self.assertEqual(self.rw_membership.uo_code, "cnr-iom.ts")
        self.assertEqual(self.rw_membership.role, "rw")
        self.assertEqual(self.ro_membership.role, "ro")
        self.assertEqual(self.ro_membership.uo_code, "")

class RecordingRGWClient:
    def __init__(self, initialized=True, users=None):
        self.initialized = initialized
        self.users = list(users or [])
        self.calls = []
        self.share_exists_at_update = None
        self.share_probe = None

    def get_structure_info(self, structure):
        self.calls.append(("structureInfo", structure))
        return {
            "initialized": self.initialized,
            "rgwintUser": {"access_key": "AK", "secret_key": "SK"},
        }

    def list_users(self, structure):
        self.calls.append(("userList", structure))
        return list(self.users)

    def create_user(self, structure, user):
        self.calls.append(("userCreate", structure, user))
        self.users.append(user)

    def create_bucket(
        self, structure, bucket_name, rw_permissions=None, ro_permissions=None, tags=None
    ):
        self.calls.append(
            (
                "bucketCreate",
                structure,
                bucket_name,
                rw_permissions or [],
                ro_permissions or [],
            )
        )

    def update_bucket(
        self, structure, bucket_name, rw_permissions=None, ro_permissions=None, tags=None
    ):
        if self.share_probe:
            bucket, user = self.share_probe
            self.share_exists_at_update = BucketPermission.objects.filter(
                bucket=bucket, user=user
            ).exists()
        self.calls.append(
            (
                "bucketUpdate",
                structure,
                bucket_name,
                rw_permissions or [],
                ro_permissions or [],
            )
        )

    def delete_bucket(self, structure, bucket_name):
        self.calls.append(("bucketDelete", structure, bucket_name))


@override_settings(
    RGWSQUARED_URL="http://rgw",
    RGWSQUARED_USERNAME="admin",
    RGWSQUARED_PASSWORD="secret",
)
class BucketLifecycleTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view_create = BucketViewSet.as_view({"post": "create"})
        self.view_delete = BucketViewSet.as_view({"delete": "destroy"})
        self.view_shares = BucketViewSet.as_view({"post": "shares"})
        self.tenant, _ = Tenant.objects.update_or_create(
            code="NFFADI",
            defaults={
                "name": "NFFA-DI",
                "rgwsquared_structure": "NFFADI",
                "access_model": Tenant.RGWSQUARED_SYNCED,
            },
        )
        GroupTenantMapping.objects.get_or_create(
            tenant=self.tenant,
            authentik_group="nffa-di-users",
            defaults={"role": "rw"},
        )
        self.owner = self.create_user("owner")
        self.target = self.create_user("target")
        self.owner_membership = TenantMembership.objects.create(
            user=self.owner,
            tenant=self.tenant,
            ceph_username="owner",
            role="rw",
            uo_code="cnr-iom.ts",
        )
        TenantMembership.objects.create(
            user=self.target,
            tenant=self.tenant,
            ceph_username="target",
            role="ro",
            uo_code="cnr-iom.ts",
        )

    def create_user(self, username):
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            external_id=f"sub-{username}",
        )

    def authenticated_request(self, method, path, user, data=None):
        request = getattr(self.factory, method)(
            path,
            data or {},
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )
        force_authenticate(request, user=user)
        return request

    def local_bucket(self):
        bucket = Bucket.objects.create(
            name="owner-cnr-iom-ts-existing",
            tenant=self.tenant,
            owner=self.owner,
            bucket_type=Bucket.LOCAL,
            is_deletable=True,
            display_name="existing",
        )
        BucketPermission.objects.create(
            bucket=bucket,
            user=self.owner,
            permission="owner",
            source="local",
        )
        return bucket

    def test_create_calls_rgwsquared_before_persisting_metadata(self):
        rgw = RecordingRGWClient(users=[])
        request = self.authenticated_request(
            "post", "/api/buckets/", self.owner, {"name": "project-1"}
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_create(request)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            rgw.calls,
            [
                ("structureInfo", "NFFADI"),
                ("userList", "NFFADI"),
                ("userCreate", "NFFADI", "owner"),
                (
                    "bucketCreate",
                    "NFFADI",
                    "owner-cnr-iom-ts-project-1",
                    ["owner"],
                    [],
                ),
            ],
        )
        bucket = Bucket.objects.get(name="owner-cnr-iom-ts-project-1")
        self.assertTrue(
            BucketPermission.objects.filter(
                bucket=bucket,
                user=self.owner,
                permission="owner",
                source="local",
            ).exists()
        )

    def test_create_duplicate_uses_display_name_in_error(self):
        Bucket.objects.create(
            name="owner-cnr-iom-ts-project-1",
            tenant=self.tenant,
            owner=self.owner,
            bucket_type=Bucket.LOCAL,
            is_deletable=True,
            display_name="project-1",
        )
        request = self.authenticated_request(
            "post", "/api/buckets/", self.owner, {"name": "project-1"}
        )

        response = self.view_create(request)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "Bucket 'project-1' already exists")
        self.assertNotIn("owner-cnr-iom-ts-project-1", response.data["error"])

    def test_rgwsquared_duplicate_create_uses_display_name_in_error(self):
        class DuplicateRGWClient(RecordingRGWClient):
            def create_bucket(
                self,
                structure,
                bucket_name,
                rw_permissions=None,
                ro_permissions=None,
                tags=None,
            ):
                self.calls.append(("bucketCreate", structure, bucket_name))
                raise RGWSquaredError(f"Bucket {bucket_name} already exists")

        rgw = DuplicateRGWClient(users=["owner"])
        request = self.authenticated_request(
            "post", "/api/buckets/", self.owner, {"name": "project-dup"}
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_create(request)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"], "Bucket 'project-dup' already exists")
        self.assertFalse(Bucket.objects.filter(display_name="project-dup").exists())

    def test_uninitialized_structure_blocks_bucket_create(self):
        rgw = RecordingRGWClient(initialized=False, users=["owner"])
        request = self.authenticated_request(
            "post", "/api/buckets/", self.owner, {"name": "project-2"}
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_create(request)

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(rgw.calls, [("structureInfo", "NFFADI")])
        self.assertFalse(Bucket.objects.filter(display_name="project-2").exists())

    def test_delete_calls_rgwsquared_before_deleting_metadata(self):
        bucket = self.local_bucket()
        rgw = RecordingRGWClient(users=["owner"])
        request = self.authenticated_request(
            "delete", f"/api/buckets/{bucket.id}/", self.owner
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_delete(request, pk=bucket.id)

        self.assertEqual(response.status_code, 204, response.data)
        self.assertEqual(
            rgw.calls,
            [
                ("structureInfo", "NFFADI"),
                ("bucketDelete", "NFFADI", "owner-cnr-iom-ts-existing"),
            ],
        )
        self.assertFalse(Bucket.objects.filter(id=bucket.id).exists())

    def test_delete_connectivity_error_keeps_metadata(self):
        bucket = self.local_bucket()

        class ConnectivityRGWClient(RecordingRGWClient):
            def delete_bucket(self, structure, bucket_name):
                super().delete_bucket(structure, bucket_name)
                raise RGWSquaredError(
                    "Inaccessible host: `10.128.2.28' at port `7480'"
                )

        rgw = ConnectivityRGWClient(users=["owner"])
        request = self.authenticated_request(
            "delete", f"/api/buckets/{bucket.id}/", self.owner
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_delete(request, pk=bucket.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(Bucket.objects.filter(id=bucket.id).exists())

    def test_delete_not_found_still_removes_metadata(self):
        bucket = self.local_bucket()

        class MissingRGWClient(RecordingRGWClient):
            def delete_bucket(self, structure, bucket_name):
                super().delete_bucket(structure, bucket_name)
                raise RGWSquaredError("Bucket not found")

        rgw = MissingRGWClient(users=["owner"])
        request = self.authenticated_request(
            "delete", f"/api/buckets/{bucket.id}/", self.owner
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_delete(request, pk=bucket.id)

        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(Bucket.objects.filter(id=bucket.id).exists())

    def test_share_updates_rgwsquared_before_persisting_permission(self):
        bucket = self.local_bucket()
        rgw = RecordingRGWClient(users=["owner", "target"])
        rgw.share_probe = (bucket, self.target)
        request = self.authenticated_request(
            "post",
            f"/api/buckets/{bucket.id}/shares/",
            self.owner,
            {"username": "target", "permission": "ro"},
        )

        with patch("storage.views.buckets._get_rgw_client", return_value=rgw):
            response = self.view_shares(request, pk=bucket.id)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(rgw.share_exists_at_update)
        self.assertIn(
            (
                "bucketUpdate",
                "NFFADI",
                "owner-cnr-iom-ts-existing",
                ["owner"],
                ["target"],
            ),
            rgw.calls,
        )
        self.assertTrue(
            BucketPermission.objects.filter(
                bucket=bucket, user=self.target, permission="ro", source="local"
            ).exists()
        )


class SyncServiceTests(TestCase):
    def test_authentik_managed_refresh_skips_unregistered_upstream_users(self):
        tenant = Tenant.objects.create(
            code="LIFESCIENCE",
            name="Life Science",
            rgwsquared_structure="LIFESCIENCE",
            access_model=Tenant.AUTHENTIK_MANAGED,
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return []

            def list_users(self, structure):
                return ["not-registered"]

            def get_user_info(self, structure, username):
                raise AssertionError("unregistered users must be skipped before userInfo")

        stats = refresh_local_cache(tenant, client=SyncClient())

        self.assertEqual(stats["users_synced"], 0)
        self.assertEqual(stats["users_skipped_unregistered"], 1)
        self.assertFalse(TenantMembership.objects.filter(tenant=tenant).exists())
        self.assertFalse(User.objects.filter(external_id__startswith="ms:").exists())

    def test_refresh_uses_bucket_list_and_user_info_without_credentials(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="NFFADI",
            defaults={
                "name": "NFFA-DI",
                "rgwsquared_structure": "NFFADI",
                "access_model": Tenant.RGWSQUARED_SYNCED,
            },
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [
                    {
                        "name": "NFFADI:275",
                        "auto": True,
                        "manual": False,
                        "RWPermissions": [],
                        "ROPermissions": [],
                    },
                    {
                        "name": "manual-bucket",
                        "auto": False,
                        "manual": True,
                        "RWPermissions": [],
                        "ROPermissions": [],
                    },
                ]

            def list_users(self, structure):
                return ["alice"]

            def get_user_info(self, structure, user):
                return {
                    "uid": "NFFADI$ext-users:alice",
                    "access_key": "USER_AK",
                    "secret_key": "USER_SK",
                    "RWBuckets": ["NFFADI:275"],
                    "ROBuckets": [],
                }

        stats = refresh_local_cache(tenant, client=SyncClient())

        self.assertTrue(stats["initialized"])
        self.assertEqual(stats["buckets_synced"], 2)
        proposal = Bucket.objects.get(name="275", tenant=tenant)
        manual = Bucket.objects.get(name="manual-bucket", tenant=tenant)
        self.assertEqual(proposal.bucket_type, Bucket.PROPOSAL)
        self.assertEqual(manual.bucket_type, Bucket.LOCAL)

        membership = TenantMembership.objects.get(tenant=tenant, ceph_username="alice")
        self.assertIsNone(membership.access_revoked_at)
        self.assertFalse(hasattr(membership, "s3_access_key"))
        self.assertFalse(hasattr(tenant, "mgmt_access_key"))
        self.assertTrue(
            BucketPermission.objects.filter(
                bucket=proposal,
                user=membership.user,
                permission="rw",
                source="rgwsquared",
            ).exists()
        )

    def test_refresh_creates_orphan_manual_bucket_without_permissions(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="GENOME",
            defaults={"name": "Genome", "rgwsquared_structure": "GENOME"},
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [
                    {
                        "name": "massimo-cuscuna-bucket01",
                        "auto": False,
                        "manual": True,
                        "RWPermissions": ["massimo.cuscuna@nanotec.cnr.it"],
                        "ROPermissions": [],
                    }
                ]

            def list_users(self, structure):
                return []

        stats = refresh_local_cache(tenant, client=SyncClient())

        bucket = Bucket.objects.get(name="massimo-cuscuna-bucket01", tenant=tenant)
        self.assertEqual(bucket.bucket_type, Bucket.LOCAL)
        self.assertEqual(stats["orphan_buckets_seen"], 1)
        self.assertEqual(BucketPermission.objects.filter(bucket=bucket).count(), 0)

    def test_refresh_preserves_local_owner_on_manual_bucket(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="NFFADI",
            defaults={"name": "NFFA-DI", "rgwsquared_structure": "NFFADI", "access_model": Tenant.RGWSQUARED_SYNCED},
        )
        owner = User.objects.create_user(
            username="alice",
            email="alice@example.com",
            external_id="sub-alice",
        )
        bucket = Bucket.objects.create(
            tenant=tenant,
            name="alice-bucket01",
            display_name="bucket01",
            bucket_type=Bucket.LOCAL,
            is_deletable=True,
            owner=owner,
        )
        BucketPermission.objects.create(
            bucket=bucket,
            user=owner,
            permission="owner",
            source="local",
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [
                    {
                        "name": "alice-bucket01",
                        "auto": False,
                        "manual": True,
                        "RWPermissions": ["alice"],
                        "ROPermissions": [],
                    }
                ]

            def list_users(self, structure):
                return []

        refresh_local_cache(tenant, client=SyncClient())

        bucket.refresh_from_db()
        self.assertEqual(bucket.display_name, "bucket01")
        owner_perm = BucketPermission.objects.get(bucket=bucket, user=owner)
        self.assertEqual(owner_perm.permission, "owner")
        self.assertEqual(owner_perm.source, "local")

    def test_refresh_clears_stale_orphan_permissions_after_user_info_sync(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="GENOME",
            defaults={"name": "Genome", "rgwsquared_structure": "GENOME"},
        )
        owner = User.objects.create_user(
            username="massimo.cuscuna@nanotec.cnr.it",
            email="massimo.cuscuna@nanotec.cnr.it",
            external_id="sub-massimo",
        )
        bucket = Bucket.objects.create(
            tenant=tenant,
            name="massimo-cuscuna-bucket01",
            display_name="massimo-cuscuna-bucket01",
            bucket_type=Bucket.LOCAL,
            is_deletable=True,
        )
        BucketPermission.objects.create(
            bucket=bucket,
            user=owner,
            permission="owner",
            source="rgwsquared",
        )
        TenantMembership.objects.create(
            user=owner,
            tenant=tenant,
            ceph_username="massimo.cuscuna@nanotec.cnr.it",
            role="ro",
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [
                    {
                        "name": "massimo-cuscuna-bucket01",
                        "auto": False,
                        "manual": True,
                        "RWPermissions": ["massimo.cuscuna@nanotec.cnr.it"],
                        "ROPermissions": [],
                    }
                ]

            def list_users(self, structure):
                return ["massimo.cuscuna@nanotec.cnr.it"]

            def get_user_info(self, structure, username):
                return {"RWBuckets": [], "ROBuckets": []}

        stats = refresh_local_cache(tenant, client=SyncClient())

        bucket = Bucket.objects.get(name="massimo-cuscuna-bucket01", tenant=tenant)
        self.assertEqual(stats["orphan_permissions_cleared"], 1)
        self.assertEqual(BucketPermission.objects.filter(bucket=bucket).count(), 0)

    def test_refresh_clears_stale_uo_from_read_only_membership(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="NFFADI",
            defaults={"name": "NFFA-DI", "rgwsquared_structure": "NFFADI", "access_model": Tenant.RGWSQUARED_SYNCED},
        )
        UOMapping.objects.create(
            tenant=tenant,
            uo_code="cnr-iom.ts",
            institution_name="CNR - Istituto Officina dei Materiali - Trieste",
        )
        user = User.objects.create_user(
            username="reader",
            email="reader@example.com",
            external_id="sub-reader",
            institution="CNR - Istituto Officina dei Materiali - Trieste",
        )
        TenantMembership.objects.create(
            user=user,
            tenant=tenant,
            ceph_username="reader",
            role="ro",
            uo_code="stale-code",
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [{"name": "NFFADI:proposal", "auto": True, "manual": False}]

            def list_users(self, structure):
                return ["reader"]

            def get_user_info(self, structure, username):
                return {"ROBuckets": ["NFFADI:proposal"], "RWBuckets": []}

        stats = refresh_local_cache(tenant, client=SyncClient())

        membership = TenantMembership.objects.get(user=user, tenant=tenant)
        self.assertEqual(membership.role, "ro")
        self.assertEqual(membership.uo_code, "")
        self.assertEqual(stats["uo_codes_cleared"], 1)

    def test_refresh_discovers_objects_without_inventing_uploader(self):
        tenant = Tenant.objects.create(
            code="LIFE",
            name="Life",
            rgwsquared_structure="LIFE",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )

        class SyncClient:
            def get_structure_info(self, structure):
                return {"initialized": True}

            def list_buckets(self, structure):
                return [{"name": "LIFE:project", "auto": True, "manual": False}]

            def list_users(self, structure):
                return ["alice"]

            def get_user_info(self, structure, username):
                return {"RWBuckets": ["LIFE:project"], "ROBuckets": []}

        class Paginator:
            def paginate(self, **kwargs):
                return [{
                    "Contents": [{
                        "Key": "legacy.dat",
                        "Size": 42,
                        "ETag": '"legacy-etag"',
                        "LastModified": timezone.now(),
                    }]
                }]

        s3 = type("S3", (), {"get_paginator": lambda self, name: Paginator()})()
        with patch("storage.services.sync_service.get_mgmt_s3_client", return_value=s3):
            stats = refresh_local_cache(tenant, client=SyncClient())

        record = FileUploadRecord.objects.get(file_key="legacy.dat")
        self.assertIsNone(record.uploaded_by)
        self.assertEqual(record.origin, FileUploadRecord.DISCOVERED)
        self.assertEqual(record.object_etag, "legacy-etag")
        self.assertEqual(stats["objects_synced"], 1)
        self.assertEqual(stats["objects_discovered"], 1)

    def test_refresh_reattributes_changed_objects_and_preserves_cache_on_list_error(self):
        tenant = Tenant.objects.create(
            code="MAT",
            name="Materials",
            rgwsquared_structure="MAT",
            access_model=Tenant.RGWSQUARED_SYNCED,
        )
        user = User.objects.create_user(
            username="alice-mat",
            email="alice@example.com",
            external_id="sub-alice-mat",
        )
        bucket = Bucket.objects.create(
            tenant=tenant,
            name="project",
            bucket_type=Bucket.PROPOSAL,
        )
        BucketPermission.objects.create(
            bucket=bucket,
            user=user,
            permission="rw",
            source="rgwsquared",
        )
        changed = FileUploadRecord.objects.create(
            bucket=bucket,
            file_key="changed.dat",
            uploaded_by=user,
            file_size=1,
            origin=FileUploadRecord.APP,
            object_etag="old-etag",
        )
        stale = FileUploadRecord.objects.create(
            bucket=bucket,
            file_key="stale.dat",
            uploaded_by=user,
            file_size=1,
            origin=FileUploadRecord.APP,
            object_etag="stale-etag",
        )

        class Paginator:
            def paginate(self, **kwargs):
                return [{
                    "Contents": [{
                        "Key": "changed.dat",
                        "Size": 99,
                        "ETag": '"new-etag"',
                        "LastModified": timezone.now(),
                    }]
                }]

        s3 = type("S3", (), {"get_paginator": lambda self, name: Paginator()})()
        stats = {"objects_synced": 0, "objects_discovered": 0,
                 "object_records_removed": 0, "object_sync_errors": 0}
        from storage.services.sync_service import _sync_authorized_bucket_objects

        with patch("storage.services.sync_service.get_mgmt_s3_client", return_value=s3):
            _sync_authorized_bucket_objects(tenant, stats, object())

        changed.refresh_from_db()
        self.assertIsNone(changed.uploaded_by)
        self.assertEqual(changed.origin, FileUploadRecord.DISCOVERED)
        self.assertFalse(FileUploadRecord.objects.filter(id=stale.id).exists())
        self.assertEqual(stats["object_records_removed"], 1)

        class FailingPaginator:
            def paginate(self, **kwargs):
                raise RuntimeError("S3 unavailable")

        failing_s3 = type(
            "FailingS3",
            (),
            {"get_paginator": lambda self, name: FailingPaginator()},
        )()
        with patch(
            "storage.services.sync_service.get_mgmt_s3_client",
            return_value=failing_s3,
        ):
            _sync_authorized_bucket_objects(tenant, stats, object())

        self.assertTrue(FileUploadRecord.objects.filter(id=changed.id).exists())
        self.assertEqual(stats["object_sync_errors"], 1)

    def test_fetch_mgmt_keys_requires_initialized_structure(self):
        tenant, _ = Tenant.objects.update_or_create(
            code="NFFADI",
            defaults={
                "name": "NFFA-DI",
                "rgwsquared_structure": "NFFADI",
                "access_model": Tenant.RGWSQUARED_SYNCED,
            },
        )

        class UninitializedClient:
            def get_structure_info(self, structure):
                return {"initialized": False}

        with self.assertRaisesRegex(RuntimeError, "not initialized"):
            fetch_mgmt_keys(tenant, client=UninitializedClient())


class CryptoServiceTests(TestCase):
    @override_settings(SECRET_KEY="test-secret-key-for-crypto-roundtrip")
    def test_encrypt_decrypt_roundtrip(self):
        from storage.services.crypto import decrypt_if_needed, encrypt_if_needed, is_encrypted

        plain = "transient-s3-secret-value"
        encrypted = encrypt_if_needed(plain)
        self.assertTrue(is_encrypted(encrypted))
        self.assertNotEqual(encrypted, plain)
        self.assertEqual(decrypt_if_needed(encrypted), plain)

    @override_settings(SECRET_KEY="crypto-tamper-test-key")
    def test_decrypt_tampered_ciphertext_returns_empty(self):
        from storage.services.crypto import decrypt_if_needed, encrypt_if_needed

        encrypted = encrypt_if_needed("credential-value")
        tampered = encrypted[:-2] + "xx"
        self.assertEqual(decrypt_if_needed(tampered), "")


@override_settings(
    RGWSQUARED_URL="http://rgw",
    RGWSQUARED_USERNAME="admin",
    RGWSQUARED_PASSWORD="secret",
)
class RGWSquaredUserCreateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="genome_user",
            email="genome@example.com",
            external_id="sub-genome",
        )
        self.tenant = Tenant.objects.create(
            code="GENOME",
            name="Genome",
            rgwsquared_structure="GENOME",
        )

    def test_auto_provision_calls_user_create_for_optional_tenant(self):
        from storage.pipeline import _sync_rgwsquared_user_access

        created = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def list_users(self, structure):
                return []

            def create_user(self, structure, ceph_username):
                created.append((structure, ceph_username))
                return {}

        with patch("storage.services.rgw_squared.RGWSquaredClient", Client):
            result = _sync_rgwsquared_user_access(
                self.user, "genome_user", self.tenant, required=False
            )

        self.assertTrue(result)
        self.assertEqual(created, [("GENOME", "genome_user")])

    def test_required_tenant_does_not_call_user_create(self):
        from storage.pipeline import _sync_rgwsquared_user_access

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def list_users(self, structure):
                return []

            def create_user(self, structure, ceph_username):
                raise AssertionError("userCreate must not run when required=True")

        with patch("storage.services.rgw_squared.RGWSquaredClient", Client):
            result = _sync_rgwsquared_user_access(
                self.user, "genome_user", self.tenant, required=True
            )

        self.assertFalse(result)


@override_settings(AUTHENTIK_ADMIN_GROUP=ADMIN_GROUP, DEBUG=True)
class ApiDocumentationAccessTests(TestCase):
    """OpenAPI schema and Swagger UI are admin-only in all environments."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="docs-admin",
            email="docs-admin@example.com",
            external_id="sub-docs-admin",
            password="unused",
            is_staff=True,
            is_approved=True,
            idp_source="authentik",
        )
        self.regular = User.objects.create_user(
            username="docs-user",
            email="docs-user@example.com",
            external_id="sub-docs-user",
            password="unused",
            is_staff=False,
            is_approved=True,
            idp_source="authentik",
        )
        link_authentik_user(self.admin)
        link_authentik_user(self.regular, groups=[])

    def test_anonymous_denied_schema_and_docs(self):
        for path in ("/api/schema/", "/api/docs/", "/api/redoc/"):
            response = self.client.get(path)
            # JWT auth advertises Bearer, so DRF may return 401 instead of 403.
            self.assertIn(response.status_code, (401, 403), path)

    def test_non_staff_denied_schema_and_docs(self):
        self.client.force_login(self.regular)
        for path in ("/api/schema/", "/api/docs/", "/api/redoc/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)

    def test_staff_session_allowed_schema_and_docs(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)
        self.assertEqual(self.client.get("/api/docs/").status_code, 200)
        self.assertEqual(self.client.get("/api/redoc/").status_code, 200)
