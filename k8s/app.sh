#!/bin/bash
# ==============================================================================
# app.sh — Application Layer Management (Webapp: Backend + Frontend)
# ==============================================================================
#
# PURPOSE:
#   Manages the Buckets Explorer webapp in the bucket-explorer namespace.
#   This is the APPLICATION layer — it assumes Authentik is already running
#   and reachable (deployed by infra.sh, or running locally for testing).
#
#   Both scripts are self-contained and do not call each other.
#   The companion script infra.sh manages the Authentik infrastructure layer
#   (authentik-bucket-explorer namespace) independently.
#
# USAGE:
#   Full webapp deploy:
#     ./app.sh deploy --rebuild    # build images, push to GHCR, deploy
#     ./app.sh deploy              # deploy only (use existing images)
#
#   Standalone namespace creation:
#     ./app.sh deploy-namespace
#
#   Inner-loop rebuilds (no manifest re-apply — just build, push, restart):
#     ./app.sh backend       # Rebuild + redeploy backend  (~60s)
#     ./app.sh frontend      # Rebuild + redeploy frontend (~90s)
#     ./app.sh all           # Rebuild + redeploy both
#
#   Operations:
#     ./app.sh status              # Show pods in bucket-explorer namespace
#     ./app.sh logs [component]    # Tail backend or frontend logs
#     ./app.sh access              # SSH tunnel + port-forwards for frontend + Authentik
#     ./app.sh restart <comp>      # Restart a deployment without rebuild
#     ./app.sh cleanup             # Delete bucket-explorer namespace + optional image cleanup
#     ./app.sh dev-db <action>     # Local PostgreSQL via podman (start|stop|destroy|status)
#     ./app.sh verify              # Run the same checks as GitHub Actions verify job
#
# WHAT EACH SUBCOMMAND DOES (for manual reference):
#
#   deploy:
#     1. Build and push images to GHCR (if --rebuild)
#     2. Create bucket-explorer namespace (deploy-namespace)
#     3. Apply app secrets and backend ConfigMap
#     4. Apply app secrets (backend-secret) and backend ConfigMap
#     5. Apply django-postgres; wait for ready
#     6. Apply backend (init containers wait for postgres + Authentik FQDN); wait for ready
#     7. Apply frontend; wait for ready
#
#   deploy-namespace:
#     Idempotent: kubectl apply 00-namespace.yaml
#     Called automatically by 'deploy'.
#
#   backend/frontend/all:
#     1. podman build -t ghcr.io/luisfpal/buckets-explorer-<component>:latest
#     2. ghcr_login + podman push; refresh the namespace pull secret
#     3. kubectl rollout restart deployment/<component>  -n bucket-explorer
#     4. kubectl rollout status (waits; imagePullPolicy: Always fetches new image)
#
#   access:
#     1. Check/establish SSH tunnel to K3s API (port 16443 → 6443)
#     2. Fetch kubeconfig if missing
#     3. Start port-forward: :3000 → bucket-explorer/frontend-service:80
#     4. Start port-forward: :9000 → authentik-bucket-explorer/authentik-service:9000
#        (Authentik port-forward is needed here because the login flow redirects to it)
#
# PREREQUISITES:
#   - k8s/.env with GHCR_TOKEN (classic PAT, write:packages scope)
#   - Run './app.sh access' once per session (or when /tmp/k3s-tunnel-kubeconfig.yaml
#     is missing) before deploy — it creates the SSH tunnel and kubeconfig file
#   - KUBECONFIG set to /tmp/k3s-tunnel-kubeconfig.yaml (default; export in new shells)
#   - Environment overlays in k8s/env/<env>/
#   - Authentik running in authentik-bucket-explorer namespace (deployed by infra.sh)
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Namespace managed by this script
NAMESPACE="bucket-explorer"

# Authentik namespace — read-only reference for access() port-forward setup.
# The app script does not manage this namespace.
INFRA_NAMESPACE="authentik-bucket-explorer"

APP_MANIFESTS_DIR="$SCRIPT_DIR/manifests/app"
ENV_BASE_DIR="$SCRIPT_DIR/env"

K3S_NODES=("192.168.132.10" "192.168.132.11" "192.168.132.12")
TUNNEL_SOCK="/tmp/k3s-api-tunnel.sock"
TUNNEL_KUBECONFIG="/tmp/k3s-tunnel-kubeconfig.yaml"

GHCR_OWNER="luisfpal"
REGISTRY="ghcr.io/${GHCR_OWNER}"
BACKEND_IMAGE="${REGISTRY}/buckets-explorer-backend:latest"
FRONTEND_IMAGE="${REGISTRY}/buckets-explorer-frontend:latest"

