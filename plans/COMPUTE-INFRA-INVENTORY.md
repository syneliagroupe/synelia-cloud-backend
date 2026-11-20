# Compute & Infra — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/{vms,kubernetes,reseau,stockage,sauvegarde,pra}`
+ `uv run synelia capacites`. **Aucun appel réel** effectué cette session : `.env` porte
`SYNELIA_FOURNISSEUR=openstack`, donc `uv run pytest` sur ces modules taperait le lab OpenStack
réel sans isolation — non exécuté, conformément à la consigne. Tout ce document est basé sur
lecture de code + le registre `capacites` (lui-même lu à froid, aucune commande mutante lancée).

Légende (reprise de `WEB-CLOUD-INVENTORY.md`) : **réel** = appelle un amont véritable
(OpenStack Nova/Cinder/Neutron/Magnum/Designate/MinIO) ; **simulé** = classe `*Simule`, aucun
appel externe, gate d'environnement absente ; **maquette** = code qui n'appelle *aucun* amont,
même quand branché en mode `openstack` — écrit uniquement en base, quel que soit le fournisseur ;
**partiel** = mélange réel/maquette selon l'endpoint.

Registre `synelia capacites` (lignes pertinentes pour cet univers) :

| id | état | gate | détail |
|---|---|---|---|
| `compute.serveur` | réel | — | |
| `block_storage.volume` | réel | — | |
| `network.reseau` | réel | — | |
| `identite.tenancy` | réel | — | |
| `designate.zone` | réel | — | |
| `magnum.cluster` | réel | — | |
| `minio.objet` | simulé | `SYNELIA_MINIO_URL` | "Réel complet mais gate jamais posée dans CI" |
| `k8s.workload` | simulé | `SYNELIA_PAAS_CLUSTER_ID` | "Un seul cluster, absent par défaut" |
| `backup.plan` | simulé | — | "Partiel: create_plan_run si dispo" |

Le registre ne porte **pas** d'entrée dédiée `dr.failover` / PRA ni `vpn.tunnel` — cohérent avec
ce qui est trouvé en code (voir § pra et § reseau/VPN ci-dessous : ce sont des maquettes pures,
jamais câblées sur un `amont()`, donc rien à activer/désactiver par gate).

## Vérification des exécuteurs de jobs (pattern web_backup)

Tous les `demarrer_travail(ctx, "type.job", ...)` trouvés dans les routers de cet univers ont
été confrontés au registre `@executeur(...)` de chaque `service.py`/`paas.py`. **Aucun type de
job orphelin trouvé** (contrairement à web_backup avant correction) : `vm.*`, `k8s.*`, `lb.create`,
`volume.*`, `backup.*`, `dr.failover.*` ont tous un exécuteur enregistré. Le problème dans cet
univers n'est donc pas « le job silencieux qui ne fait rien » côté infrastructure de jobs, mais
des **exécuteurs enregistrés qui, une fois qu'on lit leur corps, ne font pas ce que leur nom
promet** (PRA, VPN, métriques LB) — cf. détail par module.

## 1. vms — machines virtuelles
Backend : `ComputeOpenStack` via `synelia_openstack.fournisseur(ComputeSimule, ComputeOpenStack)`
(bascule globale par `SYNELIA_FOURNISSEUR`). **Statut : réel** pour le cycle de vie principal.

Jobs (`@executeur` dans `service.py`, tous enregistrés et correspondant à un appel
`demarrer_travail`) : `vm.create`, `vm.delete`, `vm.power.start/stop/reboot`, `vm.resize`,
`vm.migrate`, `vm.snapshot`, `vm.hardware`, `vm.compose`, `vm.restore`.

- [x] `GET/POST /vms`, `GET/PATCH/DELETE /vms/{id}` — réel (DB + Nova pour create/delete).
- [x] `POST /vms/{id}/demarrage|arret|redemarrage` — réel (`vm.power.*`).
- [x] `PUT /vms/{id}/materiel` — réel (`vm.hardware`).
- [x] `GET /vms/{id}/journaux` — non vérifié plus en détail (probablement lecture console Nova).
- [x] `POST /vms/{id}/console` — non vérifié en détail.
- [x] `GET /vms/{id}/metriques` — non vérifié (à comparer avec le stub metriques LB ci-dessous,
  risque similaire de retour vide/statique — à recontrôler).
