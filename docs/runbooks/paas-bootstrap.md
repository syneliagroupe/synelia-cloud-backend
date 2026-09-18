# Runbook — Bootstrap PaaS d'un cluster Kubernetes (Zot + opérateurs)

À exécuter **après** qu'un cluster Magnum/CAPI soit `CREATE_COMPLETE / HEALTHY`.
Script : `tools/paas-bootstrap.sh` (idempotent). Ce document explique l'ordre et
les pièges — chacun a été rencontré et corrigé en direct sur le lab.

```
export KUBECONFIG=/chemin/vers/workload.kubeconfig
REGISTRY_STORAGE_CLASS=block-ssd ./tools/paas-bootstrap.sh
```

## Ordre imposé

### 0. Préflight — catalogue OpenStack public
Le CCM du cluster (qui provisionne les `Service type=LoadBalancer`) lit le
catalogue Keystone **public** depuis les nœuds tenants. Si une entrée publique
pointe sur une IP interne (`192.168.26.x`), tout LB reste `PENDING`.

```bash
openstack endpoint list --interface public | grep 192.168.26   # doit être vide
```
À basculer sur les vhosts `https://<service>.openstack-lab.dev01.ovh.smile.ci` :
`keystone` (**avec `/v3` explicite**, sinon gophercloud suit le `href` http du
version-doc → 301 → 401), `neutron`, `nova`, `cinder`, `octavia`, `placement`,
`glance`.

