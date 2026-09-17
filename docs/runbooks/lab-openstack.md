# Runbook — Lab OpenStack (ctrl1 / comp1 / comp2 / stor1)

| hôte  | IP             | rôle       |
|-------|----------------|------------|
| ctrl1 | 192.168.26.235 | contrôleur (+ CAPI/CAPO, k3s de management Magnum) |
| comp1 | 192.168.26.236 | calcul     |
| comp2 | 192.168.26.238 | calcul     |
| stor1 | 192.168.26.237 | stockage   |

Depuis dev01 : `env -u SSH_AUTH_SOCK ssh -o PubkeyAuthentication=no root@192.168.26.235` (mot de passe partagé,
hors dépôt). L'agent SSH de dev01 est mort : toujours `env -u SSH_AUTH_SOCK`. Symptôme si oublié : un échec
SSH absurde (`Connection to UNKNOWN port 65535 timed out`) au lieu d'un vrai refus/timeout — c'est l'agent
mort qui casse la négociation, pas un problème réseau réel.

Le physical host redémarre les VM libvirt du lab (arrêt nocturne ~21h UTC) : après chaque redémarrage,
vérifier Octavia (`o-hm0`/`octavia-interface`, bug de course au boot connu, cf. plus bas) et l'état des
VM hébergement Web Cloud / cluster Kubernetes (cf. §CAPI).

## Accéder à une VM du lab sans IP flottante

Les VM sans IP flottante (masters Magnum, nœuds sans routage externe) ne sont joignables que depuis le
même réseau L2 — pas depuis dev01. Trouver le namespace `qdhcp-<network_id>` du réseau Neutron concerné
(`ip netns list` sur ctrl1) puis `ip netns exec qdhcp-<id> ssh ...` (ou `ping`, `curl`) depuis ctrl1.

## Bug récurrent : `containerd` version du config.toml après mise à jour non surveillée

Une mise à jour de paquet régénère `/etc/containerd/config.toml` en `version = 4`, alors que le binaire
installé sur les VM du lab ne sait lire que jusqu'à la version 3 → `containerd.service`/`docker.service`
boot-loop indéfiniment au moindre redémarrage de VM. Symptôme : Docker inutilisable sur une VM
hébergement/Drive après un reboot, alors qu'elle l'était avant.
- Fix réactif : `sed -i 's/^version = 4/version = 3/' /etc/containerd/config.toml && systemctl reset-failed containerd docker && systemctl restart containerd docker`.
- Fix durable : un drop-in systemd (`DROP_IN_CONTAINERD` dans `web_hebergement/service.py`, réutilisé par
  `web_drive` et `projets`) baké dans le cloud-init de toute nouvelle VM, avec un `ExecStartPre` qui ré-applique
  le sed avant chaque démarrage de containerd — pas la peine de refixer à la main sur les VM créées après ce fix.

## Cluster Kubernetes (Magnum/CAPI) — où regarder quand `openstack coe cluster show` reste bloqué

Le statut Magnum (`CREATE_IN_PROGRESS`, etc.) reflète l'état du `Cluster` Cluster-API, pas forcément le vrai
état des VM. Les contrôleurs CAPI/CAPO tournent **sur ctrl1**, sous containerd/`crictl` (pas `docker ps`),
dans un cluster de management **k3s** local :
```
ssh ctrl1
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get cluster,machines,kubeadmcontrolplane,openstackmachine -A
kubectl describe machine <nom> -n magnum-system   # conditions Ready/NodeHealthy/HealthCheckSucceeded
kubectl logs -n capi-kubeadm-control-plane-system deploy/capi-kubeadm-control-plane-controller-manager --tail=50
```
Si une `Machine` reste `Ready: False` avec `OpenstackMachine Ready: true` (le port Neutron et la VM Nova
existent), le nœud est monté côté infra mais son kubelet/API server ne répond pas — souvent un simple hang
post-reboot de la VM. Vérifier le boot réel sans réseau via la console série Nova
(`compute.get_server_console_output(server_id, length=60)`) avant de creuser plus loin ; un `reboot_server(id, 'HARD')`
suffit fréquemment à débloquer un nœud qui bootait proprement mais ne répondait plus (ping/SSH silencieux
alors que le port Neutron est `ACTIVE` — la VM elle-même était juste figée). CAPI reconcilie toutes les
~10 min : laisser le temps après un hard reboot avant de conclure à un problème plus profond.

