#!/usr/bin/env bash
# Bootstrap « PaaS » d'un cluster Kubernetes Magnum/CAPI : registre local Zot,
# confiance containerd des nœuds, puis opérateurs de bases de données.
#
# À exécuter APRÈS qu'un cluster soit `CREATE_COMPLETE / HEALTHY`, avec un
# KUBECONFIG admin du cluster workload. Idempotent : ré-exécutable sans casser.
#
# Ordre imposé :
#   0. Préflight (endpoints OpenStack publics, capacité)
#   1. Registre Zot (volume Cinder)
#   2. Confiance containerd des nœuds vers Zot (certs.d + config_path)
#   3. Opérateurs : CloudNativePG, MariaDB, Redis, ECK, MongoDB
#   4. (option) Bases de démonstration
#
# Usage :
#   export KUBECONFIG=/chemin/workload.kubeconfig
#   REGISTRY_STORAGE_CLASS=block-ssd ./tools/paas-bootstrap.sh
#
# Variables :
#   REGISTRY_STORAGE_CLASS (défaut block-ssd)  classe Cinder CSI pour Zot
#   REGISTRY_STORAGE_SIZE  (défaut 8Gi)
#   SKIP_DATABASES=1       n'installe pas les bases de démonstration
set -euo pipefail

REGISTRY_NS="${REGISTRY_NS:-registry}"
REGISTRY_STORAGE_CLASS="${REGISTRY_STORAGE_CLASS:-block-ssd}"
REGISTRY_STORAGE_SIZE="${REGISTRY_STORAGE_SIZE:-8Gi}"
APPS_NS="${APPS_NS:-demo-apps}"
SKIP_DATABASES="${SKIP_DATABASES:-0}"

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERREUR: %s\033[0m\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null || die "'$1' introuvable dans le PATH."; }
need kubectl
need helm
[ -n "${KUBECONFIG:-}" ] || die "KUBECONFIG non défini (kubeconfig admin du cluster workload)."

# ─────────────────────────────────────────────────────────────────────────────
log "0. Préflight"
kubectl get nodes >/dev/null || die "cluster injoignable"
kubectl get nodes -o wide
# Le CCM (Service type=LoadBalancer) lit le catalogue Keystone **public** : si une
# entrée publique pointe encore sur une IP interne (192.168.26.x), tout LB restera
# PENDING. À vérifier côté OpenStack :
#   openstack endpoint list --interface public | grep 192.168.26
# et basculer keystone/neutron/nova/cinder/octavia/placement/glance sur leurs vhosts.
# (Octavia exige aussi un provider activé : le driver CAPI défaut à `amphorav2`,
#  ce lab n'a que `amphora` — cf. backend `magnum.py`, label `octavia_provider`.)
# Quota Cinder : chaque PVC = un volume réel ; le défaut (10) sature vite.
#   openstack quota set --volumes 30 <projet-du-cluster>

# ─────────────────────────────────────────────────────────────────────────────
# Beaucoup d'opérateurs (MongoDB, etc.) créent des PVC sans `storageClassName` :
# sans classe par défaut, ils restent Pending. On marque la classe Cinder comme
# défaut du cluster.
kubectl patch storageclass "$REGISTRY_STORAGE_CLASS" \
  -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' >/dev/null 2>&1 || true

log "1. Registre Zot (persistance Cinder)"
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm repo add ot-helm https://ot-container-kit.github.io/helm-charts/ >/dev/null 2>&1 || true
helm repo add mariadb-operator https://mariadb-operator.github.io/mariadb-operator >/dev/null 2>&1 || true
helm repo add elastic https://helm.elastic.co >/dev/null 2>&1 || true
helm repo add mongodb https://mongodb.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add percona https://percona.github.io/percona-helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1 || true

cat >/tmp/zot-values.yaml <<EOF
persistence: true
pvc:
  storage: ${REGISTRY_STORAGE_SIZE}
  storageClassName: ${REGISTRY_STORAGE_CLASS}
service:
  type: LoadBalancer
  port: 5000
EOF
if ! helm upgrade --install zot oci://ghcr.io/project-zot/helm-charts/zot \
  -n "$REGISTRY_NS" --create-namespace -f /tmp/zot-values.yaml --timeout 8m 2>/tmp/zot-helm.err; then
  # Un StatefulSet n'accepte pas de changement de volumeClaimTemplate : si Zot est
  # déjà installé avec une autre taille, l'upgrade échoue là — ce n'est pas une
  # erreur de bootstrap, le registre existant tourne.
  if grep -q "updates to statefulset spec" /tmp/zot-helm.err; then
    echo "AVERTISSEMENT: Zot déjà installé (volumeClaimTemplate différent) — inchangé."
  else
    cat /tmp/zot-helm.err >&2; exit 1
  fi