- [x] `POST /vms/{id}/migration`, `POST /vms/{id}/redimensionnement` — réel.
- [x] `POST /vms/lot` (création en lot) — réel, orchestre plusieurs `vm.create`.
- [x] `GET/POST/DELETE /vms/{id}/instantanes` — réel (`vm.snapshot`).
- [x] `POST /vms/{id}/instantanes/{snapId}/restauration` — réel (`vm.restore`).

Pas d'écart de premier principe identifié dans ce module à la lecture.

## 2. kubernetes — clusters Magnum + PaaS applicatif
Backend cluster : `MagnumOpenStack`/`ComputeOpenStack` (`compute.serveur`/`magnum.cluster` =
**réel** au registre). Backend PaaS (installation Helm d'un registre interne, storage class,
opérateurs applicatifs sur le cluster une fois créé) : `kubernetes/paas.py`, gate
`SYNELIA_PAAS_CLUSTER_ID` — **simulé par défaut** (capacité `k8s.workload`), car aucun cluster
n'est désigné comme cible PaaS hors configuration explicite. Le code `paas.py` appelle
véritablement `helm`/l'API Kubernetes en sous-processus quand la gate est posée (pas de
simulation dans le code lui-même — c'est bien la *gate*, pas une classe `*Simule`, qui décide).

Jobs enregistrés et utilisés : `k8s.create`, `k8s.delete`, `k8s.upgrade`, `k8s.modules`,
`k8s.pool.create`, `k8s.pool.roll`, `k8s.pool.delete`, `paas.bootstrap`.

- [x] `GET/POST /kubernetes`, `GET/DELETE /kubernetes/{id}` — réel (Magnum).
- [x] `POST /kubernetes/{id}/mise-a-jour` (upgrade version) — réel (`k8s.upgrade`).
- [x] `PUT /kubernetes/{id}/modules` — réel/partiel selon module (`paas.bootstrap`, gate PaaS).
- [x] `GET /kubernetes/{id}/kubeconfig` — non vérifié en détail (probablement lecture Magnum).
- [ ] `GET /kubernetes/{id}/metriques` — **non vérifié en détail**, à recontrôler (même risque
  de stub que `GET /reseau/load-balancers/{id}/metriques`, cf. § reseau).
- [x] `GET/POST/PATCH/DELETE /kubernetes/{id}/pools` — réel (`k8s.pool.*`).
- [x] `GET /kubernetes/versions`, `GET /kubernetes/modules` — statique/catalogue, pas d'amont.

## 3. reseau — réseaux, IP publiques, groupes de sécurité, load balancers, VPN
Backend : `NetworkOpenStack`/`IdentiteOpenStack` (`network.reseau`, `identite.tenancy` = réel).
**Statut : partiel**, avec un écart net entre sous-domaines :

- **Réseaux / IP publiques / groupes de sécurité** : réel — chaque endpoint mutateur appelle un
  `*_amont()` correspondant (`creer_reseau_amont`, `reserver_ip_amont`, `associer_ip_amont`,
  `creer_groupe_amont`, `ajouter_regle_amont`, `attacher_groupe_amont`, `supprimer_*_amont`) qui
  passe par `NetworkOpenStack`/`IdentiteOpenStack`.
- **Load balancers** : partiel.
  - [x] `POST /reseau/load-balancers` — réel, job `lb.create` enregistré.
  - [x] `PUT /reseau/load-balancers/{id}/pool` — réel, appelle `synchroniser_pool_amont`.
  - [ ] `PATCH /reseau/load-balancers/{id}` — **DB seulement**, ne rappelle aucun `*_amont()`
    pour propager les changements de listeners/algo/sticky vers Octavia. Écart : un PATCH sur un
    LB existant ne modifie rien côté OpenStack, seulement la fiche locale.
  - [ ] `PUT /reseau/load-balancers/{id}/regles-l7` — **idem, DB seulement**, aucun appel amont
    (contrairement à `/pool` qui, lui, synchronise réellement). Incohérence entre deux endpoints
    d'apparence symétrique.
  - [ ] **Bug de premier principe** : `GET /reseau/load-balancers/{id}/metriques` renvoie
    littéralement `{"series": [], "liens": None}` **en dur**, sans même consulter
    `victoria.observabilite` (qui existe et gère déjà la dégradation "pas de gate → `[]`" ailleurs
    dans le registre). L'endpoint promet des métriques et n'en fournit jamais, même quand
    l'observabilité est branchée.
