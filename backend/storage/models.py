"""Core storage models for users, tenants, buckets, permissions, and mappings."""

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """User model with federated identity attributes."""

    external_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique identifier from the identity provider (OAuth2 'sub').",
    )

    idp_source = models.CharField(
        max_length=100,
        default="authentik",
        db_index=True,
        help_text="Identity provider that authenticated this user.",
    )

    institution = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text="Institution affiliation from OAuth2 claims.",
    )

    department = models.CharField(
        max_length=255,
        blank=True,
        help_text="Department within institution (e.g., 'Physics', 'Biology')",
    )

    affiliation_status = models.CharField(
        max_length=50,
        blank=True,
        choices=[
            ("faculty", "Faculty"),
            ("staff", "Staff"),
            ("student", "Student"),
            ("affiliate", "Affiliate"),
            ("guest", "Guest"),
        ],
        help_text="User's role/status at institution",
    )

    orcid = models.CharField(
        max_length=19,  # Format: 0000-0002-1825-0097
        blank=True,
        unique=True,
        null=True,
        help_text="ORCID identifier for researchers",
    )

    profile_picture_url = models.URLField(
        blank=True, help_text="URL to user's profile picture from IdP"
    )

    last_idp_sync = models.DateTimeField(
        auto_now=True, help_text="Last time user profile was synced from IdP"
    )

    is_approved = models.BooleanField(
        default=True, help_text="Whether user is approved to access the system"
    )

    notes = models.TextField(blank=True, help_text="Admin notes about this user")

    display_username = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        unique=True,
        help_text=(
            "Collision-safe display name derived from email prefix at first login. "
            "Used for UI display, sharing, and local bucket naming. "
            "Collisions resolved with -2, -3, etc. suffix."
        ),
    )

    email = models.EmailField(
        help_text=(
            "Email address from IdP (OAuth2 'email' claim). Not an identity key: "
            "distinct OIDC subjects may share an email address."
        )
    )

    class Meta:
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        indexes = [
            models.Index(fields=["external_id", "idp_source"]),
            models.Index(fields=["institution"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        """String representation shows name and institution"""
        if self.institution:
            return f"{self.get_full_name()} ({self.institution})"
        return self.get_full_name() or self.username

    def get_full_name(self):
        """Override to provide fallback"""
        full_name = super().get_full_name()
        if not full_name:
            return self.username
        return full_name

    @property
    def display_name(self):
        """Stable display name: display_username if set, else email prefix, else username."""
        if self.display_username:
            return self.display_username
        if self.email and "@" in self.email:
            return self.email.split("@")[0]
        return self.username

    @property
    def is_federated(self):
        """Whether this account came from a federated identity provider."""
        return bool(self.external_id)

    @property
    def can_access_system(self):
        """Single account gate used by API permission checks."""
        return self.is_active and self.is_approved

    def get_identity_summary(self):
        """Identity fields safe to include in audit/debug logs."""
        return {
            "external_id": self.external_id,
            "idp_source": self.idp_source,
            "institution": self.institution,
            "department": self.department,
            "affiliation_status": self.affiliation_status,
            "email": self.email,
        }


class Tenant(models.Model):
    """A research project or laboratory that gets its own S3 namespace.

    Each tenant maps to a structure in RGWSquared and a local naming prefix.
    RGWSquared owns Ceph users, bucket lifecycle, and S3 credential issuance.
    """

    RGWSQUARED_SYNCED = "rgwsquared_synced"
    AUTHENTIK_MANAGED = "authentik_managed"
    ACCESS_MODEL_CHOICES = [
        (RGWSQUARED_SYNCED, "RGWSquared-synced"),
        (AUTHENTIK_MANAGED, "Authentik-managed"),
    ]

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=200)
    rgwsquared_structure = models.CharField(
        max_length=50,
        blank=True,
        help_text="Structure name for RGWSquared API calls (e.g., 'NFFADI')",
    )
    bucket_name_prefix = models.CharField(
        max_length=50,
        blank=True,
        help_text="Prefix for local research bucket names (e.g., 'nffa-di')",
    )
    access_model = models.CharField(
        max_length=24,
        choices=ACCESS_MODEL_CHOICES,
        default=AUTHENTIK_MANAGED,
        help_text=(
            "RGWSquared-synced tenants import users and roles from RGWSquared; "
            "Authentik-managed tenants register users and roles at OIDC login."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "tenants"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class TenantMembership(models.Model):
    """Links a user to a tenant with role and RGWSquared username.

    ceph_username = preferred_username from Authentik JWT.
    This is the subuser name in Ceph: {TENANT}$ext-users:{ceph_username}
    """

    ROLE_CHOICES = [
        ("ro", "Read-Only"),
        ("rw", "Read-Write"),
        ("admin", "Admin"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="memberships"
    )
    ceph_username = models.CharField(max_length=200)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="ro")
    uo_code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Operational unit code for NFFADI bucket naming (e.g., 'cnr-iom.ts')",
    )
    is_active = models.BooleanField(default=True)
    access_revoked_at = models.DateTimeField(null=True, blank=True)
    access_revocation_reason = models.CharField(
        max_length=32,
        blank=True,
        choices=[
            ("mapping_removed", "Authentik group mapping removed"),
            ("claim_missing", "Mapped Authentik group missing at login"),
        ],
    )
    access_revoked_group = models.CharField(
        max_length=200,
        blank=True,
        help_text="Mapped Authentik group whose loss caused the revocation.",
    )

    class Meta:
        db_table = "tenant_memberships"
        unique_together = [("user", "tenant")]
        indexes = [
            models.Index(fields=["tenant", "ceph_username"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "ceph_username"],
                condition=models.Q(is_active=True),
                name="unique_active_ceph_username_per_tenant",
            ),
        ]

    def __str__(self):
        return f"{self.ceph_username} @ {self.tenant.code} ({self.role})"

    @property
    def ceph_subuser_uid(self):
        """Full Ceph subuser UID: {TENANT}$ext-users:{ceph_username}"""
        return f"{self.tenant.code}$ext-users:{self.ceph_username}"


class Bucket(models.Model):
    """S3 bucket metadata.

    Two types:
    - proposal: created by RGWSquared sync, undeletable, named by instrument ID
    - local: created by users for their own research, deletable by owner

    name = bare S3 name as seen by tenanted users (e.g., "275", not "NFFADI/275").
    display_name = human-readable with tenant prefix (e.g., "NFFADI/275").
    """

    PROPOSAL = "proposal"
    LOCAL = "local"
    TYPE_CHOICES = [(PROPOSAL, "Proposal"), (LOCAL, "Local Research")]

    name = models.CharField(
        max_length=255, help_text="Bare S3 bucket name (no tenant prefix)"
    )
    display_name = models.CharField(
        max_length=300, blank=True, help_text="Human-readable name with prefix"
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="buckets")
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_buckets",
        help_text="Creator. Null for proposal buckets synced from RGWSquared.",
    )
    bucket_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    is_deletable = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "buckets"
        unique_together = [("name", "tenant")]
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "bucket_type"]),
        ]

    def __str__(self):
        return self.display_name or f"{self.tenant.code}/{self.name}"

    def save(self, *args, **kwargs):
        if not self.display_name:
            self.display_name = self.name
        if self.bucket_type == self.PROPOSAL:
            self.is_deletable = False
        super().save(*args, **kwargs)


