# Deploying to Production

## Why the manifests look the way they do

`k8s/manifests/` is divided into `app/` and `infra/` because in production these two layers have different owners.

At AREA Science Park, Authentik is a shared identity platform administered by the infrastructure team — not by the team operating Buckets Explorer. The `infra/` directory contains Authentik and its dependencies (PostgreSQL, Redis). The `app/` directory contains the webapp: Django backend, React frontend, PostgreSQL. The boundary between them is intentional and reflects a real organizational split.

The development environment reproduces this boundary explicitly so that deploying to production is a **subtraction problem**: you hand off `infra/` responsibility to whoever administers Authentik, and apply only `app/` yourself. The scripts and structure you use during development are close enough to production that there are no surprises at deploy time.

For the full development topology (virtual machines, IP layout, networking), see [dev-environment-overview.md](dev-environment-overview.md).

---

## Before you start

You need:

- Access to the existing `bucket-explorer` namespace; do not delete platform-owned namespace controls
- A **kubeconfig** for that cluster on the machine where you run `kubectl` and apply manifests (see [Kubernetes access (kubeconfig)](#kubernetes-access-kubeconfig))
- An Authentik instance already running, **or** willingness to deploy one (see [Scenario B](#scenario-b--no-existing-authentik))
- OIDC client credentials from the Authentik administrator
- Ceph RGW endpoint URL and RGWSquared service credentials
- A container registry reachable by your cluster nodes (see [Container image ownership](#container-image-ownership))

---

## Kubernetes access (kubeconfig)

Production operators need a kubeconfig file issued by the platform or cluster team. It is the same concept as in development: a file that tells `kubectl` **where** the API server is and **how** to authenticate.

Typical setup on the operator workstation:

```bash
# Path chosen by your platform team — store outside Git, mode 600
export KUBECONFIG=/absolute/path/to/prod-kubeconfig.yaml

kubectl config current-context
kubectl -n bucket-explorer get resourcequota
kubectl auth can-i create deployments -n bucket-explorer
```

All three commands must succeed before you apply manifests or roll out image updates.

| Check | Pass criteria |
|-------|----------------|
| `kubectl config current-context` | Points at the intended production cluster (not a dev context) |
| `kubectl -n bucket-explorer get resourcequota` | Namespace and platform quota are reachable |
| `kubectl auth can-i create deployments -n bucket-explorer` | `yes` |

**Network access:** Production API servers are usually reachable only from an institutional VPN or bastion — the same role the dev SSH tunnel plays in [dev-environment-setup.md](dev-environment-setup.md#step-8-k8s-api-access-tunnel), but with your organization's production networking instead of `localhost:16443`.

**Session habit:** Export `KUBECONFIG` in every shell (or merge the production context into `~/.kube/config` with `kubectl config use-context`). Deployment scripts and CI jobs must set `KUBECONFIG` explicitly when they are not using the default kubeconfig path.

**Security:** Never commit kubeconfig files. Treat them like passwords (file mode `600`, store in a secrets manager or secure home directory).

---

## Scenario A — Authentik is already deployed

This is the normal case at AREA Science Park. The Authentik administrator creates an OAuth2 provider for Buckets Explorer and gives you:

| Credential | Used as |
|---|---|
| Client ID | `OIDC_CLIENT_ID` in the ConfigMap |
| Client secret | `oidc-client-secret` in the Secret |
| Application slug | `OIDC_APPLICATION_SLUG` in the ConfigMap |
| Internal Authentik service URL | `AUTHENTIK_URL` in the ConfigMap |
| Public Authentik URL | `AUTHENTIK_EXTERNAL_URL` in the ConfigMap |

The redirect URI to register in Authentik is: `https://<your-domain>/api/oauth/complete/authentik/`

### 1. Prepare configuration

Use `k8s/env/prod/backend-config.yaml` as the public template and keep real
values in a mode-600, gitignored `backend-config.local.{yaml,json}` file.

| Key | Dev default | Production value |
|---|---|---|
| `DJANGO_DEBUG` | `True` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `*` | Your domain(s), comma-separated |
| `AUTHENTIK_URL` | In-cluster dev URL | Internal Authentik service URL |
| `AUTHENTIK_EXTERNAL_URL` | `http://localhost:9000` | `https://<authentik-public-domain>` |
| `OIDC_CLIENT_ID` | placeholder | From Authentik admin |
| `OIDC_APPLICATION_SLUG` | placeholder | From Authentik admin |
| `S3_ENDPOINT` | placeholder | `https://<ceph-rgw-endpoint>` |
| `S3_VERIFY_SSL` | `False` | `True` (unless using a self-signed cert) |
| `RGWSQUARED_URL` | placeholder | `https://<rgwsquared-endpoint>` |
| `OAUTH_LOG_LEVEL` | `DEBUG` | `INFO` |

Use `k8s/env/prod/app-secrets.yaml` as the public backend Secret template.

| Key | How to generate |
|---|---|
| `django-secret-key` | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `database-password` | `openssl rand -base64 32` |
| `oidc-client-secret` | From the Authentik admin |
| `rgwsquared-username` | From the RGWSquared admin |
| `rgwsquared-password` | From the RGWSquared admin |

Keep the real backend Secret, ConfigMap, and TLS Secret in gitignored production
local files. Copying them into the repository directory is safe only because
`k8s/env/**/*.local.{yaml,json}` is ignored; require file mode `600` and verify
with `git status --ignored` before deployment. Never apply the tracked
`tls-secret.yaml` placeholder.

For an existing production deployment, export these three live resources after
removing runtime metadata so a fresh application deployment is reconstructible.

### 2. Apply the manifests

Production operators apply `k8s/manifests/app/` manually and provide validated
production-local Secret/ConfigMap files. Never invoke `k8s/app.sh` and never
apply `k8s/manifests/infra/`; production Authentik is external.

Apply in order. Each step waits for its dependency before the next one starts.

```bash
export NS=bucket-explorer
export NEW_BACKEND_IMAGE='ghcr.io/<owner>/buckets-explorer-backend@sha256:<digest>'
export NEW_FRONTEND_IMAGE='ghcr.io/<owner>/buckets-explorer-frontend@sha256:<digest>'

# The namespace and platform-owned quota/RBAC already exist. Do not recreate or delete them.
kubectl -n "$NS" get resourcequota

# Private registry pull credentials
GHCR_AUTH_FILE=$(mktemp)
trap 'rm -f "$GHCR_AUTH_FILE"' EXIT
rm -f "$GHCR_AUTH_FILE"
printf '%s' "$GHCR_TOKEN" | podman login --authfile "$GHCR_AUTH_FILE" \
  ghcr.io -u '<owner>' --password-stdin
kubectl -n "$NS" create secret generic ghcr-pull-secret \
  --from-file=.dockerconfigjson="$GHCR_AUTH_FILE" \
  --type=kubernetes.io/dockerconfigjson --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" patch serviceaccount default --type=strategic \
  -p '{"imagePullSecrets":[{"name":"ghcr-pull-secret"}]}'

# Validated runtime configuration and TLS material
kubectl apply -f k8s/env/prod/app-secrets.local.json
kubectl apply -f k8s/env/prod/backend-config.local.json
kubectl apply -f k8s/env/prod/tls-secret.local.json

# PostgreSQL — must be ready before the backend starts
kubectl apply -f k8s/manifests/app/01-django-postgres.yaml
kubectl -n "$NS" rollout status deployment/django-postgres --timeout=300s

# Backend and service; final deployment identity is the immutable digest
kubectl apply -f k8s/manifests/app/02-backend.yaml
kubectl -n "$NS" set image deployment/backend backend="$NEW_BACKEND_IMAGE"
kubectl -n "$NS" rollout status deployment/backend --timeout=300s

# Frontend and service
kubectl apply -f k8s/manifests/app/03-frontend.yaml
kubectl -n "$NS" set image deployment/frontend frontend="$NEW_FRONTEND_IMAGE"
kubectl -n "$NS" rollout status deployment/frontend --timeout=300s

# Public route only after both workloads are healthy
kubectl apply -f k8s/manifests/app/04-ingress.yaml
```

Set `AUTHENTIK_ADMIN_GROUP` in your production ConfigMap (default: `buckets-explorer-admin`) and assign admin users to that group in Authentik.

Backend startup runs Django migrations, loads UO mapping fixtures, purges legacy local staff users, and collects static files automatically. Watch the logs during the first deploy to confirm each step completes cleanly before gunicorn starts:

```bash
kubectl logs -n bucket-explorer deployment/backend -f
```

Expected sequence: `migrate` → `load_uo_mappings` → `purge_local_staff_users` → `collectstatic` → gunicorn listening on port 8000.

### 3. Configure Ingress

`k8s/manifests/app/04-ingress.yaml` declares the production host,
`haproxy-4` IngressClass, and TLS Secret
`wildcard.areasciencepark.it-certs`. Apply it only after the frontend and backend
are ready and after verifying that the TLS Secret exists.

If your cluster routes differently (NodePort, LoadBalancer, or a custom HAProxy configuration), adapt accordingly. The services you need to expose:

| Service | Port | What it serves |
|---|---|---|
| `frontend-service` | 80 | React SPA + proxied `/api/*` calls |
| `backend-service` | 8000 | Django REST API (internal; reached via frontend nginx) |

Only `frontend-service` needs to be externally accessible. The backend is proxied by nginx inside the pod.

### 4. Verify

| Check | Expected result |
|---|---|
| `curl https://<domain>/api/health/` | `{"status": "ok"}` |
| `https://<domain>/admin/login` | Admin panel loads; log in with Authentik (admin group member) |
| Click "Login with Authentik" | OIDC redirect to Authentik, JWT returned after login |
| Activate a tenant in admin panel | Buckets and users sync from RGWSquared |

---

## Scenario B — No existing Authentik

If deploying a fully standalone instance, apply `k8s/manifests/infra/` first to start Authentik and its dependencies, then follow Scenario A for the webapp. The development script `k8s/infra.sh deploy` automates this for the dev topology; for production, adapt the Ingress and DNS setup to your environment.

After Authentik is running:

1. Log in to the Authentik admin UI and create an OAuth2 provider for Buckets Explorer
2. Note the client ID, client secret, and application slug
3. Register the redirect URI: `https://<domain>/api/oauth/complete/authentik/`
4. Proceed with Scenario A

---

## Container image ownership

The manifests reference images at `ghcr.io/luisfpal/buckets-explorer-{backend,frontend}:latest`. That registry was used during development for full control and rapid iteration. A team deploying Buckets Explorer on their own infrastructure must publish their own images and update the manifests accordingly.

To take ownership:

1. Set the registry owner and immutable release tag in the operator shell:
   ```bash
   export GHCR_OWNER=<GitHub username or organisation>
   export RELEASE_TAG=prod-$(date -u +%Y%m%dT%H%M%SZ)
   ```
2. Authenticate manually with a token that can read and write packages:
   ```bash
   printf '%s' "$GHCR_TOKEN" | podman login ghcr.io \
     -u "$GHCR_OWNER" --password-stdin
   ```
3. Build and push unique release tags plus compatibility `latest` tags with
   `podman`. Pull the release tags back and deploy their exact repository
   digests, never an unverified local image digest.

Production deployment is manual. Do not invoke `k8s/app.sh`; it is the
development-environment operator script.

For production deployments, pin `repository@sha256:digest`. The `latest` tag is
only a compatibility reference and is not a deployment identity.

---

## Resource sizing

Manifest values in `k8s/manifests/app/` are sized for the AREA Science Park production namespace quota (`limits.cpu: 8`, `limits.memory: 32Gi`). The app is IO-bound (OIDC, RGWSquared, S3); keep 2 gunicorn workers per pod and scale replicas if load grows.

| Component | requests (CPU · RAM) | limits (CPU · RAM) | Notes |
|---|---|---|---|
| Backend | 250m · 512Mi | 1000m · 1Gi | Startup migrate/collectstatic needs memory headroom |
| PostgreSQL | 100m · 256Mi | 500m · 512Mi | Metadata-only DB; bump limit to 1Gi if tenant count grows 10×+ |
| Frontend (nginx) | 50m · 64Mi | 200m · 256Mi | Static + API proxy; rarely the bottleneck |

Total pod limits: ~1700m CPU · ~1792Mi RAM, leaving quota headroom for deploy-time temp pods. Monitor with `kubectl top pods -n bucket-explorer`.

---

## Production mental model

You typically own only the `bucket-explorer` namespace. Cluster admins own Authentik, DNS, TLS, and public routing.

| Service | Type | Purpose |
| --- | --- | --- |
| `frontend-service` | ClusterIP | Public traffic should route here (port 80) |
| `backend-service` | ClusterIP | Internal API; reached via frontend nginx |
| `django-postgres` | ClusterIP | Application PostgreSQL |

Storage boundaries: Django holds metadata; RGWSquared owns bucket policy; Ceph RGW stores objects.

**Golden rule:** every `kubectl apply` is followed by a verification check. If a check fails, stop and fix before continuing.

Before building production images, run the Django tests, `manage.py check`,
migration consistency check, frontend production build, shell validation,
manifest dry-runs, secret scan, and `git diff --check`. Do not use `app.sh` for
production preparation or deployment.

---

## Prepare production configuration

Create or capture `app-secrets.local.json`, `backend-config.local.json`, and
`tls-secret.local.json` under `k8s/env/prod/`, all mode `600`. They are ignored
by Git. The corresponding tracked YAML files are placeholders and documentation,
not production values.

### Expected secret keys

```text
database-password
django-secret-key
oidc-client-secret
rgwsquared-password
rgwsquared-username
```

Admin access uses **Authentik** (`AUTHENTIK_ADMIN_GROUP` in the ConfigMap, default `buckets-explorer-admin`). There is no local Django admin password secret.

### Preflight checks

```bash
export KUBECONFIG=/absolute/path/to/prod-kubeconfig.yaml
export NS=bucket-explorer

kubectl config current-context
kubectl -n "$NS" get resourcequota
kubectl auth can-i create deployments -n "$NS"

chmod 600 k8s/env/prod/app-secrets.local.json \
  k8s/env/prod/backend-config.local.json \
  k8s/env/prod/tls-secret.local.json

# No unfilled placeholders
grep -R "REPLACE_WITH_PROD" k8s/env/prod/*.local.json && exit 1 || true

kubectl apply --dry-run=client --validate=false -f k8s/env/prod/app-secrets.local.json
kubectl apply --dry-run=client --validate=false -f k8s/env/prod/backend-config.local.json
kubectl apply --dry-run=client --validate=false -f k8s/env/prod/tls-secret.local.json
kubectl apply --dry-run=client --validate=false -f k8s/manifests/app/04-ingress.yaml
kubectl apply --dry-run=server -f k8s/manifests/app/04-ingress.yaml
git status --ignored --short k8s/env/prod
```

---

## Updating after code changes

Changing source files does not affect running pods. You must **build a new image → push → restart** the deployment.

### Decision table

| Change | Rebuild image? | Action |
| --- | --- | --- |
| Python/Django code, migrations | Backend | Build backend → push → `kubectl rollout restart deployment/backend` |
| React/TypeScript or `nginx.conf` | Frontend | Build frontend → push → restart frontend |
| Both | Both | Build and restart both |
| ConfigMap env vars only | No | `kubectl apply` ConfigMap → restart backend |
| Secret values (except DB password) | No | `kubectl apply` Secret → restart backend |
| `database-password` | No | Change live PostgreSQL role password, apply Secret, restart (see below) |

Production deployments are pinned to immutable digests. `latest` is updated only
for compatibility and must reference the same content as the release tag.

### Build and push (example)

```bash
export NS=bucket-explorer
export OWNER=<owner>
export RELEASE_TAG=prod-$(date -u +%Y%m%dT%H%M%SZ)
export BACKEND_REPO="ghcr.io/$OWNER/buckets-explorer-backend"
export FRONTEND_REPO="ghcr.io/$OWNER/buckets-explorer-frontend"

# Authenticate to GHCR; never put the token directly in a command argument.
printf '%s' "$GHCR_TOKEN" | podman login ghcr.io -u "$OWNER" --password-stdin

podman build -t "$BACKEND_REPO:$RELEASE_TAG" -t "$BACKEND_REPO:latest" \
  -f backend/Containerfile backend/
podman build -t "$FRONTEND_REPO:$RELEASE_TAG" -t "$FRONTEND_REPO:latest" \
  -f frontend/Containerfile frontend/

podman push "$BACKEND_REPO:$RELEASE_TAG"
podman push "$BACKEND_REPO:latest"
podman push "$FRONTEND_REPO:$RELEASE_TAG"
podman push "$FRONTEND_REPO:latest"

# Pull the published tags back before resolving repository digests.
podman pull "$BACKEND_REPO:$RELEASE_TAG"
podman pull "$FRONTEND_REPO:$RELEASE_TAG"
podman image inspect "$BACKEND_REPO:$RELEASE_TAG" --format '{{json .RepoDigests}}'
podman image inspect "$FRONTEND_REPO:$RELEASE_TAG" --format '{{json .RepoDigests}}'
```

Expected backend logs: `migrate` → `load_uo_mappings` → `purge_local_staff_users` → `collectstatic` → gunicorn.

Deploy only the resolved `repository@sha256:digest` values:

```bash
kubectl -n "$NS" set image deployment/backend \
  backend="$BACKEND_REPO@sha256:<registry-digest>"
kubectl -n "$NS" rollout status deployment/backend --timeout=300s
kubectl -n "$NS" set image deployment/frontend \
  frontend="$FRONTEND_REPO@sha256:<registry-digest>"
kubectl -n "$NS" rollout status deployment/frontend --timeout=300s
kubectl -n "$NS" get pods \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .status.containerStatuses[*]}{.imageID}{" "}{end}{"\n"}{end}'
```

### Post-update verification

```bash
export APP_HOST=<production-domain>

curl -fsS  "https://$APP_HOST/api/health/"
curl -fsSI "https://$APP_HOST/api/oauth/login/authentik/" | grep -i location
```

- Health returns `{"status":"ok"}`.
- OIDC `Location` header should show `redirect_uri=https://` (not `http://`).
- Admin Panel: open `/admin/login`, sign in with Authentik (member of admin group).

---

## Routine updates (Class A and B)

Most production changes are safe rollouts that keep the PostgreSQL PVC intact.

| Class | Examples | Data risk |
| --- | --- | --- |
| **A** | ConfigMap/Secret change, OIDC rotation | None — restart backend after apply |
| **B** | New backend/frontend image, migration-only schema change | Low — PVC survives; run post-deploy health checks |

After any Class A or B update:

1. Confirm pods are `Running` and readiness probes pass.
2. Hit `/api/health/` on the public URL.
3. Spot-check user login and Admin Panel sync.

See [storage-cache-and-redeploy.md](storage-cache-and-redeploy.md) for the three-layer model.

---

## After database loss (Class C)

Deleting the PostgreSQL PVC wipes Django metadata only. Ceph RGW and
RGWSquared keep existing buckets and policies. Production operations are always
manual; never run the development `app.sh` against a production context.

### Fresh application reset with intentionally disposable metadata

Use this procedure only when application metadata is explicitly disposable and
must start empty. It deletes every application-owned resource but preserves the
namespace, resource quota, RBAC, service account, Authentik, RGWSquared, Ceph,
DNS, and Ingress controller.

Before deletion, require all three mode-600 production-local files, verified
immutable image digests, a valid TLS certificate/private-key pair, and clean
client/server manifest dry-runs. Stop on the first failed command.

```bash
set -euo pipefail
export NS=bucket-explorer
export NEW_BACKEND_IMAGE='ghcr.io/<owner>/buckets-explorer-backend@sha256:<digest>'
export NEW_FRONTEND_IMAGE='ghcr.io/<owner>/buckets-explorer-frontend@sha256:<digest>'

test "$(kubectl config current-context)" = 'bucket-explorer@prod'
kubectl -n "$NS" get resourcequota bucket-explorer-resources-quota
test -s k8s/env/prod/app-secrets.local.json
test -s k8s/env/prod/backend-config.local.json
test -s k8s/env/prod/tls-secret.local.json

# Stop public traffic and application workloads.
kubectl -n "$NS" delete ingress buckets-explorer.areasciencepark.it --ignore-not-found
kubectl -n "$NS" scale deployment/backend deployment/frontend \
  deployment/django-postgres --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app=backend --timeout=180s
kubectl -n "$NS" wait --for=delete pod -l app=frontend --timeout=180s
kubectl -n "$NS" wait --for=delete pod -l app=django-postgres --timeout=180s

# Delete only application-owned objects. Never delete the namespace.
kubectl -n "$NS" delete deployment backend frontend django-postgres --ignore-not-found
kubectl -n "$NS" delete service backend-service frontend-service django-postgres --ignore-not-found
kubectl -n "$NS" delete pvc django-postgres-pvc --ignore-not-found
kubectl -n "$NS" delete secret backend-secret ghcr-pull-secret \
  wildcard.areasciencepark.it-certs --ignore-not-found
kubectl -n "$NS" delete configmap backend-config --ignore-not-found
kubectl -n "$NS" get resourcequota bucket-explorer-resources-quota

# Recreate registry authentication without exposing the token in process args.
GHCR_AUTH_FILE=$(mktemp)
trap 'rm -f "$GHCR_AUTH_FILE"' EXIT
rm -f "$GHCR_AUTH_FILE"
printf '%s' "$GHCR_TOKEN" | podman login --authfile "$GHCR_AUTH_FILE" \
  ghcr.io -u '<owner>' --password-stdin
kubectl -n "$NS" create secret generic ghcr-pull-secret \
  --from-file=.dockerconfigjson="$GHCR_AUTH_FILE" \
  --type=kubernetes.io/dockerconfigjson --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" patch serviceaccount default --type=strategic \
  -p '{"imagePullSecrets":[{"name":"ghcr-pull-secret"}]}'

# Restore runtime configuration and TLS, not application metadata.
kubectl apply -f k8s/env/prod/app-secrets.local.json
kubectl apply -f k8s/env/prod/backend-config.local.json
kubectl apply -f k8s/env/prod/tls-secret.local.json

# Recreate PostgreSQL and require readiness before starting Django.
kubectl apply -f k8s/manifests/app/01-django-postgres.yaml
kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Bound \
  pvc/django-postgres-pvc --timeout=180s
kubectl -n "$NS" rollout status deployment/django-postgres --timeout=300s

# Apply workloads, immediately pin the verified immutable digests, then wait.
kubectl apply -f k8s/manifests/app/02-backend.yaml
kubectl -n "$NS" set image deployment/backend backend="$NEW_BACKEND_IMAGE"
kubectl -n "$NS" rollout status deployment/backend --timeout=300s
kubectl apply -f k8s/manifests/app/03-frontend.yaml
kubectl -n "$NS" set image deployment/frontend frontend="$NEW_FRONTEND_IMAGE"
kubectl -n "$NS" rollout status deployment/frontend --timeout=300s

# Restore public traffic only after both workloads are healthy.
kubectl apply -f k8s/manifests/app/04-ingress.yaml
```

The backend startup applies migrations to the empty database. Before manual
tenant setup, require zero users, tenants, memberships, buckets, permissions,
file records, social-auth rows, and sessions. Verify the public certificate,
frontend, health endpoint, OAuth redirect, anonymous 401 boundary, immutable
pod image IDs, external dependency connectivity, and clean logs.

### Controlled production reset

Never delete the namespace for a database reset. Preserve Secrets, ConfigMaps,
Services, Ingress, Authentik, RGWSquared, and Ceph. Record the current images and
create an encrypted dump first:

```bash
export NS=bucket-explorer
export BACKUP_DIR="${HOME}/bucket-explorer-backups/$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$BACKUP_DIR"

kubectl -n "$NS" get deployment backend frontend \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
  > "$BACKUP_DIR/images.txt"

POSTGRES_POD=$(kubectl -n "$NS" get pod -l app=django-postgres \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NS" exec "$POSTGRES_POD" -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$BACKUP_DIR/djangodb.dump"
test -s "$BACKUP_DIR/djangodb.dump"
```

Encrypt the dump with the organisation's approved mechanism and verify that
`pg_restore --list` can read the decrypted stream before continuing. Store the
decryption key separately from the dump.

Deploy images by immutable digest, then reset only PostgreSQL:

```bash
export NEW_BACKEND_IMAGE='ghcr.io/<owner>/buckets-explorer-backend@sha256:<digest>'
export NEW_FRONTEND_IMAGE='ghcr.io/<owner>/buckets-explorer-frontend@sha256:<digest>'

# Required when the GHCR packages are private. This avoids placing the token
# directly in a kubectl argument and is safe to repeat when the token rotates.
GHCR_AUTH_FILE=$(mktemp)
trap 'rm -f "$GHCR_AUTH_FILE"' EXIT
rm -f "$GHCR_AUTH_FILE"  # podman creates valid JSON; it rejects an empty file
printf '%s' "$GHCR_TOKEN" | podman login --authfile "$GHCR_AUTH_FILE" \
  ghcr.io -u '<owner>' --password-stdin
kubectl -n "$NS" create secret generic ghcr-pull-secret \
  --from-file=.dockerconfigjson="$GHCR_AUTH_FILE" \
  --type=kubernetes.io/dockerconfigjson --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" patch serviceaccount default --type=strategic \
  -p '{"imagePullSecrets":[{"name":"ghcr-pull-secret"}]}'

kubectl -n "$NS" scale deployment/backend --replicas=0
kubectl -n "$NS" scale deployment/django-postgres --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app=backend --timeout=180s
kubectl -n "$NS" wait --for=delete pod -l app=django-postgres --timeout=180s

kubectl -n "$NS" delete pvc django-postgres-pvc
kubectl apply -f k8s/manifests/app/01-django-postgres.yaml
kubectl -n "$NS" rollout status deployment/django-postgres --timeout=300s

kubectl -n "$NS" set image deployment/backend backend="$NEW_BACKEND_IMAGE"
kubectl -n "$NS" scale deployment/backend --replicas=1
kubectl -n "$NS" rollout status deployment/backend --timeout=300s
kubectl -n "$NS" set image deployment/frontend frontend="$NEW_FRONTEND_IMAGE"
kubectl -n "$NS" rollout status deployment/frontend --timeout=300s
```

If startup or acceptance checks fail, scale the backend down, recreate an empty
PostgreSQL PVC, restore the dump with `pg_restore`, set the recorded previous
image digests, and scale the backend back to one replica. Do not attempt rollback
by reconnecting a partially initialized fresh database.

After redeploy:

1. Recreate tenants with their explicit access model, then restore group mappings
   before running **Admin Panel → Tenants → Refresh**. Require zero object sync errors.
   RGWSquared-synced users should be authorized; Authentik-managed unknown users
   should be reported only as skipped, not created as placeholders.
2. Review buckets flagged **ORPHAN** in the admin Buckets view — these exist in RGW but are **not** visible on user dashboards.
3. Delete orphans that should not exist (admin **Delete** calls RGWSquared `bucketDelete`).
4. Tell researchers to recreate needed buckets through the webapp — sync does not restore user access for manual RGW buckets.
5. Do **not** expect Django to auto-delete orphan Ceph buckets without an explicit admin delete.

Full semantics: [storage-cache-and-redeploy.md](storage-cache-and-redeploy.md).

---

## Updating secrets and configuration

Kubernetes injects Secret/ConfigMap values when a pod **starts**. After `kubectl apply`, restart the backend:

```bash
kubectl apply -f k8s/env/prod/app-secrets.local.json
kubectl apply -f k8s/env/prod/backend-config.local.json
kubectl apply -f k8s/env/prod/tls-secret.local.json
kubectl rollout restart deployment/backend -n bucket-explorer
kubectl rollout status deployment/backend -n bucket-explorer --timeout=300s
```

| Change | Notes |
| --- | --- |
| `django-secret-key` | Invalidates existing JWTs/sessions; plan a maintenance window |
| `oidc-client-secret` | Verify OIDC login after rollout |
| `rgwsquared-*` | Verify tenant sync in admin panel after rollout |
| `database-password` | Update PostgreSQL role password first, then apply Secret and restart. Do not delete the PVC for routine rotation |

Default to **preserving PostgreSQL data**. Only recreate the database PVC for an intentional fresh install with explicit data-loss approval.

---

## Database migrations

The backend pod CMD runs `python manage.py migrate --noinput` before gunicorn starts. The readiness probe blocks traffic until gunicorn listens.

- **Additive migrations** (new tables/columns): rolling restart is usually safe.
- **Destructive migrations** (drop/rename): use a two-phase deploy—deploy code that no longer uses the old schema, then deploy the migration.

---

## Troubleshooting (quick reference)

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Backend CrashLoopBackOff after secret change | Invalid config or migration error | `kubectl logs deployment/backend --previous` |
| Public URL 404 but port-forward works | Routing/DNS/TLS | Ask admins to route domain to `frontend-service:80` |
| OIDC `redirect_uri=http://` | `AUTHENTIK_EXTERNAL_URL` wrong | Fix ConfigMap, restart backend |
| Admin login 403 | User not in `AUTHENTIK_ADMIN_GROUP` | Add user to Authentik group; check ConfigMap value |
| RGWSquared sync fails | Wrong credentials or URL | Check Secret and `RGWSQUARED_URL` in ConfigMap |

---

## Final production checklist

- [ ] `KUBECONFIG` points to production cluster
- [ ] `app-secrets.local.yaml` and `backend-config.local.yaml` exist, mode `600`, not committed
- [ ] No `REPLACE_WITH_PROD` placeholders remain
- [ ] `AUTHENTIK_ADMIN_GROUP` set; admin users assigned in Authentik
- [ ] PostgreSQL, backend, frontend pods `1/1 Running`
- [ ] `/api/health/` OK via public URL
- [ ] User OIDC login and Admin Panel Authentik login work
- [ ] RGWSquared sync and a disposable test-tenant smoke test pass

---

## Automated production deploy (future)

Production clusters often cannot host self-hosted GitHub runners. The recommended path when automation is needed: GitHub managed runner + scoped kubeconfig secret + manual approval gate on `main`. Dev automation is documented in [testing-and-ci.md](testing-and-ci.md).
