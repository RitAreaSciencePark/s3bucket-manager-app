# Buckets Explorer Documentation

Reading order for maintainers and operators.

## Public documentation policy

Files in `docs/` are **public**: they must be self-contained so another operator or maintainer can deploy, test, and understand the system without private notes.

- Keep how-to guides, architecture, API contracts, and placeholder templates here.
- Do not add release checklists, session todos, or links to local-only paths (`internal_docs/`, `PLAN.md`, agent instruction files).
- Maintainer-specific registry or CI accounts may be documented when they reflect the current deployment, with a note that future teams must substitute their own.

## Get started

| Document | Purpose |
| --- | --- |
| [Development environment setup](dev-environment-setup.md) | Provision K3s, Ceph, Authentik, and deploy the app from scratch |
| [Development environment overview](dev-environment-overview.md) | Stencil topology, networking, and how components connect |
| [Development deployment operations](dev-deployment-operations.md) | Day-to-day deploy and updates with `k8s/app.sh` and `k8s/infra.sh` |

## Maintain the application

| Document | Purpose |
| --- | --- |
| [Maintainer guide](bucket-explorer-maintainer-guide.md) | Tenants, identity, buckets, nginx BFF, RGWSquared `userCreate`, data model |
| [Testing and CI](testing-and-ci.md) | Run tests locally, `app.sh verify`, GitHub Actions, Codecov |
| [RGWSquared API guide](rgwsquared-api.md) | Stable RGWSquared endpoints used by the webapp |
| [UO code tenants](uo-code-tenants.md) | Institutional UO codes for multi-institution tenants |

## Deploy and operate

| Document | Purpose |
| --- | --- |
| [Production deployment and operations](production-deployment.md) | Initial deploy, verification, code updates, secret rotation |
| [Storage cache and redeploy](storage-cache-and-redeploy.md) | Three-layer model, Class A/B/C deploy semantics, self-healing limits |
| [API documentation guide](api-documentation.md) | Swagger UI and OpenAPI schema |
| [Database schema](database-schema.html) | Visual ERD of Django models |

## Dev vs production

| Environment | Tooling |
| --- | --- |
| Development (Stencil K3s) | `k8s/infra.sh`, `k8s/app.sh` — see [dev-deployment-operations.md](dev-deployment-operations.md) |
| Production | Manual `kubectl apply` ladder; see [production-deployment.md](production-deployment.md) |