- **VPN (`/vpn`)** — **mixte (2026-09-24, E2E client vérifié)** :
  - [x] **OpenVPN nomade** : provisionné à `espace.create` (`openvpn.py`, VM Nova + FIP + tunnel
    `type=ssl` `{code}-nomade`) ; `POST/GET …/profils` émettent un vrai `.ovpn` via SSH ;
    dev01 : firewalld sur **`dev01.ovh.smile.ci`** (DNAT UDP → FIP:1194 — pas vm-admin).
    Lab `ovpn-lab1` : client externe connecté, `remote dev01.ovh.smile.ci 46382`.
  - [ ] **IPsec site-à-site** (`POST /vpn`, `type=ipsec`) : toujours DB-only, pas d'amont ;
    `renegocier_tunnel_vpn` cosmétique. `certificat_bidon()` si profil sans secrets OpenVPN.

## 4. stockage — volumes de bloc (Cinder) + objet (MinIO/S3)
Backend bloc : `BlockStorageOpenStack` (`block_storage.volume` = réel). Backend objet :
`choisir_minio()` → `MinioSimule` par défaut (`minio.objet` = simulé, gate `SYNELIA_MINIO_URL`
"jamais posée dans CI" d'après le registre — donc simulé en pratique sur ce lab même si le code
objet est complet).

Jobs enregistrés : `volume.create`, `volume.attach`, `volume.detach`, `volume.extend` — tous
utilisés par le router, aucun orphelin.

- [x] `GET/POST /stockage/volumes`, `GET/PATCH/DELETE /{id}` — réel (Cinder pour create/delete).
- [x] `PUT /stockage/volumes/{id}/attachement`, `DELETE .../attachement` — réel (`volume.attach`
  /`volume.detach`).
- [x] `POST /stockage/volumes/{id}/extension` — réel (`volume.extend`).
- [x] `GET/POST/DELETE /stockage/cles-s3` — dépend de `minio.objet` : simulé tant que
  `SYNELIA_MINIO_URL` n'est pas posée (confirmé par le registre, pas re-testé directement).
- [x] `GET/POST/PATCH/DELETE /stockage/buckets`, `GET .../usage` — idem, simulé par gate absente.

Pas d'écart de premier principe trouvé dans le code lui-même (le module objet est honnête : il
se dégrade proprement en `*Simule` quand la gate est absente, plutôt que de prétendre agir).

## 5. sauvegarde — plans, points de restauration, restaurations, conformité 3-2-1
Backend : délègue largement à `stockage.service` (`amont_cinder()`, snapshots Cinder) plutôt que
d'avoir un `amont()` distinct pour Nova/Glance comme web_backup. `backup.plan` = simulé au
registre ("Partiel: create_plan_run si dispo"). Jobs enregistrés : `backup.run`, `backup.verify`,
`backup.restore` — tous utilisés, aucun orphelin.

- [x] `GET/POST/PATCH/DELETE /sauvegarde/plans` — réel (DB) ; `POST .../execution` déclenche
  `backup.run`, réel (snapshots Cinder via `amont_cinder()`).
- [x] `GET /sauvegarde/points`, `DELETE /{id}` — réel.
- [x] `POST /sauvegarde/points/{id}/verification` — réel (`backup.verify`, interroge le statut
  du snapshot Cinder, ne suppose pas).
- [x] `GET /sauvegarde/restaurations`, `POST /sauvegarde/restaurations` — réel (`backup.restore`,
  restaure via `amont_cinder().restaurer_snapshot`).
