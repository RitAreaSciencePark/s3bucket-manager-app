# Testing and Continuous Integration

Buckets Explorer uses **pytest** for backend tests and **`npm run build`** as the frontend compile gate. Coverage reports upload to [Codecov](https://app.codecov.io/gh/luisfpal/s3bucket-manager-app) on every CI run.

There is no Jest test suite—the Codecov onboarding wizard may suggest Jest; ignore that for this project.

## Run checks locally

From the repository root:

```bash
./k8s/app.sh verify
```

For deploy, updates, and kubeconfig setup, see [Development deployment operations](dev-deployment-operations.md).

This runs the same steps as the GitHub Actions `verify` job:

1. `bash -n` on `k8s/app.sh` and `k8s/infra.sh`
2. `pip install ./backend[dev]` and `pytest --cov=storage`
3. `npm run build` in `frontend/`

### Backend only

```bash
cd backend
python -m pip install "./[dev]"
DJANGO_DEBUG=True python -m pytest --cov=storage --cov-report=term-missing
```

Tests live in `backend/storage/tests.py` (auth, admin, OAuth pipeline, RGWSquared client, bucket lifecycle, sync service, crypto, user provisioning).

### Frontend only

```bash
cd frontend
npm ci
npm run build
```

## GitHub Actions

Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)

| Job | When | What |
| --- | --- | --- |
| `verify` | Every push/PR to `main` | pytest + coverage.xml + frontend build + Codecov upload |

CI answers **"Is this commit safe?"** — it does not build, push, or roll out images. Dev deployment is **manual** on the operator host via [`k8s/app.sh`](../k8s/app.sh). See [Development deployment operations](dev-deployment-operations.md). Production deployment is also manual. See [production-deployment.md](production-deployment.md).

## GHCR and secrets (local vs GitHub)

| Artifact | Uses `GHCR_TOKEN`? | What it does |
| --- | --- | --- |
| `k8s/app.sh` | **Yes** — sources `k8s/.env` | Logs in for build/push paths and refreshes the namespace `ghcr-pull-secret` before deployment |
| `k8s/.env.example` | Documents only | Template for local `GHCR_TOKEN` (classic PAT, `read:packages` + `write:packages`) |
| `k8s/manifests/app/*.yaml` | **No** — static `image:` URLs | Deployments use the pull secret attached to the namespace's default service account |
| `k8s/ci.sh` | **No** — uses `GITHUB_PAT` | Optional ARC self-hosted runner install (`repo` scope) |
| GitHub Actions | **No** `GHCR_TOKEN` | Verify job only; image push is not run in CI |

**Takeaway:** keep `GHCR_TOKEN` in gitignored `k8s/.env` for local deployment and image publication. You do not need `GHCR_TOKEN` as a GitHub repository secret for the current workflow.

## Codecov setup (one-time)

1. Link the repo at [codecov.io](https://codecov.io) (this project currently uses `luisfpal/s3bucket-manager-app` for coverage uploads during development).
2. Install the [Codecov GitHub App](https://github.com/apps/codecov) on the repository.
3. Add repository secret `CODECOV_TOKEN` (Settings → Secrets → Actions) with the upload token from Codecov → Settings → General.

After the first green CI run, open the Codecov dashboard for coverage history and line-level reports.

Future maintainers should point Codecov at their own GitHub organisation or fork and update the workflow token accordingly.

## Dev deploy workflow

| Step | Command |
| --- | --- |
| Verify locally | `cd k8s && ./app.sh verify` |
| Build, push images, roll out dev | `cd k8s && ./app.sh deploy --rebuild` |
| Port-forward / access URL | `./app.sh access` |

`GHCR_TOKEN` in `k8s/.env` is required for deploys and image-push paths. The self-hosted runner (`bucket-explorer-runner`) installed via `k8s/ci.sh` is **optional** and not used by the current CI workflow.

## Before production image push

Run `./k8s/app.sh verify` (or confirm CI is green on the commit you are deploying). Do not push production images from a commit that fails tests.

## Dependencies

Backend Python dependencies are declared in [`backend/pyproject.toml`](../backend/pyproject.toml). Production images install with `pip install .`; dev/CI install with `pip install ./backend[dev]`.
