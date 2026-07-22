# Setting Up the Development Environment

This document walks through building the Stencil virtual datacenter from scratch: cloning the provisioning repositories, configuring each layer, and adding the components specific to this project. A person who completes these steps will have an environment identical to the one used to develop and validate Buckets Explorer.

Before starting: read [dev-environment-overview.md](./dev-environment-overview.md) to understand what you are building and why.

> The addresses in this document use the TEST-NET-2 documentation block (`198.51.100.0/24`). Replace them with the addresses assigned by your own Stencil deployment.

---

## Prerequisites

**Hardware:**

See [dev-environment-overview.md](./dev-environment-overview.md#host-machine-requirements) for the full requirements rationale. Summary:

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 8 physical cores (VT-x/AMD-V required) | 16+ cores |
| RAM | 64 GB | 128 GB |
| Disk | 300 GB SSD | 500 GB NVMe |

Linux host OS is required — Fedora or RHEL/CentOS-compatible recommended.

**Software on the host:**

```bash
# Fedora/RHEL
sudo dnf install -y git opentofu libvirt libvirt-client qemu-kvm \
    wget ansible python3 helm kubectl

# Enable and start libvirt
sudo systemctl enable --now libvirtd

# Add your user to the libvirt group (log out and back in after this)
sudo usermod -aG libvirt $(whoami)
```

Minimum versions: OpenTofu ≥ 1.6, Ansible ≥ 2.15, Helm ≥ 3.12, Python ≥ 3.10.

**SSH key** — must exist at `~/.ssh/id_rsa` (the provisioning tools expect RSA):

```bash
# Check if it exists
ls ~/.ssh/id_rsa.pub

# If not, generate one
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
```

**GitHub account with a Personal Access Token** — required in Step 7 for pushing container images to GHCR. The token needs `write:packages` scope. Create one at GitHub → Settings → Developer settings → Personal access tokens.

---

## Step 1: Clone the Stencil Repositories

The Stencil project is hosted on GitLab under AREA Science Park. Clone all four provisioning repositories:

```bash
mkdir -p ~/virtual_cluster_setup
cd ~/virtual_cluster_setup

# Virtualization layer
git clone https://gitlab.com/area7/datacenter/codes/stencil/tofu-libvirt

# Storage layer
git clone https://gitlab.com/area7/datacenter/codes/stencil/ceph-provisioning

# Identity and DNS layer
git clone https://gitlab.com/area7/datacenter/codes/stencil/freeipa-provisioning

# Kubernetes layer
git clone https://gitlab.com/area7/datacenter/codes/stencil/kubernetes-provisioning
```

The official documentation for each repository is at:
> https://gitlab.com/area7/datacenter/codes/stencil/docs/-/tree/main/docs

---

## Step 2: Deploy Virtual Machines (tofu-libvirt)

```bash
cd ~/virtual_cluster_setup/tofu-libvirt
```

### 2.1 Configure vars.json

Open `vars.json` and set the following fields. Using the wrong values is a common source of later failures.

**Critical: always use absolute paths for `ssh_key_path`.** A relative path like `~/.ssh/id_rsa.pub` silently fails on some libvirt versions.

```json
{
  "ssh_key_path": "/root/.ssh/id_rsa.pub",
  "disk_size_per_vm": 15
}
```

> **Disk size warning:** If `disk_size_per_vm` is absent or `null`, libvirt falls back to the base image size (~5 GB). Ceph OSD nodes need at least 15 GB for the root volume plus the OSD data disks. Always set this field explicitly.

Minimum recommended disk sizes by node type (set the OSD data disks separately in the Ceph configuration):

| Node type      | `disk_size_per_vm` |
|----------------|--------------------|
| K3s nodes      | 15 GB              |
| Ceph svc nodes | 20 GB              |
| Ceph OSD nodes | 15 GB root + 3×20 GB OSD data |

### 2.2 Download the Base Image

The `tofu-libvirt` project requires a Fedora cloud image. Check `vars.json` for the expected image name and download it to the expected location (usually `~/images/`):

```bash
mkdir -p ~/images
# Check the image URL in vars.json or the project README, then:
wget -O ~/images/fedora-cloud.qcow2 <image-url-from-vars.json>
```

### 2.3 Initialize and Apply

```bash
tofu init
tofu plan    # review what will be created
tofu apply   # creates all 10 VMs
```

### 2.4 Verify

```bash
virsh list --all
# Should show 10 running VMs: ipa01, kube01-03, ceph-svc01-03, ceph-osd01-03

virsh net-dhcp-leases TOFU-devel
# Should show all VMs with their IP addresses on 198.51.100.0/24
```

Test SSH connectivity:

```bash
ssh -o StrictHostKeyChecking=accept-new root@198.51.100.10 "hostname"
# Should print: kube01
```

---

## Step 3: Deploy Ceph (ceph-provisioning)

```bash
cd ~/virtual_cluster_setup/ceph-provisioning
```

### 3.1 Install Ansible Galaxy Requirements

```bash
ansible-galaxy install -r requirements.yml
```

### 3.2 Verify the Inventory

Check `inventory.yml` to confirm the IP addresses match the VMs created in Step 2. The defaults should match if you used the standard `vars.json`.

### 3.3 Run the Playbook

Ensure all Ceph VMs are running before executing:

```bash
virsh list --all | grep ceph   # all should be "running"
ansible-playbook -i inventory.yml ceph_installation.yml
```

This takes 20–30 minutes. The playbook is idempotent — if it fails, fix the issue and re-run.

### 3.4 Verify Ceph Health

```bash
ssh root@198.51.100.90 "ceph -s"
# Look for: health: HEALTH_OK
# and: X osds: Y up, Y in
```

### Known Issue: RGW SSL Certificate Missing

The default `ceph_installation.yml` may configure the RGW haproxy ingress on port 443 without an SSL certificate. This causes a TLS handshake error:

```
curl: (35) TLS connect error: wrong version number
```

**Fix:** Generate a self-signed certificate and update the ingress spec:

```bash
ssh root@198.51.100.90 bash <<'EOF'
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/rgw.key -out /tmp/rgw.crt \
  -subj '/CN=198.51.100.110/O=Stencil/C=IT' \
  -addext 'subjectAltName=IP:198.51.100.110'
cat /tmp/rgw.crt /tmp/rgw.key > /tmp/rgw.pem
EOF
```

Then apply an updated ingress spec that includes `ssl_cert`. See the full procedure in the Ceph provisioning project README.

---

## Step 4: Deploy FreeIPA (freeipa-provisioning)

```bash
cd ~/virtual_cluster_setup/freeipa-provisioning
ansible-galaxy install -r requirements.yml
ansible-playbook -i inventory.yml freeipa_installation.yml
```

### Verify

```bash
ssh root@198.51.100.70 "ipactl status"
# All services should show RUNNING
```

---

## Step 5: Deploy K3s (kubernetes-provisioning)

```bash
cd ~/virtual_cluster_setup/kubernetes-provisioning
ansible-galaxy install -r requirements.yml
```

### 5.1 Configure vars_files/default.yml

The relevant networking configuration (defaults are correct for the Stencil IP range):

```yaml
cluster_cidr: "<POD_CIDR>"
service_cidr: "<SERVICE_CIDR>"
cluster_dns: "<CLUSTER_DNS_IP>"
k3s_flannel_backend: "vxlan"

k3s_ingress: "nginx"     # default; haproxy-4 is installed separately in Step 6
install_metallb: true
metallb_pool: "198.51.100.100-198.51.100.105"
kube_vip: "198.51.100.105"
k3s_datastore: "etcd"
control_plane_ha: true
```

### 5.2 Run the Playbook

The K3s provisioning requires **both** inventories: the Kubernetes inventory for K3s itself, and the FreeIPA inventory for CoreDNS forwarding configuration.

```bash
ansible-playbook kubernetes_installation.yml \
  -i inventory/kubernetes_server.yml \
  -i inventory/freeipa_cluster.yml
```

> **Important:** If you run the playbook with only the Kubernetes inventory, it will fail with `'dict object' has no attribute 'ipaserver'` because the CoreDNS template references the FreeIPA group.

### 5.3 Fetch the kubeconfig

```bash
scp root@198.51.100.10:/etc/rancher/k3s/k3s.yaml /tmp/k3s.yaml
sed -i 's/127.0.0.1/198.51.100.10/g' /tmp/k3s.yaml
export KUBECONFIG=/tmp/k3s.yaml
kubectl get nodes
```

Expected output — all three nodes Ready:

```
NAME                 STATUS   ROLES                       AGE
kube01.stencil.com   Ready    control-plane,etcd,master   5m
kube02.stencil.com   Ready    control-plane,etcd,master   4m
kube03.stencil.com   Ready    control-plane,etcd,master   3m
```

### Known Issue: Etcd Split-Brain

If the Ansible role is outdated, it may configure all three nodes with `cluster-init: true`, causing each to form an independent etcd cluster. Symptom: `kubectl get nodes` returns "connection refused" on all nodes.

**Fix:** Only `kube01` should have `cluster-init: true`. `kube02` and `kube03` should have `server: https://198.51.100.10:6443` instead. Manual recovery:

```bash
# 1. Stop all nodes
for ip in 198.51.100.10 198.51.100.11 198.51.100.12; do
  ssh root@$ip "systemctl stop k3s-server && rm -rf /var/lib/rancher/k3s/server/db/* /var/lib/rancher/k3s/server/tls/*"
done

# 2. Write correct config to kube01 (/etc/rancher/k3s/config.yaml):
#    cluster-init: true (ONLY on kube01)

# 3. Write correct config to kube02 and kube03 (/etc/rancher/k3s/config.yaml):
#    server: https://198.51.100.10:6443 (join kube01)

# 4. Start kube01 first, wait 90s, then start the others
ssh root@198.51.100.10 "systemctl start k3s-server"
sleep 90
ssh root@198.51.100.11 "systemctl start k3s-server"
ssh root@198.51.100.12 "systemctl start k3s-server"
```

---

## Step 6: Install the HAProxy Ingress Controller

> **This step is a deliberate deviation from the Stencil base provisioning.** Stencil's `kubernetes-provisioning` installs an nginx ingress by default (`k3s_ingress: "nginx"` in `vars_files/default.yml`). This step adds a *second* ingress controller with IngressClass `haproxy-4` to match the production cluster's IngressClass name. Both controllers coexist; the application manifests use only `haproxy-4`.
>
> **This step is optional.** If you skip it, the app still deploys and works — `app.sh access` automatically falls back to direct `kubectl port-forward` to the frontend pod. The HAProxy path is valuable when you want to test the same ingress routing chain as production (HAProxy → nginx → backend).

**Why `haproxy-4`:** The production cluster at AREA Science Park uses a HAProxy-based ingress controller with IngressClass `haproxy-4`. Using the same name in development means the Kubernetes manifests (`k8s/manifests/app/04-ingress.dev.yaml`, `04-ingress.yaml`) work in both environments without modification.

```bash
# Add the HAProxy Technologies Helm chart repository
helm repo add haproxytech https://haproxytech.github.io/helm-charts
helm repo update

# Before installing, verify available options for the current chart version:
helm show values haproxytech/kubernetes-ingress | grep -E "ingressClass|service.type"

# Install the controller in the haproxy-ingress namespace
# Key settings: IngressClass name must be haproxy-4 to match production
helm install haproxy-ingress haproxytech/kubernetes-ingress \
  --namespace haproxy-ingress \
  --create-namespace \
  --set "controller.ingressClassResource.name=haproxy-4" \
  --set "controller.ingressClass=haproxy-4" \
  --set "controller.service.type=NodePort"
```

> **Note:** The `--set` parameter names above reflect the standard HAProxy Technologies chart defaults. If installation fails with "unknown parameter", run `helm show values haproxytech/kubernetes-ingress` to confirm current parameter paths for your chart version.

### Verify

```bash
kubectl get ingressclass haproxy-4
# NAME        CONTROLLER                                  PARAMETERS   AGE
# haproxy-4   haproxy.org/ingress-controller-haproxy-4   <none>        1m

kubectl get pods -n haproxy-ingress
# haproxy-ingress-kubernetes-ingress-...   Running
```

The controller listens on a NodePort. `app.sh access` detects it automatically and forwards `localhost:3000` to that NodePort via SSH tunnel.

---

## Step 7: Set Up Container Registry Credentials (GHCR)

The application images are published to **GitHub Container Registry** (GHCR).
`app.sh` uses the local token both to push images and to create the namespace-scoped
pull secret required when those packages are private.

### 7.1 Create a GitHub Personal Access Token

1. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
2. Create a new token with `read:packages` and `write:packages` scopes
3. Copy the token (`ghp_...`)

### 7.2 Create the k8s/.env File

A committed template `k8s/.env.example` documents the required variable. Copy it and fill in your token:

```bash
cd /root/s3bucket_manager_app/k8s
cp .env.example .env
# Edit .env: set GHCR_TOKEN=ghp_your_token_here
chmod 600 .env
```

This file is gitignored. `app.sh` sources it automatically for `podman push` and `podman login` operations.

---

## Step 8: K8s API Access Tunnel

This step produces **`/tmp/k3s-tunnel-kubeconfig.yaml`** — the patched kubeconfig file used by all deploy scripts and documented in [Development deployment operations](dev-deployment-operations.md#how-the-kubeconfig-file-is-created). You do not create it manually in normal use.

K3s exposes its API on port 6443, which is firewalled on the VMs — a direct `kubectl` from the deployment host will fail. Access requires an SSH tunnel that forwards a local port to the K3s API on the VM.

**In normal use, run `./app.sh access` at the start of each session.** It sets up the SSH tunnel, fetches and patches the kubeconfig (if the file is missing), and starts port-forwards for the frontend and Authentik — all in one command.

```bash
cd k8s
./app.sh access          # creates /tmp/k3s-tunnel-kubeconfig.yaml if missing
export KUBECONFIG=/tmp/k3s-tunnel-kubeconfig.yaml
kubectl cluster-info   # should print API at 127.0.0.1:16443
```

**Manual steps (for reference when debugging tunnel issues):**

```bash
# 1. Open SSH tunnel: local port 16443 → kube01 port 6443
ssh -fNM -S /tmp/k3s-api-tunnel.sock \
    -L 16443:127.0.0.1:6443 \
    root@198.51.100.10

# 2. Fetch and patch kubeconfig to use the tunneled port
scp root@198.51.100.10:/etc/rancher/k3s/k3s.yaml /tmp/k3s-tunnel-kubeconfig.yaml
sed -i 's/127.0.0.1:6443/127.0.0.1:16443/g' /tmp/k3s-tunnel-kubeconfig.yaml

export KUBECONFIG=/tmp/k3s-tunnel-kubeconfig.yaml
kubectl cluster-info
```

`app.sh access` runs the same steps and additionally starts frontend and Authentik port-forwards.

---

## Step 9: Deploy the Application

With the cluster running and kubeconfig set, copy and fill the three environment overlay templates. Each template has `CHANGE_ME_*` or `REPLACE_WITH_*` placeholder values that must be set before deployment.

```bash
cd /root/s3bucket_manager_app/k8s

# 1. Authentik bootstrap secrets (required by ./infra.sh deploy)
cp env/dev/infra-secrets.yaml env/dev/infra-secrets.local.yaml
# Replace every CHANGE_ME_* placeholder in infra-secrets.local.yaml.
# Values include the Authentik signing secret, initial admin credential,
# and Authentik database credential.

# 2. Application secrets (Django + OIDC + RGWSquared credentials)
cp env/dev/app-secrets.yaml env/dev/app-secrets.local.yaml
# Replace every CHANGE_ME_* placeholder in app-secrets.local.yaml.
# Values include Django signing, Django database, RGWSquared API,
# Django admin, and OIDC client credentials.

# 3. Backend configuration (S3 endpoint + RGWSquared URL)
cp env/dev/backend-config.yaml env/dev/backend-config.local.yaml
# Set these fields in backend-config.local.yaml:
#   S3_ENDPOINT:              https://198.51.100.110
#   RGWSQUARED_URL:           URL of the RGWSquared microservice
#   OIDC_CLIENT_ID, OIDC_APPLICATION_SLUG: leave as-is; configure_authentik.py sets these

# Deploy Authentik (identity layer) — uses infra-secrets.local.yaml
./infra.sh deploy

# Build and deploy the application — uses app-secrets.local.yaml + backend-config.local.yaml
./app.sh deploy --rebuild
```

The scripts wait for pods to become healthy before continuing. After a successful deploy:

```bash
./app.sh access   # sets up SSH tunnels and port-forwards
# Open http://localhost:3000
```

### Access from your laptop

The development deployment host SSH alias is **`orfeo-vm`**. Port-forwards run on that host (frontend `:3000`, Authentik `:9000`). From your laptop, forward those ports through SSH:

```bash
ssh -L 3000:localhost:3000 -L 9000:localhost:9000 orfeo-vm
```

Then open `http://localhost:3000`. `./app.sh access` on the deployment host prints this command when setup completes.

> **Ongoing operations:** For day-to-day deploy, code updates, kubeconfig, and command reference, see [Development deployment operations](dev-deployment-operations.md).

---

## Step 10: Optional — Install the GitHub Actions Runner (ARC)

This step sets up the self-hosted runner for CI/CD. It is required only if you want pushes to `dev` to trigger automatic deploys.

```bash
# Set a GitHub PAT with 'repo' scope (different from the GHCR write token)
export GITHUB_PAT=ghp_your_repo_scope_token

cd /root/s3bucket_manager_app/k8s
./ci.sh install
```

This installs:

1. **cert-manager** (required by ARC for webhook TLS)
2. **ARC controller** — the Kubernetes operator that manages runner pods
3. **RunnerScaleSet** — 1–3 runner pods labelled `bucket-explorer-runner`

After installation, verify the runner appears in GitHub:

```
GitHub → Repository → Settings → Actions → Runners
# Should show: bucket-explorer-runner (online)
```

### What ARC Does

When a CI/CD job with `runs-on: bucket-explorer-runner` is queued, ARC:
1. Spawns a new pod from the runner image
2. The pod registers itself with GitHub using a short-lived token (ARC handles rotation)
3. The pod executes the job steps using its ServiceAccount (in-cluster kubectl access — no kubeconfig file needed)
4. The pod terminates; the next job gets a fresh pod

---

## Day-to-Day Access Pattern

After initial setup, each session follows this pattern:

```bash
# 1. Verify VMs are running
virsh list --all | grep -E "kube|ceph|ipa"

# 2. Check cluster health
ssh root@198.51.100.90 "ceph -s"
kubectl get nodes

# 3. Establish access tunnel + port-forwards (creates kubeconfig if missing)
cd k8s
./app.sh access
export KUBECONFIG=/tmp/k3s-tunnel-kubeconfig.yaml

# 4. Open http://localhost:3000
```

---

## Troubleshooting Quick Reference

| Symptom | First check | Fix |
|---------|-------------|-----|
| `kubectl` connection refused | SSH tunnel down | `./app.sh access` |
| K3s kubeconfig cert expired | Cluster was re-initialized | `scp` fresh kubeconfig from node |
| Ceph `HEALTH_WARN` | Check `ceph health detail` | Often a clock skew or OSD down; resolve per warning |
| Ceph RGW 503 | OSD disk full | Clean disk space on svc nodes, then `ceph orch daemon redeploy rgw.main.*` |
| SSH `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | VM was reprovisioned | `ssh-keygen -R 198.51.100.XX` |
| K3s etcd split-brain | All nodes had `cluster-init: true` | Follow the manual recovery procedure in Step 5 |
| `haproxy-4` IngressClass missing | HAProxy controller not installed or wrong release | Re-run Step 6 |
| Image pull failure on K3s pods | Missing/expired GHCR credentials, wrong repository, missing digest, or network issue | Re-run `./app.sh deploy` to refresh `ghcr-pull-secret`; inspect pod Events |
