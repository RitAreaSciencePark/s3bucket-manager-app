# Environment overlays for Kubernetes deploy

This directory centralizes all environment-specific values.

## Structure

- `dev/backend-config.yaml` — non-sensitive development config
- `dev/app-secrets.yaml` — development secret placeholders (replace locally)
- `dev/authentik-service-nodeport.yaml` — dev-only NodePort exposure
- `prod/backend-config.yaml` — production config template (secure defaults)
- `prod/app-secrets.yaml` — production backend secret placeholders
- `prod/tls-secret.yaml` — production TLS secret placeholder; never apply it directly

## Local override pattern (recommended)

Create local files that are not committed:

- `k8s/env/dev/backend-config.local.yaml`
- `k8s/env/dev/app-secrets.local.yaml`
- `k8s/env/prod/backend-config.local.yaml`
- `k8s/env/prod/app-secrets.local.yaml` or `.local.json`
- `k8s/env/prod/tls-secret.local.yaml` or `.local.json`

Development scripts use the development `*.local.*` overrides. Production is
deployed manually; operators pass the production-local files explicitly to
`kubectl` and verify each resource before continuing.

## Policy

- Never commit plaintext real credentials.
- Commit templates with placeholders only.
- For production use Sealed Secrets, External Secrets, or SOPS-encrypted files.
