# Storage Cache and Redeploy Semantics

Buckets Explorer sits on three independent layers. Understanding which layer owns what prevents surprises after deploys, rollouts, or database wipes.

## Three layers

| Layer | Owns | Survives app rollout restart? | Survives `app.sh cleanup`? |
| --- | --- | --- | --- |
| **Ceph RGW** | Object bytes, physical bucket names (`TENANT/name`) | Yes | Yes |
| **RGWSquared** | Policy cache, `bucketList`, `userCreate`, transient S3 credentials | Yes | Yes |
| **Django PostgreSQL** | UI metadata, `Bucket` + `BucketPermission`, shares, upload records | Yes (PVC) | No (PVC deleted) |

The user dashboard only shows buckets where Django has a matching `BucketPermission` for the signed-in user. Admin views can list buckets synced from RGWSquared even when the user dashboard is empty.

## Deploy classes

### Class A — Config or secret change

Examples: `RGWSQUARED_URL`, OIDC client secret, `AUTHENTIK_ADMIN_GROUP`.

1. Apply updated ConfigMap/Secret.
2. `kubectl rollout restart deployment/backend`.
3. Verify `/api/health/`, OIDC login, admin sync.

PostgreSQL and RGWSquared state are unchanged. No orphan risk.

### Class B — Image rollout (routine code update)

Examples: new backend/frontend image, `app.sh rebuild`.

1. Build and push images (or use local registry in dev).
2. Rollout restart backend and/or frontend.
3. Backend pod runs `migrate` on start; readiness probe gates traffic.

PostgreSQL PVC persists. Django cache repopulates from existing rows plus the next admin sync. No orphan risk for buckets that were already in PostgreSQL.

### Class C — PostgreSQL PVC wipe or fresh install

Examples: `app.sh cleanup`, intentional DB reset, lost PVC.

PostgreSQL is empty after redeploy. Ceph and RGWSquared still hold prior buckets and policies.

**What admin sync restores:**

- `rgwsquared_synced` tenants restore upstream users, memberships, roles,
  proposal buckets, and grants from `userInfo`.
- `authentik_managed` tenants never create placeholder users during refresh;
  unknown upstream identities are counted as `users_skipped_unregistered`.
- Objects inside buckets with trusted permissions are inventoried from Ceph.
  Objects not uploaded through the app have unknown uploader provenance.
- Manual `bucketList` items reappear as Django `Bucket` rows for **admin inventory only** (`bucket_type=local`, flagged **ORPHAN** when not webapp-created).

**What sync does not do (by design):**

- Orphan buckets are **never** granted `BucketPermission` rows — users do not see them on the dashboard.
- `display_name` for orphans stays the full RGW id (e.g. `massimo-cuscuna-bucket01`), not a short project id like `bucket01`. Only webapp bucket create sets the short name.
- `source=local` shares and owner records created only in Django are gone until users recreate or re-share.
- Object inventory does not grant bucket access. A `BucketPermission` must come
  from trusted RGWSquared state or a surviving app-owned local bucket.
- Object data in Ceph is never auto-deleted. Remove orphan buckets explicitly via Admin Panel delete (RGWSquared `bucketDelete`) or operator curl.

## Bucket delete semantics

Admin and user bucket delete follow the same order documented in [rgwsquared-api.md](rgwsquared-api.md):

1. Call RGWSquared `bucketDelete` (storage and policy teardown).
2. Delete the Django `Bucket` row only after step 1 succeeds, or when RGWSquared reports the bucket is already absent.

There is **no** database-only delete path in the UI or API. If `bucketDelete` fails (for example RGWSquared cannot reach its internal Ceph endpoint), the Django row stays and the admin must fix RGWSquared connectivity before retrying.

## Orphan buckets

An **orphan** is a manual RGW bucket that exists in RGWSquared/Ceph but was not created through the webapp (`source=local` owner permission missing). Researchers must never see orphans. Admins see them in the Buckets view with an **ORPHAN** badge and can delete them from the panel.

**Prevention:** always create research buckets through the webapp UI. Do not create manual buckets in RGWSquared except for emergencies, and delete orphans promptly after a database wipe.

## Operator checklist after Class C

1. Redeploy app manifests (`app.sh deploy` or production equivalent).
2. Restore each tenant's explicit access model and Authentik group mappings
   before refresh.
3. Sign in to Admin Panel and run **Tenants → Refresh** per tenant. Confirm the
   returned user, bucket, permission, and object counters.
4. Require RGWSquared-synced users to show authorized unless explicitly revoked,
   and require Authentik-managed unknown users to remain skipped.
5. Review buckets flagged **ORPHAN** — these exist in RGW but are invisible to users.
6. Delete orphans that should not exist (Admin Panel **Delete** or RGWSquared `bucketDelete`).
7. Tell affected researchers to recreate buckets through the webapp if they need access again.

## Related docs

- [production-deployment.md](production-deployment.md) — production apply order and update flows
- [bucket-explorer-maintainer-guide.md](bucket-explorer-maintainer-guide.md) — architecture and sync pipeline