class BucketPermission(models.Model):
    """Per-user permission on a bucket.

    source='rgwsquared': synced from RGWSquared userInfo (ROBuckets/RWBuckets)
    source='local': granted by bucket owner for shared local research buckets
    """

    PERMISSION_CHOICES = [
        ("ro", "Read-Only"),
        ("rw", "Read-Write"),
        ("owner", "Owner"),
    ]
    SOURCE_CHOICES = [
        ("rgwsquared", "From RGWSquared"),
        ("local", "Local Sharing"),
    ]

    bucket = models.ForeignKey(
        Bucket, on_delete=models.CASCADE, related_name="permissions"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="bucket_permissions"
    )
    permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES)
    source = models.CharField(
        max_length=15, choices=SOURCE_CHOICES, default="rgwsquared"
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bucket_permissions"
        unique_together = [("bucket", "user")]

    def __str__(self):
        return f"{self.user.username} → {self.bucket} ({self.permission})"


class FileUploadRecord(models.Model):
    """Tracks who uploaded a file. Needed for shared buckets where
    shared RW users can only delete their own files."""

    APP = "app"
    DISCOVERED = "discovered"
    ORIGIN_CHOICES = [
        (APP, "Uploaded through Buckets Explorer"),
        (DISCOVERED, "Discovered in object storage"),
    ]

    bucket = models.ForeignKey(
        Bucket, on_delete=models.CASCADE, related_name="upload_records"
    )
    file_key = models.CharField(max_length=1024)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    file_size = models.BigIntegerField(default=0)
    origin = models.CharField(max_length=12, choices=ORIGIN_CHOICES, default=APP)
    object_etag = models.CharField(max_length=255, blank=True)
    object_last_modified = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "file_upload_records"
        unique_together = [("bucket", "file_key")]

    def __str__(self):
        return f"{self.file_key} by {self.uploaded_by}"


class UOMapping(models.Model):
    """Maps institution name to operational unit code for bucket naming.

    NFFADI local research buckets: nffa-di_{uo_code}_{project_id}
    Example: "CNR - Istituto Officina dei Materiali - Trieste" → "cnr-iom.ts"
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="uo_mappings"
    )
    institution_name = models.CharField(max_length=300)
    uo_code = models.CharField(max_length=50)

    class Meta:
        db_table = "uo_mappings"
        unique_together = [("tenant", "uo_code")]

    def __str__(self):
        return f"{self.uo_code} ({self.institution_name[:50]})"


class GroupTenantMapping(models.Model):
    """Maps an Authentik group name to a tenant with an access role.

    Used for @areasciencepark.it users who may belong to multiple tenants.
    The JWT 'groups' claim is matched against this table to determine
    which tenants a user can access and with what role.

    Each tenant can have at most one RW group and one RO group:
    - RW group (role="rw"): e.g. "lage-users" — grants read-write access
    - RO group (role="ro"): e.g. "lage-ext"  — grants read-only access
    - NFFADI uses a single group "nffa-di-users" with role="rw"; RO is determined
      at login time by inspecting the user's actual bucket permissions in RGWSquared.

    Example:
      "nffa-di-users" → NFFADI, role="rw"
      "lage-users"   → LAGE,   role="rw"
      "lage-ext"     → LAGE,   role="ro"
    """

    ROLE_CHOICES = [
        ("rw", "Read-Write"),
        ("ro", "Read-Only"),
    ]

    authentik_group = models.CharField(
        max_length=200,
        unique=True,
        help_text="Authentik group name. Each group name can only map to one tenant.",
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="group_mappings"
    )
    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default="rw",
        help_text=(
            "rw = this group grants read-write access; "
            "ro = this group grants read-only access"
        ),
    )

    class Meta:
        db_table = "group_tenant_mappings"
        unique_together = [("tenant", "role")]  # max one rw group + one ro group per tenant

    def __str__(self):
        return f"{self.authentik_group} → {self.tenant.code} ({self.role})"


class FileNameRule(models.Model):
    """Required substring for filenames uploaded to a tenant.

    A file DEVIATES if its name contains NONE of the tenant's rules.
    Zero rules = no constraints; all filenames are accepted.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="file_name_rules"
    )
    substring = models.CharField(max_length=200)

    class Meta:
        db_table = "file_name_rules"
        unique_together = [("tenant", "substring")]

    def __str__(self):
        return f"{self.tenant.code}: must contain '{self.substring}'"


class TenantDocument(models.Model):
    """Per-tenant Markdown document shown to users as a configurable nav tab.

    When is_visible=False or content is empty, no nav tab appears for users.
    Admin can replace, clear, or delete this record at any time.
    """

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="document"
    )
    tab_name = models.CharField(
        max_length=100,
        default="Documentation",
        help_text="Label shown in the user nav bar",
    )
    content = models.TextField(blank=True, help_text="Markdown source")
    is_visible = models.BooleanField(
        default=False,
        help_text="Show the tab to users only when True and content is non-empty",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenant_documents"

    def __str__(self):
        return f"{self.tenant.code} doc ({self.tab_name}, visible={self.is_visible})"