### Cause racine du statut bloqué (2026-09-07) : le catalog Keystone pointait Neutron en interne

Le CCM (`openstack-cloud-controller-manager` v1.33.1, DaemonSet `kube-system` du cluster workload, config dans
le Secret `cloud-config`) s'authente bien sur le Keystone **public** (`auth-url=https://keystone.openstack-lab…`,
joignable du réseau tenant), puis résout les URLs des autres services **depuis le catalog du token**. Or dans ce
lab, l'entrée **public** de `network` (Neutron) pointait comme l'interne sur `http://192.168.26.234:9696` —
seul service dont l'URL publique n'avait pas été basculée sur les vhost HTTPS `*.openstack-lab.dev01.ovh.smile.ci`.
D'où : `dial tcp 192.168.26.234:9696: i/o timeout` dans les logs CCM → jamais de `providerID` sur les nœuds →
Machines CAPI jamais Ready → Magnum bloqué en `CREATE_IN_PROGRESS` — alors même que le cluster sert des
workloads réels sans problème (les nœuds sont Ready côté kubelet, l'API répond).

Fix (ré-appliqué et **vérifié** le 2026-09-07 13:11 UTC — la pose du matin n'avait pas pris : l'URL était
encore interne à 13:09) : `openstack endpoint set <id-endpoint-network-public> --url
https://neutron.openstack-lab.dev01.ovh.smile.ci`, puis redémarrer le CCM pour qu'il reprenne le catalog
(`kubectl --kubeconfig /tmp/wl.kubeconfig -n kube-system rollout restart ds/openstack-cloud-controller-manager`).
Preuve de reprise : la boucle `Update N nodes status` du CCM passe de ~1m00s (timeout) à ~0,4s. Ancienne URL de
repli : `http://192.168.26.234:9696`. Ne concerne que l'interface **public** (les services kolla internes
utilisent l'interface internal, inchangée). Leçon générale : après publication des vhost HTTPS publiques,
vérifier que **toutes** les entrées `public` du catalog ont suivi — `openstack endpoint list --interface public`
et chercher les `http://192.168.26.x` restants (re-vérifier aussi après chaque redémarrage du lab).

### Piège suivant (2026-09-07) : providerID absent des Nodes alors que le CCM appelle bien Neutron

Le redémarrage du CCM ne suffit pas si les nœuds sont **déjà enregistrés sans taint**
`node.cloudprovider.kubernetes.io/uninitialized` : dans le node controller du CCM, seul `syncNode` (chemin des
nœuds taintés, i.e. kubelet lancé avec `--cloud-provider=external`) pose `spec.providerID` ; la boucle
périodique des nœuds non taintés (`Update N nodes status`) ne met à jour que les adresses. Ici les
`KubeadmConfig` magnum n'ont pas de `kubeletExtraArgs` cloud-provider → pas de taint → le CCM ne peuplera
jamais le providerID de ces nœuds. Fix : patcher les Nodes à la main avec la valeur exacte des
`OpenStackMachine.spec.providerID` (mapping vérifié par port Neutron `--device-id` ↔ IP du nœud) :
`kubectl patch node <nom> --type=merge -p '{"spec":{"providerID":"openstack:///<uuid-vm>"}}'`
(réversible : patch à `null`). CAPI mappe alors Machine↔Node (`nodeRef`) en quelques secondes et les Machines
passent Ready. Le kubeconfig de `coe cluster config` sort avec `server: None` tant que Magnum ignore
l'api_address : lire le vrai endpoint dans `openstackcluster.status.controlPlaneEndpoint` (cluster de
management k3s de ctrl1) et patcher le champ `server`.

### Piège encore (2026-09-07) : LB Octavia kubeapi en provisioning ERROR mais ONLINE

