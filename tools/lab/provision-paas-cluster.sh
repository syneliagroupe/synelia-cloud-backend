#!/usr/bin/env bash
# Crée un cluster Magnum PaaS sur le lab (admin Keystone requis) puis bootstrap Zot/opérateurs.
#
# Prérequis : CTRL1_ROOT_PASSWORD (SSH root@ctrl1), helm/kubectl sur la machine qui lance le script.
#
# Usage :
#   export CTRL1_ROOT_PASSWORD='…'
#   CLUSTER_NAME=paas-univers-test ./tools/lab/provision-paas-cluster.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-paas-univers-test}"
CTRL1="${CTRL1:-192.168.26.235}"
DEMO_NET="${DEMO_NET:-2265d89f-5dcb-4a3c-86bf-a1195a1159a3}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [ -z "${CTRL1_ROOT_PASSWORD:-}" ]; then
  echo "Définir CTRL1_ROOT_PASSWORD (SSH root@${CTRL1})." >&2
  exit 1
fi

ssh_ctrl1() {
  env -u SSH_AUTH_SOCK sshpass -p "$CTRL1_ROOT_PASSWORD" \
    ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new "root@${CTRL1}" "$1"
}

need() { command -v "$1" >/dev/null || { echo "'$1' introuvable" >&2; exit 1; }; }
need helm
need kubectl

ADMIN_PW="$(ssh_ctrl1 "awk -F= '/^export OS_PASSWORD=/{print \$2}' /etc/kolla/admin-openrc.sh | tr -d \"'\"")"
[ -n "$ADMIN_PW" ] || { echo "Impossible de lire OS_PASSWORD sur ctrl1" >&2; exit 1; }

echo "== création Magnum ${CLUSTER_NAME} (Python / admin) =="
CLUSTER_ID="$(cd "$ROOT" && uv run python <<PY
import openstack
pwd = """${ADMIN_PW}"""
conn = openstack.connect(
    load_yaml_config=False, load_envvars=False,
    auth_type="password", auth_url="http://192.168.26.234:5000/v3",
    username="admin", password=pwd, project_name="admin",
    user_domain_name="Default", project_domain_name="Default",
    region_name="RegionOne", interface="internal", api_timeout=120,
)
tpl = next(iter(conn.container_infra.cluster_templates()))
labels = dict(getattr(tpl, "labels", None) or {})
labels["octavia_provider"] = "amphora"
cl = conn.container_infra.create_cluster(
    name="${CLUSTER_NAME}",
    cluster_template_id=tpl.id,
    master_count=1,
    node_count=1,
    create_timeout=60,
    labels=labels,
    fixed_network="${DEMO_NET}",
)
print(cl.id)
PY
)"
echo "Cluster soumis : ${CLUSTER_ID}"

echo "== attente CREATE_COMPLETE =="
cd "$ROOT" && uv run python <<PY
import time, openstack
pwd = """${ADMIN_PW}"""
conn = openstack.connect(
    load_yaml_config=False, load_envvars=False,
    auth_type="password", auth_url="http://192.168.26.234:5000/v3",
    username="admin", password=pwd, project_name="admin",
    user_domain_name="Default", project_domain_name="Default",
    region_name="RegionOne", interface="internal", api_timeout=120,
)
cid = "${CLUSTER_ID}"
for _ in range(60):
    cl = conn.container_infra.get_cluster(cid)
    print(cl.status, (getattr(cl, "status_reason", None) or "")[:100])
    if cl.status not in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", None):
        break
    time.sleep(30)
if cl.status != "CREATE_COMPLETE":
    raise SystemExit(f"Cluster en {cl.status}")
PY

STACK="$(ssh_ctrl1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get cluster -n magnum-system -l cluster-uuid=${CLUSTER_ID} -o jsonpath='{.items[0].metadata.name}'")"
[ -n "$STACK" ] || { echo "Stack CAPI introuvable pour ${CLUSTER_NAME}" >&2; exit 1; }

KCFG="/tmp/${CLUSTER_NAME}.kubeconfig"
ssh_ctrl1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get secret ${STACK}-kubeconfig -n magnum-system -o jsonpath='{.data.value}' | base64 -d" >"$KCFG"
chmod 600 "$KCFG"

echo "== bootstrap PaaS =="
export KUBECONFIG="$KCFG" REGISTRY_STORAGE_CLASS=block-ssd
bash "$ROOT/tools/paas-bootstrap.sh"

echo ""
echo "SYNELIA_PAAS_CLUSTER_ID=${CLUSTER_ID}"
echo "Kubeconfig admin : ${KCFG}"