# Source local secrets (GHCR_TOKEN for registry auth). k8s/.env is gitignored.
# See k8s/.env.example for the expected format and token creation instructions.
[ -f "${SCRIPT_DIR}/.env" ] && source "${SCRIPT_DIR}/.env"

ENV_DIR=""
APP_SECRETS_FILE=""
BACKEND_CONFIG_FILE=""

# Local PostgreSQL container for running Django without K3s
DEV_PG_CONTAINER="dev-postgres"
DEV_PG_IMAGE="postgres:15-alpine"
DEV_PG_DB="djangodb"
DEV_PG_USER="djangouser"
DEV_PG_PASS="dev-db-password"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[  OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[FAIL]${NC} $*"; }
fail()    { error "$*"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}"; }

# ==============================================================================
# Rollout helpers
# ==============================================================================

rollout_with_retry() {
    # Wait for a deployment rollout, retrying on timeout.
    # Usage: rollout_with_retry <resource> [timeout] [attempts] [namespace]
    local resource="$1"
    local timeout="${2:-120s}"
    local attempts="${3:-2}"
    local ns="${4:-$NAMESPACE}"
    local attempt

    for ((attempt=1; attempt<=attempts; attempt++)); do
        if kubectl rollout status "$resource" -n "$ns" --timeout="$timeout"; then
            return 0
        fi
        warn "$resource rollout attempt ${attempt}/${attempts} failed (ns=$ns)"
        if [ "$attempt" -lt "$attempts" ]; then
            info "Retrying rollout wait for $resource..."
        fi
    done

    fail "$resource rollout did not complete after ${attempts} attempts (ns=$ns)"
}

wait_for_rollout() {
    local resource="$1"
    local timeout="${2:-300s}"
    local attempts="${3:-2}"
    local ns="${4:-$NAMESPACE}"
    info "Waiting for $resource (ns=$ns)..."
    rollout_with_retry "$resource" "$timeout" "$attempts" "$ns"
    success "$resource is ready"
}

# ==============================================================================
# Kubeconfig
# ==============================================================================

ensure_kubeconfig() {
    export KUBECONFIG="${KUBECONFIG:-$TUNNEL_KUBECONFIG}"
    if [ ! -f "$KUBECONFIG" ]; then
        error "Kubeconfig not found: $KUBECONFIG"
        echo "  Run: ./app.sh access"
        exit 1
    fi
    if ! kubectl cluster-info &>/dev/null 2>&1; then
        error "Cannot connect to K3s. SSH tunnel may be down."
        echo "  Run: ./app.sh access"
        exit 1
    fi
}

# ==============================================================================
# Environment file selection
# ==============================================================================

resolve_app_environment_files() {
    ENV_DIR="$ENV_BASE_DIR/dev"
    APP_SECRETS_FILE="$ENV_DIR/app-secrets.yaml"
    BACKEND_CONFIG_FILE="$ENV_DIR/backend-config.yaml"

    # Local override files (gitignored, contain real credentials)
    local app_secrets_local="$ENV_DIR/app-secrets.local.yaml"
    local backend_local="$ENV_DIR/backend-config.local.yaml"

    [ -f "$app_secrets_local" ] && APP_SECRETS_FILE="$app_secrets_local"
    [ -f "$backend_local" ]     && BACKEND_CONFIG_FILE="$backend_local"

    [ -f "$APP_SECRETS_FILE" ]    || fail "Missing app secrets file: $APP_SECRETS_FILE"
    [ -f "$BACKEND_CONFIG_FILE" ] || fail "Missing backend config file: $BACKEND_CONFIG_FILE"

    if [[ "$APP_SECRETS_FILE" == *.local.yaml ]] || [[ "$BACKEND_CONFIG_FILE" == *.local.yaml ]]; then
        info "Using local overrides from $ENV_DIR (*.local.*)"
    fi
}

# ==============================================================================
# GHCR authentication + pull secret
# ==============================================================================

ghcr_login() {
    if [ -z "${GHCR_TOKEN:-}" ]; then
        fail "GHCR_TOKEN is not set. Add it to k8s/.env — see k8s/.env.example."
    fi
    info "Logging in to ghcr.io as ${GHCR_OWNER}..."
    echo "${GHCR_TOKEN}" | podman login ghcr.io -u "${GHCR_OWNER}" --password-stdin
    success "Authenticated to ghcr.io"
}

ensure_ghcr_pull_secret() (
    if [ -z "${GHCR_TOKEN:-}" ]; then
        fail "GHCR_TOKEN is not set. It is required to pull private GHCR packages."
    fi

    local auth_file
    auth_file=$(mktemp)
    trap 'rm -f "$auth_file"' EXIT
    rm -f "$auth_file"

    echo "${GHCR_TOKEN}" | podman login \
        --authfile "$auth_file" \
        ghcr.io -u "${GHCR_OWNER}" --password-stdin >/dev/null
    chmod 600 "$auth_file"
    kubectl -n "$NAMESPACE" create secret generic ghcr-pull-secret \
        --from-file=.dockerconfigjson="$auth_file" \
        --type=kubernetes.io/dockerconfigjson \
        --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n "$NAMESPACE" wait --for=create serviceaccount/default \
        --timeout=60s >/dev/null
    kubectl -n "$NAMESPACE" patch serviceaccount default --type=strategic \
        -p '{"imagePullSecrets":[{"name":"ghcr-pull-secret"}]}' >/dev/null
    success "GHCR pull credentials configured in $NAMESPACE"
)

# ==============================================================================
# Frontend build helper
# ==============================================================================

prepare_frontend_generated_dirs() {
    local frontend_dir="$PROJECT_DIR/frontend"
    local needs_install=false
    local needs_build=false

    [ -d "$frontend_dir/node_modules" ] || needs_install=true
    [ -d "$frontend_dir/dist" ] || needs_build=true

    if [ "$needs_install" = false ] && [ "$needs_build" = false ]; then
        return 0
    fi

    if ! command -v npm &>/dev/null; then
        warn "npm not found; skipping local frontend bootstrap (container build will still generate artifacts)."
        return 0
    fi

    step "Bootstrapping missing frontend generated directories"
    (
        cd "$frontend_dir"
        if [ "$needs_install" = true ]; then
            info "Creating frontend/node_modules via npm ci"
            npm ci
        fi
        if [ "$needs_build" = true ]; then
            info "Creating frontend/dist via npm run build"
            npm run build
        fi
    )
    success "Frontend generated directories are present"
}

# ==============================================================================
# Namespace deployment (standalone step)
# ==============================================================================

deploy_namespace() {
    # Create the bucket-explorer namespace only.
    # Called automatically as the first step of 'deploy'.
    # kubectl apply is idempotent — safe to run even if the namespace already exists.
    resolve_app_environment_files
    ensure_kubeconfig
    step "Creating app namespace ($NAMESPACE)"
    kubectl apply -f "$APP_MANIFESTS_DIR/00-namespace.yaml"
    success "Namespace $NAMESPACE ready"
}

# ==============================================================================
# Apply app manifests (full webapp deployment)
# ==============================================================================

apply_app_manifests_for_env() {
    step "Deploying webapp to $NAMESPACE"

    # Step 1: namespace (idempotent — safe even if it already exists)
    kubectl apply -f "$APP_MANIFESTS_DIR/00-namespace.yaml"

    # Step 2: registry credentials, app secrets, and backend ConfigMap
    ensure_ghcr_pull_secret
    kubectl apply -f "$APP_SECRETS_FILE"
    kubectl apply -f "$BACKEND_CONFIG_FILE"

    # Step 3: Django's PostgreSQL database — must be ready before backend init containers run
    kubectl apply -f "$APP_MANIFESTS_DIR/01-django-postgres.yaml"
    wait_for_rollout "deployment/django-postgres" "300s" "2" "$NAMESPACE"

    # Step 4: Backend — two init containers run first:
    #   - wait-for-postgres: polls django-postgres:5432
    #   - wait-for-authentik: polls authentik-service.authentik-bucket-explorer.svc.cluster.local:9000
    kubectl apply -f "$APP_MANIFESTS_DIR/02-backend.yaml"
    wait_for_rollout "deployment/backend" "300s" "2" "$NAMESPACE"

    # Step 5: Frontend — serves the React SPA and proxies /api/* to backend-service:8000
    kubectl apply -f "$APP_MANIFESTS_DIR/03-frontend.yaml"
    wait_for_rollout "deployment/frontend" "300s" "2" "$NAMESPACE"

    # Step 6: Dev Ingress (catch-all) — applied if IngressClass haproxy-4 exists in the cluster.
    # 04-ingress.dev.yaml accepts any Host header so localhost:3000 works via the HAProxy
    # NodePort while OAuth2 redirect_uri (http://localhost:3000/...) still matches Authentik.
    #
    # 04-ingress.yaml is a production-specific Ingress — it is not applied here.
    local dev_ingress_file="$APP_MANIFESTS_DIR/04-ingress.dev.yaml"
    if [ -f "$dev_ingress_file" ]; then
        local ingress_class
        ingress_class=$(grep 'ingressClassName:' "$dev_ingress_file" 2>/dev/null | awk '{print $2}')
        if [ -n "$ingress_class" ] && kubectl get ingressclass "$ingress_class" &>/dev/null 2>&1; then
            kubectl apply -f "$dev_ingress_file"
            success "Dev Ingress applied (catch-all via haproxy-4, enables localhost:3000 through Ingress)"
        else
            info "Dev Ingress skipped — IngressClass '${ingress_class:-unknown}' not in this cluster"
            info "  Use './app.sh access' (direct port-forward) to reach the app"
        fi
    fi

    success "Webapp manifests applied"
}

# ==============================================================================
# deploy: full webapp deployment
# ==============================================================================

run_deploy() {
    local rebuild=false
    local skip_authentik_check=false

    while [ $# -gt 0 ]; do
        case "$1" in
            -h|--help)
                echo "Usage: ./app.sh deploy [--rebuild] [--skip-authentik-check]"
                echo ""
                echo "  --rebuild              Build and push images to GHCR before deploying"
                echo "  --skip-authentik-check Skip the Authentik readiness check"
                echo "                         Use when Authentik is known to be running"
                return 0
                ;;
            --rebuild)
                rebuild=true
                ;;
            --skip-authentik-check)
                skip_authentik_check=true
                ;;
            *) fail "Unknown argument: $1  (run  ./app.sh deploy --help)" ;;
        esac
        shift
    done

    resolve_app_environment_files
    ensure_kubeconfig

    # Guard: warn if Authentik infra is not found — the backend init container will wait
    # for it anyway, but this gives early feedback before manifest application starts.
    if [ "$skip_authentik_check" = false ] && [ -n "${INFRA_NAMESPACE:-}" ]; then
        if ! kubectl get deployment authentik-server -n "$INFRA_NAMESPACE" &>/dev/null 2>&1; then
            warn "Authentik not found in '$INFRA_NAMESPACE' — deploy the infra layer first:"
            warn "  ./infra.sh deploy"
            warn "Continuing anyway — the backend init container will wait for Authentik."
        fi
    fi

    step "Webapp deploy start"

    if [ "$rebuild" = true ]; then
        build_component_image backend
        build_component_image frontend
    else
        info "Skipping image rebuild. Use --rebuild to build and push new images."
    fi

    apply_app_manifests_for_env

    if [ "$rebuild" = true ]; then
        info "Restarting app deployments to pull rebuilt :latest images..."
        kubectl rollout restart deployment/backend -n "$NAMESPACE"
        rollout_with_retry "deployment/backend" "180s" "2" "$NAMESPACE"
        kubectl rollout restart deployment/frontend -n "$NAMESPACE"
        rollout_with_retry "deployment/frontend" "180s" "2" "$NAMESPACE"
    fi

    success "Webapp deploy completed"
}