Machines CAPI Ready ne suffit pas : le `OpenStackCluster` de CAPO attend `provisioning_status=ACTIVE` du LB
API (`k8s-clusterapi-cluster-<ns>-<cluster>-kubeapi`), sinon condition `APIEndpointReady=False` → Cluster
jamais `Available` → Magnum reste `CREATE_IN_PROGRESS` — même si l'API répond réellement derrière. Cause ici :
l'arrêt nocturne du lab a fait perdre les heartbeats, le health manager a déclenché un failover automatique de
l'amphora (03:02 UTC), le build de l'amphora de remplacement a timeout (`ComputeWaitTimeoutException`, 32 min)
→ amphora origine laissée `ERROR` en base alors qu'elle sert toujours le trafic (`operating_status=ONLINE`,
listener ACTIVE). Diag : `openstack loadbalancer show <id>` + `amphora list --loadbalancer <id>`. Fix appliqué :
bascule d'état en base sur ctrl1 (mariadb kolla, backup des lignes + commande de rollback dans
`/root/octavia-lb-8d5455f4-backup-20260907.txt`) : `load_balancer.provisioning_status=ACTIVE`,
`amphora.status=ALLOCATED` (état normal d'une amphora saine, cf. le LB vps-zone). Alternative plus lourde :
`openstack loadbalancer failover <id>` (rebuild complet de l'amphora, ~blip API de 1-3 min, à réserver aux cas
où l'amphora est réellement morte — le précédent failover ayant déjà échoué sur un timeout compute, préférer la
bascule d'état quand `operating_status=ONLINE`). Après chaque reboot du lab, vérifier ce LB.

État final (2026-09-07 ~13:45 UTC) : endpoint public Neutron = vhost HTTPS, providerID posés, Machines CAPI
Ready, LB ACTIVE/ONLINE → `openstack coe cluster show capi-fix-verify` = **CREATE_COMPLETE / HEALTHY**
(health_reason : 2 machines Ready=True, api ok), sans recréation ni reboot du cluster.

## Brancher le backend sur le lab

`SYNELIA_FOURNISSEUR=openstack`, `SYNELIA_OS_AUTH_URL=http://192.168.26.234:5000/v3`,
`SYNELIA_OS_APPLICATION_CREDENTIAL_ID/SECRET` (créer avec `openstack application credential create synelia`),
`uv sync --extra openstack`. Depuis un poste distant : tunnel SSH + `SYNELIA_OS_ENDPOINT_OVERRIDES='{"compute":"http://127.0.0.1:8774/v2.1"}'`.

Depuis dev01 lui-même (pas besoin de tunnel) : `192.168.26.0/24` est directement routé — `ping`/`curl`/
`openstack` (CLI, `~/.config/synelia/admin-openrc.sh`) fonctionnent tels quels vers ctrl1 et les VIP
internes kolla (`192.168.26.234`), sans SSH ni tunnel.

## Console noVNC des VM (`GET /vms/{id}/console`) — vhost public `console.synelia.dev01.ovh.smile.ci`

`ComputeOpenStack.console()` (`packages/openstack/synelia_openstack/compute.py`) appelait Nova
`create_console(..., console_type="novnc")`, qui renvoie l'URL du **novnc-proxy interne** du lab —
confirmé (deux VM différentes, même host:port, seul le `token` change) :
`http://192.168.26.234:6080/vnc_lite.html?path=%3Ftoken%3D<uuid>` (VIP kolla, une seule instance
`nova-novncproxy` sert toutes les VM ; jamais atteignable depuis Internet, y compris depuis un client
mobile réel). Le correctif ne réécrit **que** le schéma/host/port vers un vhost public dev01, jamais le
chemin ni le `token` (jeton de session Nova, casse si altéré).

**Vhost Apache** (fichiers hors dépôt git, sur dev01 uniquement — `/etc/httpd/conf.d/`) :
- `console.synelia.dev01.ovh.smile.ci.conf` (port 80, redirection HTTPS + bypass ACME comme les autres
  vhosts `*.synelia.dev01.ovh.smile.ci`).
- `console.synelia.dev01.ovh.smile.ci-le-ssl.conf` (port 443) : `ProxyPass`/`ProxyPassReverse` classiques
  vers `http://192.168.26.234:6080/`, **plus** le motif websocket déjà utilisé pour n8n/argocd sur ce
  host (`mod_proxy_wstunnel`, déjà chargé — vérifié via `httpd -M`) :
  ```
  RewriteEngine On
  RewriteCond %{HTTP:Upgrade} =websocket [NC]
  RewriteRule /(.*) ws://192.168.26.234:6080/$1 [P,L]
  RewriteCond %{HTTP:Upgrade} !=websocket [NC]
  RewriteRule /(.*) http://192.168.26.234:6080/$1 [P,L]
  ```
  Sans ce motif, la page noVNC se charge mais la connexion websocket (le flux vidéo réel) échoue —
  vérifié en direct via un handshake `Upgrade: websocket` brut (réponse `101 Switching Protocols`,
  `server: WebSockify Python/3.12.14`) à travers ce vhost HTTPS, depuis dev01 (donc depuis l'extérieur
  du réseau du lab — le même point de vue qu'un client mobile réel).
- Certificat Let's Encrypt obtenu normalement (`certbot certonly --webroot -w /var/www/html -d
  console.synelia.dev01.ovh.smile.ci --key-type ecdsa`), DNS déjà wildcard sur `*.dev01.ovh.smile.ci`
  (aucun enregistrement DNS à ajouter).
- **Recharger Apache** : `sudo systemctl reload httpd` échoue sur dev01 (bug de namespace systemd déjà
  documenté dans [[dev01-backend-hosting]]) — utiliser `sudo kill -USR1 $(cat /run/httpd/httpd.pid)`.
  Vérifié après coup : `api.`/`app.`/`grafana.synelia.dev01.ovh.smile.ci` toujours fonctionnels.

**Vérification bout en bout réalisée le 2026-09-07** : `ComputeOpenStack().console(<id Nova réel>)` appelé
en direct dans le conteneur redéployé (contre `vm-projet-7a33a865dc46`, VM ACTIVE réelle du lab) renvoie
`https://console.synelia.dev01.ovh.smile.ci/vnc_lite.html?path=%3Ftoken%3D<token réel>` ; cette URL exacte
a été rechargée en HTTP (200, page noVNC) et en websocket (`101 Switching Protocols`) depuis dev01. Note :
la création d'une VM neuve via l'API réelle (`POST /v1/espaces` puis `POST /v1/vms`) pour ce test a
échoué sur `No valid host was found` — capacité du lab déjà épuisée (`vcpus_used=17/10`,
`disk_available_least=-3` sur `openstack hypervisor stats show`), confirmé indépendamment du code avec un
`openstack server create` brut ; pas un bug introduit par ce changement. L'espace/VM de test et le serveur
Nova orphelin de ce test ont été nettoyés (`DELETE /v1/vms/{id}` puis `/v1/espaces/{id}`, `openstack server
delete`).

## Zone VPS partagée (web_hebergement / projets cible `vm`)

L'Espace Cloud `vps-zone` (réseau privé + load balancer Octavia public partagés, id
`SYNELIA_VPS_ZONE_ESPACE_ID`) n'est plus un bootstrap manuel one-shot : `synelia.amorcage.amorcer()`
appelle `espaces.service.semer_zone_vps` à chaque démarrage, indépendamment de `SYNELIA_SEED_DEMO` —
idempotent, elle ne recrée rien tant que la ligne existe. Elle bascule aussi la ligne sur la
convention « plateforme » (`org_id NULL`, jamais visible depuis `/espaces` côté client — voir
`/admin/espaces` pour la retrouver côté équipe Synelia) si elle porte encore l'`org_id` client du
bootstrap historique. Si la ligne est absente (nouvel environnement), elle est provisionnée pour de
vrai avec le même exécuteur que la création normale d'un Espace (`ExecuteurEspaceCreate`) — à une
exception près : le load balancer Octavia public partagé (`lb_id` dans les secrets de l'Espace)
n'est pas créé par cet exécuteur et doit encore être posé à la main (`openstack loadbalancer create`
+ `depot_plateforme.definir_secrets(ctx, espace_id, {"lb_id": ...})`) avant le premier hébergement —
c'est ce qui a été fait manuellement pour créer `vps-zone` sur ce lab.

## État réel vs simulé de l'univers Infrastructure

Voir [[infra-universe-real-vs-simulated]] (mémoire de session) pour le détail à jour — au 2026-09-07,
VM/Espaces/Buckets/Kubernetes/Load-balancers/Réseau/Volumes/Bases sont tous réellement provisionnés sur ce
lab (Sauvegardes/PRA échouent honnêtement, Karbor n'étant pas déployé). Deux points ouverts non corrigés :
un plantage de l'API partagée sous charge concurrente réelle (suspecté : appel SDK OpenStack synchrone
bloquant la boucle asyncio), et des enregistrements `web_hebergement` orphelins en base dont la VM Nova
réelle a été supprimée sans nettoyage côté DB.