fi

log "Attente du VIP Zot..."
REGISTRY_ADDR=""
for _ in $(seq 1 60); do
  REGISTRY_ADDR=$(kubectl -n "$REGISTRY_NS" get svc zot -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$REGISTRY_ADDR" ] && break
  sleep 5
done
[ -n "$REGISTRY_ADDR" ] || die "Zot n'a pas obtenu de VIP Octavia (catalogue public / provider ?)"
REGISTRY_HOST="${REGISTRY_ADDR}:5000"
log "Registre Zot : http://${REGISTRY_HOST}"

# ─────────────────────────────────────────────────────────────────────────────
log "2. Confiance containerd des nœuds (certs.d + config_path)"
# containerd 2.x ne lit /etc/containerd/certs.d que si `config_path` est renseigné
# sous le plugin CRI images (défaut : chaîne vide → HTTPS only, donc 400/HTTP-only
# sur un registre en clair). On pose les deux puis on redémarre containerd une fois.
cat >/tmp/registry-trust.yaml <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: registry-trust
  namespace: ${REGISTRY_NS}
  labels: {app: registry-trust}
spec:
  selector: {matchLabels: {app: registry-trust}}
  template:
    metadata: {labels: {app: registry-trust}}
    spec:
      hostPID: true
      tolerations: [{operator: Exists}]
      containers:
        - name: trust
          image: alpine:3.20
          securityContext: {privileged: true}
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              HOST="${REGISTRY_HOST}"
              DIR="/host/etc/containerd/certs.d/\${HOST}"
              mkdir -p "\$DIR"
              printf 'server = "http://%s"\n\n[host."http://%s"]\n  capabilities = ["pull", "resolve", "push"]\n' "\$HOST" "\$HOST" > "\$DIR/hosts.toml"
              if ! grep -q "io.containerd.cri.v1.images'.registry" /host/etc/containerd/config.toml; then
                printf "\n[plugins.'io.containerd.cri.v1.images'.registry]\n  config_path = '/etc/containerd/certs.d'\n" >> /host/etc/containerd/config.toml
              fi
              if [ ! -f /host/tmp/.registry-trust-done ]; then
                nsenter -t 1 -m -u -i -n -p -- systemctl restart containerd || true
                touch /host/tmp/.registry-trust-done
              fi
              sleep infinity
          volumeMounts:
            - {name: host, mountPath: /host, mountPropagation: Bidirectional}
      volumes:
        - {name: host, hostPath: {path: /}}
EOF
kubectl apply -f /tmp/registry-trust.yaml
kubectl -n "$REGISTRY_NS" rollout status ds/registry-trust --timeout=5m

# ─────────────────────────────────────────────────────────────────────────────
log "3. Opérateurs de bases de données"

# CloudNativePG (PostgreSQL)
helm upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace --wait --timeout 8m

# MariaDB operator (CRDs puis opérateur)
helm upgrade --install mariadb-operator-crds mariadb-operator/mariadb-operator-crds -n mariadb-system --create-namespace --wait --timeout 8m
helm upgrade --install mariadb-operator mariadb-operator/mariadb-operator -n mariadb-system --wait --timeout 8m

# Redis operator (OT-container-kit)
helm upgrade --install redis-operator ot-helm/redis-operator -n redis-operator --create-namespace --wait --timeout 8m

# ECK : les CRDs vivent dans un chart séparé ; Helm refuse d'adopter des CRDs
# déjà posés par un autre release (même problème pour MongoDB ci-dessous). Deux
# cas possibles selon l'ordre d'installation — on tolère les deux, puis on
# réattribue les annotations d'ownership avant d'installer l'opérateur.
install_crds() {  # <release> <chart> <namespace>
  if ! helm upgrade --install "$1" "$2" -n "$3" --create-namespace --wait --timeout 8m 2>/tmp/crd.err; then
    if grep -q "cannot be imported into the current release" /tmp/crd.err; then
      echo "AVERTISSEMENT: CRDs déjà présents pour $1 — inchangé."
    else
      cat /tmp/crd.err >&2; exit 1
    fi
  fi
}
reown_crds() {  # <release> <namespace> <crd grep pattern>
  for c in $(kubectl get crd -o name | grep "$3"); do
    kubectl annotate "$c" meta.helm.sh/release-name="$1" meta.helm.sh/release-namespace="$2" --overwrite >/dev/null 2>&1 || true
    kubectl label "$c" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null 2>&1 || true
  done
}

install_crds eck-operator-crds elastic/eck-operator-crds elastic-system
reown_crds eck-operator elastic-system 'k8s.elastic.co'
helm upgrade --install eck-operator elastic/eck-operator -n elastic-system --skip-crds --wait --timeout 8m

# MongoDB : opérateur **Percona Server for MongoDB**. L'opérateur MongoDB Community
# (officiel) a un agent cassé sur ce cluster — `readinessprobe` panique, l'agent ne
# publie jamais `agent.mongodb.com/version`, la CR reste `Pending` (mongod tourne mais
# le ReplicaSet n'est jamais déclaré prêt). Percona est fiable et sans ce défaut.
install_crds psmdb-operator-crds percona/psmdb-operator-crds psmdb-system
reown_crds psmdb-operator psmdb-system 'psmdb.percona.com'
helm upgrade --install psmdb-operator percona/psmdb-operator -n psmdb-system --skip-crds --set watchAllNamespaces=true --wait --timeout 8m

log "Opérateurs :"
kubectl get pods -A | grep -E 'cnpg-system|mariadb-system|redis-operator|elastic-system|mongodb-system' || true

# ─────────────────────────────────────────────────────────────────────────────
if [ "$SKIP_DATABASES" != "1" ]; then
  log "4. Bases de démonstration dans ${APPS_NS}"
  kubectl create ns "$APPS_NS" 2>/dev/null || true
  kubectl -n "$APPS_NS" create secret generic demo-mariadb-root --from-literal=password=DemoRoot123 2>/dev/null || true
  kubectl -n "$APPS_NS" create secret generic demo-mariadb-user --from-literal=password=DemoUser123 2>/dev/null || true
  # (MongoDB/Percona génère lui-même ses secrets `internal-*` ; rien à créer ici.)

  kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: demo-cnpg, namespace: ${APPS_NS}}