# ==============================================================================
# Inner-loop: build + push + restart a single component
# ==============================================================================

redeploy_component() {
    local component="$1"  # "backend" or "frontend"
    local image_name build_dir

    if [ "$component" = "backend" ]; then
        image_name="$BACKEND_IMAGE"
        build_dir="$PROJECT_DIR/backend"
    elif [ "$component" = "frontend" ]; then
        image_name="$FRONTEND_IMAGE"
        build_dir="$PROJECT_DIR/frontend"
    else
        error "Unknown component: $component  (expected: backend or frontend)"
        exit 1
    fi

    local start_time=$(date +%s)

    [ "$component" = "frontend" ] && prepare_frontend_generated_dirs

    # Step 1: Build image locally
    step "Building $component"
    podman build -t "$image_name" -f "$build_dir/Containerfile" "$build_dir"
    success "Image built"

    # Step 2: Push to GHCR and refresh the namespace pull credential.
    ghcr_login
    info "Pushing to GHCR..."
    podman push "$image_name"
    success "Image pushed to ghcr.io"
    ensure_ghcr_pull_secret

    # Step 3: Restart deployment — Always pull policy fetches the newly pushed image
    info "Restarting deployment..."
    kubectl rollout restart "deployment/$component" -n "$NAMESPACE"
    rollout_with_retry "deployment/$component" "180s" "2" "$NAMESPACE"

    local elapsed=$(( $(date +%s) - start_time ))
    success "$component redeployed in ${elapsed}s"
}

