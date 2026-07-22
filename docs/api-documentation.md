# API Documentation: drf-spectacular and Swagger UI

## Why API Documentation Matters for This Codebase

Buckets Explorer exposes a REST API with around 30 endpoints across authentication, tenant management, bucket operations, file management, and admin workflows. Without interactive documentation, a new developer spends hours reading Django views and serializers to understand what each endpoint expects, what it returns, and how to authenticate.

The API documentation solves this: it gives maintainers a live, interactive surface to understand the API, try requests directly from the browser, and export a machine-readable schema for code generation or testing.

---

## Why drf-spectacular

Several tools exist for generating OpenAPI documentation from Django REST Framework. The choice comes down to two candidates: **drf-yasg** (Yet Another Swagger Generator) and **drf-spectacular**.

**drf-yasg** generates OpenAPI 2.0 (Swagger) spec. It has been unmaintained since 2021 and does not support OpenAPI 3.0.

**drf-spectacular** generates **OpenAPI 3.0**, is actively maintained, and has first-class support for DRF's generic views, serializers, and authentication schemes. It handles:

- JWT Bearer authentication declaration
- Separate request/response schemas (`COMPONENT_SPLIT_REQUEST = True`)
- Custom extensions for endpoints that DRF cannot introspect automatically
- Schema path prefix stripping (`SCHEMA_PATH_PREFIX = "/api/"`)
- Fine-grained access control on the schema endpoint itself

The decision is straightforward: drf-spectacular is the maintained standard for DRF + OpenAPI 3.0. drf-yasg is a dead end.

---

## What Gets Generated

drf-spectacular introspects all registered DRF views and serializers to produce an **OpenAPI 3.0 schema** in JSON format. From that schema it serves two UI renderers:

| Renderer | Description |
|----------|-------------|
| **Swagger UI** | Interactive browser UI: try endpoints, see request/response formats, authorize with tokens |
| **ReDoc** | Static, well-structured reference documentation; better for reading, not for trying requests |

The raw schema (machine-readable JSON) is also exposed directly for tools that consume OpenAPI specs (Postman, code generators, contract testing).

---

## Endpoints

| URL | What it serves |
|-----|----------------|
| `GET /api/schema/` | Raw OpenAPI 3.0 schema (JSON) |
| `GET /api/docs/` | Swagger UI (interactive) |
| `GET /api/redoc/` | ReDoc (read-only reference) |

These URLs are registered unconditionally in `urls.py`. Access control is enforced by drf-spectacular's `SERVE_PERMISSIONS` setting — the URLs are always routable, but unauthenticated or non-admin users are denied (**401 Unauthorized** or **403 Forbidden**).

### Tenant Refresh response

`POST /api/admin/sync/refresh/` accepts `structure_code` or `tenant_id`. Its
response reports `users_synced`, `users_skipped_unregistered`,
`buckets_synced`, `permissions_synced`, `objects_synced`,
`objects_discovered`, `object_records_removed`, and
`object_sync_errors`. A non-zero object error count means bucket/user policy was
refreshed but at least one Ceph bucket could not be inventoried; cached object
records for failed buckets are preserved.

---

### Tenant activation and access status

`POST /api/admin/tenants/create/` requires `access_model`:
`rgwsquared_synced` imports upstream users and roles, while
`authentik_managed` registers users and roles only after mapped-group login.
Admin user rows expose `access_status` (`authorized`, `revoked`, or
`not_configured`) plus revocation time, reason, and group. A revoked row
retains its user and storage metadata.

## Access control (all environments)

The schema and documentation UIs are **admin-only**. `SERVE_PERMISSIONS` is set to `rest_framework.permissions.IsAdminUser` in both development and production.

A user qualifies when:

1. They are authenticated (session cookie or Bearer JWT), and
2. `user.is_staff` is `True` — synced from the Authentik group in `AUTHENTIK_ADMIN_GROUP` (default: `buckets-explorer-admin`) during OAuth login.

**Why this restriction exists:** The full endpoint map is attack-surface intelligence. A public OpenAPI schema tells an attacker the exact URL patterns, parameter types, and response shapes for every endpoint. Restricting docs to admins prevents reconnaissance while keeping the documentation available to authorized maintainers.

Regular app users (non-admin) and anonymous visitors cannot open `/api/docs/`, `/api/schema/`, or `/api/redoc/`.

---

## How to access Swagger UI

### Browser (recommended)

1. Open `/admin/login` and sign in with Authentik.
2. Your Authentik user must belong to `AUTHENTIK_ADMIN_GROUP` (default: `buckets-explorer-admin`).
3. In the **same browser session**, open `/api/docs/` or use the **API Docs** link in the admin sidebar.
4. For **Try it out** on protected endpoints, click **Authorize** and enter `Bearer <access-token>` from `GET /api/admin/auth/token/`.

The admin OAuth flow sets a Django session cookie on the same origin (`localhost:3000` in dev). That cookie is what grants access to the docs pages before you optionally add a JWT for executing requests inside Swagger UI.

### curl (schema export)

```bash
# After browser login, export cookies or use an admin JWT:
curl -H "Authorization: Bearer eyJ..." http://localhost:3000/api/schema/
```

### Which token to use in Swagger **Authorize**

| Token source | Use for |
|--------------|---------|
| Admin JWT from `/api/admin/auth/token/` | `/api/admin/*` endpoints |
| User JWT from `/api/auth/token/` (after user OAuth) | `/api/buckets/`, file upload, etc. — requires `X-Tenant-ID` header |

An admin JWT has no tenant context; user-facing bucket endpoints will return 403 without `X-Tenant-ID`.

---

## Swagger UI Feature Reference

```
┌────────────────────────────────────────────────────────────────────┐
│  Swagger UI                                              [Authorize]│
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ▼ Auth                                                              │
│    POST /api/oauth/login/{backend}/    OAuth2 initiation            │
│    GET  /api/auth/token/               Exchange code → JWT          │
│                                                                      │
│  ▼ Buckets                                                           │
│    GET  /api/buckets/                  List all accessible buckets  │
│    POST /api/buckets/                  Request a new local bucket   │
│    GET  /api/buckets/{id}/files/       List files in a bucket       │
│    POST /api/buckets/{id}/upload/      Upload a file                │
│    ...                                                               │
│                                                                      │
│  ▼ Admin                                                             │
│    GET  /api/admin/auth/token/         Admin OAuth → JWT            │
│    ...                                                               │
│                                                                      │
└────────────────────────────────────────────────────────────────────┘
```

Key features:

- **Try it out**: Click any endpoint → "Try it out" button → fill parameters → "Execute" → see live response including status code, headers, and body.
- **Authorize**: Set Bearer tokens once; all subsequent requests use them.
- **Schema download**: The "Download" link in the header fetches `GET /api/schema/` as a JSON file, importable into Postman or other API clients.
- **Models section**: Scrolling to the bottom shows all request/response schemas with field names, types, and constraints — generated directly from DRF serializers.

---

## drf-spectacular Configuration

The full configuration is in `backend/settings.py` under `SPECTACULAR_SETTINGS`. Key settings:

```python
SPECTACULAR_SETTINGS = {
    "TITLE": "Buckets Explorer API",
    "VERSION": "1.0.0",
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "SERVE_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
}
```

---

## Regenerating the Schema Offline

For CI pipelines, contract testing, or SDK generation, the schema can be exported to a static file without running a server:

```bash
cd backend
python manage.py spectacular --color --file schema.yaml
```

This produces a `schema.yaml` file that is equivalent to `GET /api/schema/` but does not require the server to be running. Useful for diffing schema changes in code review or feeding into type-safe client generators.