Provider Octavia : le driver CAPI défaut à `amphorav2` (provider VEXXHOST absent
d'un Octavia vanilla → 400 `Provider 'amphorav2' is not enabled`). Sur ce lab
seul `amphora` est activé. Le backend passe désormais `octavia_provider=amphora`
(label de cluster, surchargeable par `SYNELIA_PAAS_OCTAVIA_PROVIDER`) à la
création — un cluster créé avant ce correctif doit voir son secret workload
`kube-system/cloud-config` corrigé (`lb-provider=amphora`).

### 1. Registre Zot
Chart `oci://ghcr.io/project-zot/helm-charts/zot`, `service.type=LoadBalancer`
(VIP Octavia = adresse stable joignable par tous les nœuds), persistance sur la
classe Cinder CSI (`block-ssd`) → **vrai volume Cinder**.

```bash
kubectl -n registry get svc zot          # EXTERNAL-IP = VIP
kubectl -n registry get pvc              # Bound, storageClass block-ssd
openstack volume list | grep zot         # volume Cinder réel, in-use
```

### 2. Confiance containerd des nœuds
Zot sert en **HTTP**. containerd 2.x ne lit `/etc/containerd/certs.d` que si
`config_path` est renseigné sous le plugin CRI images — sinon il force HTTPS
(`server gave HTTP response to HTTPS client`). Le DaemonSet `registry-trust` :

1. écrit `/etc/containerd/certs.d/<VIP>:5000/hosts.toml` (`server = "http://…"`) ;
2. ajoute `[plugins.'io.containerd.cri.v1.images'.registry] config_path = '/etc/containerd/certs.d'` à `config.toml` ;
3. redémarre containerd une fois (garde-fou `/tmp/.registry-trust-done` sur l'hôte).

> Redémarrer containerd tue brièvement les pods du nœud — acceptable en bootstrap,
> à faire hors fenêtre de production.

Pousser une image (sans redémarrer Docker, qui sur ctrl1 redémarrerait tout
l'OpenStack) :

```bash
crane copy <image-publique> <VIP>:5000/<nom>:<tag> --insecure
curl -s http://<VIP>:5000/v2/_catalog
```

Vérifier le pull réel : un pod avec `image: <VIP>:5000/<nom>:<tag>` doit passer
`Running` (pas `ErrImagePull`).

### 3. Opérateurs (tous nativement, dans cet ordre)

| Opérateur | Namespace | Chart |
|---|---|---|
| CloudNativePG (PostgreSQL) | `cnpg-system` | `cnpg/cloudnative-pg` |
| MariaDB | `mariadb-system` | `mariadb-operator-crds` puis `mariadb-operator` |
| Redis (OT-container-kit) | `redis-operator` | `ot-helm/redis-operator` |
| Elastic (ECK) | `elastic-system` | `elastic/eck-operator-crds` puis `eck-operator` |
| MongoDB | `psmdb-system` | `percona/psmdb-operator-crds` puis `psmdb-operator` |

Pièges :
- **CRDs en chart séparé** (ECK, MongoDB) : installer le chart CRDs puis
  l'opérateur provoque `invalid ownership metadata … release-name must equal`.
  Le script réattribue les annotations `meta.helm.sh/release-*` + label
  `managed-by=Helm` sur les CRDs avant d'installer l'opérateur (`--skip-crds`).
- **Classe de stockage par défaut** : beaucoup d'opérateurs créent des PVC sans
  `storageClassName` (MongoDB `logs-volume`, etc.) → `Pending` silencieux. Le
  bootstrap marque la classe Cinder (`block-ssd`) comme défaut du cluster.
- **MongoDB → Percona, pas Community.** L'opérateur MongoDB Community (officiel)
  est cassé sur ce cluster : l'agent ne démarre pas (`readinessprobe` panique,
  aucune annotation `agent.mongodb.com/version` publiée), la CR reste `Pending`
  alors même que `mongod` répond. `percona/psmdb-operator` fonctionne sans ce
  défaut. Pièges Percona : (1) `--set watchAllNamespaces=true` (sinon la CR dans
  un autre namespace est ignorée) ; (2) `crVersion` doit égaler le **tag de
  l'image de l'opérateur** (`kubectl -n psmdb-system get deploy psmdb-operator -o
  jsonpath='{.spec.template.spec.containers[0].image}'`), pas la version du chart
  — sinon l'init-container tire un tag inexistant (`ErrImagePull`) ; (3) un
  replica set à 1 nœud exige `spec.unsafeFlags.replsetSize: true`. (Piège
  Community, pour mémoire : ne réconcilie que son namespace sans
  `watchNamespace="*"`, exige un ServiceAccount `mongodb-database` dans le
  namespace de la base, et crée un PVC `logs-volume` sans classe.)
- **Capacité** : un opérateur de plus peut rester `Pending` (`Insufficient cpu`).
  Sur un cluster 1 master + 1 worker, retirer le taint
  `node-role.kubernetes.io/control-plane` du master, ou ajouter un worker
  (attention : la capacité compute du lab peut manquer — vérifier
  `os-hypervisors/statistics` avant).
- **Quota Cinder** : chaque PVC est un volume Cinder réel ; le quota `volumes`
  par défaut (10) est vite atteint par la stack (Zot + 5 bases + logs…). Un PVC
  reste alors `Pending` avec `VolumeLimitExceeded` dans les events. Élargir avant :
  `openstack quota set --volumes 30 <projet>`.

### 4. Bases de démonstration (optionnel)
`demo-cnpg` (CNPG), `demo-mariadb`, `demo-redis`, `demo-psmdb` (Percona MongoDB) —
chacune avec PVC sur `block-ssd` (volume Cinder).

## Vérifications de bout en bout

```bash
kubectl -n demo-apps exec demo-cnpg-1 -- psql -U postgres -tAc "SELECT version();"
kubectl -n demo-apps exec demo-mariadb-0 -- mariadb -uappuser -pDemoUser123 -N -e "SELECT VERSION();"
kubectl -n demo-apps exec demo-redis-0 -- redis-cli ping
PW=$(kubectl -n demo-apps get secret demo-es-es-elastic-user -o jsonpath='{.data.elastic}' | base64 -d)
kubectl -n demo-apps exec demo-es-es-default-0 -c elasticsearch -- curl -sk -u "elastic:$PW" https://localhost:9200/_cluster/health
MPW=$(kubectl -n demo-apps get secret internal-demo-psmdb-users -o jsonpath='{.data.MONGODB_DATABASE_ADMIN_PASSWORD}' | base64 -d)
kubectl -n demo-apps exec demo-psmdb-rs0-0 -c mongod -- mongosh --quiet "mongodb://databaseAdmin:$MPW@localhost:27017/admin?authSource=admin" --eval "db.runCommand({ping:1}).ok"
```

## Déploiement d'applications

Outil : `tools/paas-deploy-app.sh` — déploie via le chart `stakater/application` :

```bash
# depuis une image publique (Docker Hub, ghcr, quay…)
./tools/paas-deploy-app.sh --name demo-web --image nginx:alpine --port 80

# depuis un dépôt Git public : nixpacks build → push Zot → helm
./tools/paas-deploy-app.sh --name demo-api --git https://github.com/user/repo \
  --branch main --port 8080 --env PORT=8080
```

### Déclencher le CI/CD depuis GitHub
Le script est le point d'entrée : un workflow du dépôt applicatif (public) peut
l'appeler après un push. Exemple minimal (`.github/workflows/deploy.yml`) :

```yaml
name: deploy
on: { push: { branches: [main] } }
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # À adapter : le runner doit joindre le cluster (VPN/self-hosted) et avoir
      # helm/kubectl/nixpacks/crane + un kubeconfig en secret.
      - run: ./tools/paas-deploy-app.sh --name "${{ github.event.repository.name }}" \
               --git "${{ github.server_url }}/${{ github.repository }}" --port 8080
```

En pratique, sur ce lab le build tourne **sur ctrl1** (accès cluster) : un webhook
ou une tâche Temporal peut appeler `paas-deploy-app.sh` avec l'URL du dépôt, sans
exposer le cluster à Internet.

### Pièges du chart stakater/application
- Défauts `containerSecurityContext.runAsNonRoot=true` +
  `readOnlyRootFilesystem=true` (cassent `nginx:alpine`/images nixpacks → le script
  les désactive).
- Ports en **listes** (`deployment.ports[]`, `service.ports[]`, défaut 8080).
- `deployment.env` en **map** (`PORT: {value: "8080"}`).
- Build GitHub : `nixpacks build` puis `crane push <tar> <vip>:5000/... --insecure`
  (`docker save` + `crane` évite de reconfigurer Docker en registre insecure).
  `railpack` est l'alternative moderne à nixpacks.

## Ce qui n'est pas encore automatisé
- Le bootstrap n'est pas encore déclenché par le backend à la fin de `k8s.create` ;
  il se lance à la main (ou en CI) après le cluster.
- Les images des opérateurs ne sont pas mirrorées dans Zot (elles viennent
  toujours de l'amont public).