build_component_image() {
    # Build and push a component image without restarting the deployment.
    # Used by 'deploy --rebuild' to pre-push images before applying manifests.
    local component="$1"
    local image_name build_dir

    if [ "$component" = "backend" ]; then
        image_name="$BACKEND_IMAGE"
        build_dir="$PROJECT_DIR/backend"
    elif [ "$component" = "frontend" ]; then
        image_name="$FRONTEND_IMAGE"
        build_dir="$PROJECT_DIR/frontend"
    else
        fail "Unknown component: $component"
    fi

    [ "$component" = "frontend" ] && prepare_frontend_generated_dirs

    step "Building $component image"
    podman build -t "$image_name" -f "$build_dir/Containerfile" "$build_dir"
    success "$component image built"

    ghcr_login
    info "Pushing $component image to GHCR..."
    podman push "$image_name"
    success "$component image pushed to ghcr.io"
}

# ==============================================================================
# Status: show webapp pods
# ==============================================================================

show_status() {
    ensure_kubeconfig

    step "Webapp Pods ($NAMESPACE)"
    kubectl get pods -n "$NAMESPACE" -o wide 2>/dev/null || \
        warn "Cannot reach K3s or namespace not deployed yet"

    step "Port-Forwards"
    local pf_frontend pf_authentik
    pf_frontend=$(ss -tlnp 2>/dev/null | grep ":3000 " || true)
    pf_authentik=$(ss -tlnp 2>/dev/null | grep ":9000 " || true)

    if [ -n "$pf_frontend" ]; then
        success "Frontend  :3000 → active"
    else
        warn "Frontend  :3000 → NOT running"
        info "Fix: ./app.sh access"
    fi

    if [ -n "$pf_authentik" ]; then
        success "Authentik :9000 → active"
    else
        warn "Authentik :9000 → NOT running"
        info "Fix: ./app.sh access"
    fi

    step "SSH Tunnel"
    if [ -S "$TUNNEL_SOCK" ]; then
        success "K3s API tunnel active ($TUNNEL_SOCK)"
    else
        warn "K3s API tunnel NOT running"
        info "Fix: ./app.sh access"
    fi

    echo ""
}