spec:
  instances: 1
  storage: {size: 2Gi, storageClass: ${REGISTRY_STORAGE_CLASS}}
  bootstrap: {initdb: {database: appdb, owner: appuser}}
  resources: {requests: {cpu: 200m, memory: 256Mi}}
---
apiVersion: k8s.mariadb.com/v1alpha1
kind: MariaDB
metadata: {name: demo-mariadb, namespace: ${APPS_NS}}
spec:
  rootPasswordSecretKeyRef: {name: demo-mariadb-root, key: password}
  database: appdb
  username: appuser
  passwordSecretKeyRef: {name: demo-mariadb-user, key: password}
  storage: {size: 2Gi, storageClassName: ${REGISTRY_STORAGE_CLASS}}
  replicas: 1
  resources: {requests: {cpu: 200m, memory: 256Mi}}
---
apiVersion: redis.redis.opstreelabs.in/v1beta2
kind: Redis
metadata: {name: demo-redis, namespace: ${APPS_NS}}
spec:
  kubernetesConfig:
    image: quay.io/opstree/redis:v7.4.11
    imagePullPolicy: IfNotPresent
  storage:
    volumeClaimTemplate:
      spec:
        storageClassName: ${REGISTRY_STORAGE_CLASS}
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: 1Gi}}
  podSecurityContext: {runAsUser: 1000, fsGroup: 1000}
---
apiVersion: psmdb.percona.com/v1
kind: PerconaServerMongoDB
metadata: {name: demo-psmdb, namespace: ${APPS_NS}}
spec:
  crVersion: 1.23.0
  image: percona/percona-server-mongodb:7.0.14-8
  unsafeFlags: {replsetSize: true}
  replsets:
    - name: rs0
      size: 1
      volumeSpec:
        persistentVolumeClaim:
          storageClassName: ${REGISTRY_STORAGE_CLASS}
          resources: {requests: {storage: 2Gi}}
EOF
  echo "Bases créées. Vérifier : kubectl -n $APPS_NS get cluster.postgresql.cnpg.io,mariadb,redis,mongodbcommunity"
fi

log "Terminé."
echo "Registre : http://${REGISTRY_HOST} (pousser avec 'crane copy <img> ${REGISTRY_HOST}/<nom>:<tag> --insecure')"