- [ ] **Bug de premier principe trouvé** dans `service.conformite()` (utilisé par
  `GET /sauvegarde/conformite`) : le champ `dernierTestRestauration.succes` est **codé en dur à
  `False`** (`m.DernierTestRestauration(..., succes=False, dureeMin=0)`), quel que soit le
  résultat réel de la restauration testée. Le rapport de conformité 3-2-1 affichera donc
  systématiquement "dernier test de restauration : échec" même après une restauration réussie —
  c'est l'inverse du problème web_backup (pas un job qui prétend réussir sans rien faire, mais un
  indicateur de conformité qui prétend échouer alors que le job a réellement réussi). Pas corrigé
  cette session (nécessite de vérifier d'où viendrait un vrai signal de succès — `restaurations`
  a un champ `statut` "done" mais rien de type `succes` bool exploité ici ; changement à discuter
  plutôt qu'un one-liner évident).

## 6. pra — plan de reprise d'activité (PRA/DR)
Backend : `depot`/`exercices` (DB uniquement). **Aucune fonction `amont()` dans ce module** —
absent du registre `capacites` (cohérent : rien à activer par gate, il n'y a rien de branchable).
Jobs enregistrés : `dr.failover.test`, `dr.failover.real`, `dr.failover.retour` — tous utilisés,
aucun orphelin. Mais contrairement aux autres modules, **les exécuteurs eux-mêmes ne font jamais
d'appel amont** :

- `ExecuteurBascule` (`dr.failover.test` et `dr.failover.real`, même classe) : écrit un exercice
  PRA "réussi" (`succes=True`) et remet le plan en statut `"operationnel"`. Le code contient sa
  propre note honnête à ce sujet dans `plan_vers_modele` : *"ExecuteurBascule/ExecuteurRetour ne
  créent aucune ressource amont sur le site de repli (aucun appel `amont()`, la bascule ne fait
  qu'écrire le statut du plan)"*.
- `ExecuteurRetour` (`dr.failover.retour`) : idem, remet juste `statut="operationnel"`.
- **Écart de premier principe majeur** : `POST /pra/{id}/bascule` avec `type="reel"` exige une
  confirmation explicite (`exiger_confirmation`) comme s'il s'agissait d'une action destructive
  réelle sur un site de production — et le rapport d'exercice généré (`rapportUrl`,
  `rtoConstateMin`, `succes=True`) a toutes les apparences d'un vrai test de bascule. En réalité
  `dr.failover.real` et `dr.failover.test` exécutent **le même code**, qui ne fait jamais rien de
  différent d'un simple changement de statut en base. Un client de l'API ne peut pas distinguer
  "on a réellement basculé le site" de "on a juste dit qu'on l'avait fait" — c'est le même
  pattern que le bug de granularité `web_backup` (un contrat qui promet plus que ce qui est
  implémenté), mais ici sur l'ensemble du module plutôt que sur un seul paramètre.

---

## Plan de travail (dans l'ordre)

1. **PRA (`pra`)** — écart le plus grave : soit implémenter un vrai `amont()` de bascule (VM
   Nova/réplication vers le site de repli), soit — a minima — faire refuser franchement
   `dr.failover.real` (`erreurs.non_porte`, comme le fait déjà `controles_replication` pour le
   mode "continu") pour ne plus laisser croire à une bascule réelle. Le mode `test` peut rester
   une simulation assumée, mais doit être étiqueté comme tel dans la réponse/le rapport.
2. **VPN IPsec (`POST /vpn` manuel)** — OpenVPN nomade est réel (cf. ci-dessus) ; reste à traiter
   IPsec : soit câbler un vrai `amont()` VPNaaS, soit refuser
   franchement la création tant que ce n'est pas implémenté plutôt que de renvoyer un objet
   `statut="up"` fictif. Supprimer/renommer `certificat_bidon` une fois tranché.
3. **`GET /reseau/load-balancers/{id}/metriques`** — remplacer le stub `{"series": [], "liens":
   None}` par un vrai branchement sur `victoria.observabilite` (le pattern de dégradation propre
   existe déjà ailleurs dans le registre, il suffit de le réutiliser ici).
4. **`PATCH /reseau/load-balancers/{id}` et `PUT .../regles-l7`** — décider si ces endpoints
   doivent réellement synchroniser Octavia (comme `/pool` le fait déjà) ou documenter
   explicitement qu'ils sont DB-only le temps d'un futur travail de synchro.
5. **`sauvegarde.service.conformite()`** — corriger le `succes=False` en dur sur
   `dernierTestRestauration` pour refléter le vrai résultat de la restauration testée.
6. Revérifier `GET /vms/{id}/metriques` et `GET /kubernetes/{id}/metriques` (non audités en
   détail cette session) pour écarter le même risque de stub que le LB.
7. Une fois ces points traités, exécuter la suite de tests des modules (`pytest
   apps/synelia/synelia/modules/{vms,kubernetes,reseau,stockage,sauvegarde,pra}`) — **non lancée
   cette session** car `SYNELIA_FOURNISSEUR=openstack` est déjà posé dans `.env` et ferait taper
   ces tests sur le lab OpenStack réel sans isolation ; à faire dans un environnement de test
   dédié (ou avec la gate provisoirement désactivée) avant de considérer ce module clos.