# ==============================================================================
# Logs: tail backend or frontend logs
# ==============================================================================

show_logs() {
    ensure_kubeconfig
    local component="${1:-backend}"

    case "$component" in
        backend|frontend)
            kubectl logs -l "app=$component" -n "$NAMESPACE" --tail=50 -f
            ;;
        *)
            error "Unknown component: $component"
            echo "  Valid options: backend, frontend"
            echo "  For Authentik logs use: ./infra.sh logs [authentik-server|authentik-worker]"
            exit 1
            ;;
    esac
}

# ==============================================================================
# Access: establish SSH tunnel + port-forwards for the full login flow
# ==============================================================================

AUTHENTIK_SERVICE="authentik-service"
AUTHENTIK_PF_PORT="${AUTHENTIK_PF_PORT:-9000}"

stop_kubectl_port_forward_on_port() {
    local local_port="$1"
    local pids
    pids="$(ss -tlnp 2>/dev/null | awk -v port=":${local_port}" '$4 ~ port { print }' | grep 'kubectl' | grep -oP 'pid=\K[0-9]+' || true)"
    [ -n "$pids" ] || return 0
    while read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"
}

infra_authentik_pf_running_on_port() {
    local local_port="$1"
    pgrep -f "kubectl port-forward svc/${AUTHENTIK_SERVICE} ${local_port}:.* -n ${INFRA_NAMESPACE}" >/dev/null 2>&1
}

ensure_authentik_port_forward() {
    local local_port="$1"

    if infra_authentik_pf_running_on_port "$local_port"; then
        success "Authentik :${local_port} → $INFRA_NAMESPACE (already running)"
        return 0
    fi

    if ss -tlnp 2>/dev/null | grep -q ":${local_port} "; then
        warn "Port :${local_port} is held by another Authentik port-forward — reclaiming for $INFRA_NAMESPACE"
        stop_kubectl_port_forward_on_port "$local_port"
        sleep 0.5
    fi

    info "Starting Authentik port-forward (:${local_port} → $INFRA_NAMESPACE/authentik-service:9000)..."
    : > /tmp/pf-authentik.log
    nohup kubectl port-forward "svc/${AUTHENTIK_SERVICE}" "${local_port}:9000" -n "$INFRA_NAMESPACE" \
        > /tmp/pf-authentik.log 2>&1 &
    sleep 1
    if ss -tlnp 2>/dev/null | grep -q ":${local_port} "; then
        success "Authentik :${local_port} → $INFRA_NAMESPACE active"
    else
        warn "Authentik port-forward may have failed — check /tmp/pf-authentik.log"
    fi
}

setup_access() {
    step "Setting up access"

    # 1. SSH tunnel to K3s API
    if [ -S "$TUNNEL_SOCK" ]; then
        if ssh -S "$TUNNEL_SOCK" -O check root@dummy &>/dev/null 2>&1; then
            success "SSH tunnel already running"
        else
            warn "Stale tunnel socket — cleaning up"
            rm -f "$TUNNEL_SOCK"
        fi
    fi

    if [ ! -S "$TUNNEL_SOCK" ]; then
        info "Starting SSH tunnel: localhost:16443 → K3s API (192.168.132.10:6443)..."
        ssh -fNM -S "$TUNNEL_SOCK" \
            -L 16443:127.0.0.1:6443 \
            -o StrictHostKeyChecking=no \
            -o ExitOnForwardFailure=yes \
            root@192.168.132.10
        success "SSH tunnel established"
    fi

    # 2. Fetch kubeconfig if missing
    if [ ! -f "$TUNNEL_KUBECONFIG" ]; then
        info "Fetching kubeconfig..."
        scp -o StrictHostKeyChecking=no \
            root@192.168.132.10:/etc/rancher/k3s/k3s.yaml "$TUNNEL_KUBECONFIG" &>/dev/null
        sed -i 's|server: https://127.0.0.1:6443|server: https://127.0.0.1:16443|' \
            "$TUNNEL_KUBECONFIG"
        success "Kubeconfig ready"
    fi

    export KUBECONFIG="$TUNNEL_KUBECONFIG"

    # 3. Frontend access.
    # If 04-ingress.dev.yaml (catch-all Ingress) is deployed and haproxy-4 IngressClass
    # exists: forward localhost:3000 → HAProxy Ingress NodePort. Traffic exercises the
    # full production-like chain (HAProxy → nginx → backend) while OAuth2 still works
    # because the catch-all Ingress accepts any Host header (including localhost:3000).
    # If no dev Ingress is available: direct port-forward to frontend-service (always works).
    local ingress_nodeport=""
    local dev_ingress_name="bucket-explorer-dev"
    if kubectl get ingress "$dev_ingress_name" -n "$NAMESPACE" &>/dev/null 2>&1; then
        local ic
        ic=$(kubectl get ingress "$dev_ingress_name" -n "$NAMESPACE" \
            -o jsonpath='{.spec.ingressClassName}' 2>/dev/null || true)
        if [ -n "$ic" ] && kubectl get ingressclass "$ic" &>/dev/null 2>&1; then
            ingress_nodeport=$(kubectl get svc -n haproxy-ingress \
                -o jsonpath='{.items[0].spec.ports[?(@.port==80)].nodePort}' 2>/dev/null || true)
        fi
    fi

    if ! ss -tlnp 2>/dev/null | grep -q ":3000 "; then
        if [ -n "$ingress_nodeport" ]; then
            # Dev Ingress available: tunnel localhost:3000 → HAProxy NodePort.
            # 04-ingress.dev.yaml is a catch-all so localhost works and the OAuth2
            # redirect_uri http://localhost:3000/... matches the Authentik config.
            info "Starting frontend tunnel (:3000 → kube01:$ingress_nodeport via haproxy-4 catch-all Ingress)..."
            nohup ssh -fNL "3000:192.168.132.10:$ingress_nodeport" \
                -o StrictHostKeyChecking=no \
                -o ExitOnForwardFailure=yes \
                root@192.168.132.10 \
                > /tmp/pf-frontend.log 2>&1 &
            sleep 1
            if ss -tlnp 2>/dev/null | grep -q ":3000 "; then
                success "Frontend :3000 → HAProxy Ingress (haproxy-4) → $NAMESPACE/frontend-service"
            else
                warn "Ingress tunnel failed — falling back to direct frontend-service port-forward"
                nohup kubectl port-forward svc/frontend-service 3000:80 -n "$NAMESPACE" \
                    >> /tmp/pf-frontend.log 2>&1 &
                sleep 1
                ss -tlnp 2>/dev/null | grep -q ":3000 " \
                    && success "Frontend :3000 → active (direct, Ingress unavailable)" \
                    || warn "Frontend port-forward failed — check /tmp/pf-frontend.log"
            fi
        else
            # No dev Ingress: direct port-forward to frontend-service ClusterIP.
            info "Starting frontend port-forward (:3000 → $NAMESPACE/frontend-service:80)..."
            nohup kubectl port-forward svc/frontend-service 3000:80 -n "$NAMESPACE" \
                > /tmp/pf-frontend.log 2>&1 &
            sleep 1
            if ss -tlnp 2>/dev/null | grep -q ":3000 "; then
                success "Frontend :3000 → active"
            else
                warn "Frontend port-forward may have failed — check /tmp/pf-frontend.log"
            fi
        fi
    else
        success "Frontend :3000 already running"
    fi

    # 4. Authentik port-forward — required for the browser to follow OAuth2 redirects
    #    to localhost:9000. Verify namespace ownership; reclaim :9000 if another
    #    project's Authentik (e.g. sem-image-classifier) left a stale forward.
    if [ -n "${INFRA_NAMESPACE:-}" ]; then
        ensure_authentik_port_forward "$AUTHENTIK_PF_PORT"
    else
        info "INFRA_NAMESPACE not set — skipping Authentik port-forward (external Authentik assumed)"
    fi

    echo ""
    success "Access ready!"
    echo ""
    echo -e "  ${BOLD}On your LOCAL machine (if connecting via SSH):${NC}"
    echo "    ssh -L 3000:localhost:3000 -L 9000:localhost:9000 orfeo-vm"
    echo ""
    echo -e "  ${BOLD}Then open:${NC} ${GREEN}http://localhost:3000${NC}"
    echo ""
}

# ==============================================================================
# Restart: restart an app deployment without rebuilding
# ==============================================================================

restart_component() {
    local component="$1"
    ensure_kubeconfig

    case "$component" in
        backend|frontend|django-postgres)
            ;;
        authentik-server|authentik-worker|authentik-postgres|authentik-redis)
            fail "'$component' is in the infra layer — use: ./infra.sh restart $component"
            ;;
        *)
            warn "Component '$component' may not be in $NAMESPACE — attempting anyway"
            ;;
    esac

    info "Restarting $component (ns=$NAMESPACE)..."
    kubectl rollout restart "deployment/$component" -n "$NAMESPACE"
    rollout_with_retry "deployment/$component" "180s" "2" "$NAMESPACE"
    success "$component restarted"
}

# ==============================================================================
# Cleanup: delete the webapp namespace + optionally clean images from K3s nodes
# ==============================================================================

run_cleanup() {
    ensure_kubeconfig

    echo ""
    echo -e "${BOLD}Webapp — Cleanup${NC}"
    echo ""

    info "Deleting namespace '$NAMESPACE' and all its resources..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=60s 2>/dev/null || true
    success "Namespace $NAMESPACE deleted"

    read -p "Remove webapp container images from K3s nodes? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        for node in "${K3S_NODES[@]}"; do
            info "Removing images from $node..."
            ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "root@${node}" \
                "k3s ctr images rm ${BACKEND_IMAGE} ${FRONTEND_IMAGE}" \
                2>/dev/null || warn "Could not clean images on $node"
        done
        success "Images cleaned from all nodes"
    fi

    echo ""
    success "Webapp cleanup complete!"
    echo ""
    echo "  To verify:  kubectl get all -n $NAMESPACE"
    echo "  To redeploy: ./app.sh deploy --rebuild"
    echo ""
}

# ==============================================================================
# dev-db: Local PostgreSQL via podman (for running Django without K3s)
#
# Starts a postgres:15-alpine container with credentials that match
# manifests/app/01-django-postgres.yaml so that Django's settings.py picks up
# PostgreSQL instead of falling back to SQLite.
#
# Workflow:
#   ./app.sh dev-db start
#   export DATABASE_HOST=localhost DATABASE_NAME=djangodb \
#          DATABASE_USER=djangouser DATABASE_PASSWORD=dev-db-password
#   cd ../backend && python manage.py migrate && python manage.py runserver
#
# ==============================================================================

run_verify() {
    step "Syntax-checking shell scripts"
    bash -n "$SCRIPT_DIR/app.sh"
    bash -n "$SCRIPT_DIR/infra.sh"
    success "Shell syntax OK"

    step "Installing backend test dependencies"
    python3 -m pip install --upgrade pip setuptools wheel -q
    python3 -m pip install "$PROJECT_DIR/backend[dev]" -q

    step "Backend tests with coverage"
    (
        cd "$PROJECT_DIR/backend"
        DJANGO_DEBUG=True python3 -m pytest --cov=storage --cov-report=term-missing -q
    )
    success "Backend tests passed"

    step "Frontend production build"
    (
        cd "$PROJECT_DIR/frontend"
        npm run build
    )
    success "Frontend build passed"

    echo ""
    success "All verification checks passed (matches CI verify job)"
}

dev_db() {
    local action="${1:-help}"
    case "$action" in
        start)
            if podman inspect "$DEV_PG_CONTAINER" &>/dev/null; then
                local state
                state=$(podman inspect --format '{{.State.Status}}' "$DEV_PG_CONTAINER")
                if [ "$state" = "running" ]; then
                    info "dev-postgres is already running on localhost:5432"
                else
                    info "Starting existing dev-postgres container..."
                    podman start "$DEV_PG_CONTAINER"
                fi
            else
                info "Creating and starting dev-postgres container..."
                podman run -d \
                    --name "$DEV_PG_CONTAINER" \
                    -e POSTGRES_DB="$DEV_PG_DB" \
                    -e POSTGRES_USER="$DEV_PG_USER" \
                    -e POSTGRES_PASSWORD="$DEV_PG_PASS" \
                    -p 5432:5432 \
                    "$DEV_PG_IMAGE"
            fi
            echo ""
            success "PostgreSQL is running. Export these vars then use Django normally:"
            echo ""
            echo "  export DATABASE_HOST=localhost"
            echo "  export DATABASE_NAME=$DEV_PG_DB"
            echo "  export DATABASE_USER=$DEV_PG_USER"
            echo "  export DATABASE_PASSWORD=$DEV_PG_PASS"
            echo ""
            echo "  cd ../backend"
            echo "  python manage.py migrate"
            echo "  python manage.py runserver"
            echo ""
            ;;
        stop)
            info "Stopping dev-postgres..."
            podman stop "$DEV_PG_CONTAINER" && success "Stopped"
            ;;
        destroy)
            info "Removing dev-postgres container and all data..."
            podman rm -f "$DEV_PG_CONTAINER" && success "Removed"
            ;;
        status)
            if podman inspect "$DEV_PG_CONTAINER" &>/dev/null; then
                local state
                state=$(podman inspect --format '{{.State.Status}}' "$DEV_PG_CONTAINER")
                info "dev-postgres: $state"
            else
                info "dev-postgres: not created"
            fi
            ;;
        *)
            echo "  Usage: ./app.sh dev-db <start|stop|destroy|status>"
            echo ""
            echo "  start    Create and start local PostgreSQL (localhost:5432)"
            echo "  stop     Stop the container (data preserved)"
            echo "  destroy  Remove container and all data"
            echo "  status   Show container state"
            ;;
    esac
}

# ==============================================================================
# Main
# ==============================================================================

usage() {
    echo -e "${BOLD}app.sh — Webapp Management (bucket-explorer namespace)${NC}"
    echo ""
    echo "  Usage: ./app.sh <command> [args]"
    echo ""
    echo "  Deployment:"
    echo "    deploy [--rebuild] [--skip-authentik-check]"
    echo "                   Full webapp deploy (namespace → secrets → postgres → backend → frontend)"
    echo "    deploy-namespace"
    echo "                   Create bucket-explorer namespace only"
    echo ""
    echo "  Inner-loop rebuilds (no manifest re-apply):"
    echo "    backend        Rebuild + push backend image → GHCR; restart deployment (~60s)"
    echo "    frontend       Rebuild + push frontend image → GHCR; restart deployment (~90s)"
    echo "    all            Rebuild + push both; restart both deployments"
    echo ""
    echo "  Operations:"
    echo "    status         Show pods in $NAMESPACE namespace"
    echo "    logs [comp]    Tail logs (backend, frontend)"
    echo "    access         SSH tunnel + port-forwards for frontend (:3000) and Authentik (:9000)"
    echo "    restart <comp> Restart a deployment without rebuild"
    echo "    cleanup        Delete $NAMESPACE namespace + optionally clean images from K3s nodes"
    echo "    dev-db <act>   Local PostgreSQL via podman (start|stop|destroy|status)"
    echo "    verify         Run backend tests + frontend build (same as CI verify job)"
    echo ""
    echo "  Namespace  : $NAMESPACE"
    echo "  Images     : $BACKEND_IMAGE"
    echo "               $FRONTEND_IMAGE"
    echo ""
    echo "  Environment overlays (k8s/env/dev/):"
    echo "    app-secrets.yaml      — backend-secret"
    echo "    backend-config.yaml   — backend ConfigMap"
    echo "    Optional local overrides (gitignored): *.local.yaml"
    echo ""
    echo "  Companion script: ./infra.sh  (manages Authentik in $INFRA_NAMESPACE)"
    echo ""
}

COMMAND="${1:-}"
shift || true

case "$COMMAND" in
    deploy)
        run_deploy "$@"
        ;;
    deploy-namespace)
        [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] && {
            echo "Usage: ./app.sh deploy-namespace"; exit 0; }
        deploy_namespace
        ;;
    backend)
        ensure_kubeconfig
        redeploy_component backend
        ;;
    frontend)
        ensure_kubeconfig
        redeploy_component frontend
        ;;
    all)
        ensure_kubeconfig
        redeploy_component backend
        redeploy_component frontend
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "${1:-backend}"
        ;;
    access)
        setup_access
        ;;
    restart)
        [ -z "${1:-}" ] && fail "Usage: ./app.sh restart <component>"
        restart_component "$1"
        ;;
    cleanup)
        run_cleanup
        ;;
    dev-db)
        dev_db "${1:-help}"
        ;;
    verify)
        run_verify
        ;;
    *)
        usage
        exit 1
        ;;
esac
