# Support Proxmox VE en second fournisseur — plan (rédigé le 2026-11-20)

Source : lecture directe de `packages/openstack/`, `packages/kernel/synelia_kernel/config.py`,
`apps/synelia/synelia/modules/{espaces,projets,bases,stockage}/service.py`. **Aucun appel amont exécuté**
(`.env` porte des identifiants d'infra vivante). Audit par lecture de code.

Côté Proxmox, le §2 n'est **pas** écrit de mémoire : il est vérifié contre le schéma d'API réel
extrait de `/usr/share/pve-docs/api-viewer/apidoc.js` sur `jekas-proxmox` (PVE **9.1.1**, build
2025-11-18) — **433 chemins, 646 opérations**. Proxmox ne publie pas d'OpenAPI ; `apidoc.js` est
le dump JSON intégral du schéma, équivalent et faisant autorité pour cette version précise.
Copie locale : `/tmp/pveapi.json`. Chaque verdict ci-dessous cite les chemins constatés.

Objectif : faire cohabiter OpenStack et Proxmox VE comme deux amonts interchangeables, sans régression
sur l'existant et **sans jamais servir de données simulées en se faisant passer pour du réel**.

---

## 1. État actuel — le point de couture

Trois mécanismes portent aujourd'hui le choix d'amont :

| Mécanisme | Fichier | Forme actuelle |
|---|---|---|
| Réglage | `packages/kernel/synelia_kernel/config.py:51` | `fournisseur: Literal["simule", "openstack"] = "simule"` |
| Dispatch | `packages/openstack/synelia_openstack/fabrique.py:26` | `fournisseur[T](simule: type[T], reel: type[T]) -> T` — **binaire** |
| Registre | `packages/openstack/synelia_openstack/registre_capacites.py` | 17 `Capacite(…, classe_simule, classe_reel, env_gate, etat)` |

17 paires `XxxSimule` / `XxxOpenStack|XxxReel` existent (`grep -rnE "^class .*(Simule|OpenStack)"`).

### 1.1 Le piège principal : l'héritage `Reel(Simule)`

```python
class ComputeOpenStack(ComputeSimule):   # compute.py:223
```

Toutes les implémentations réelles **héritent** de la simulée. C'est commode à deux fournisseurs
(un override manquant retombe sur un stub inoffensif), mais c'est le défaut bloquant à trois :

> un `ComputeProxmox(ComputeSimule)` dont une méthode n'est pas encore écrite **renverra
> silencieusement la donnée inventée de `ComputeSimule`** — `ip_privee_serveur()` rend
> `10.0.0.{hash}`, `statut_serveur()` rend `"ACTIVE"`, `gabarits()` rend la liste statique.

L'API répondrait `200` avec une VM fantôme. C'est exactement le « faux succès silencieux » déjà
documenté pour le registre de jobs dans `ADMIN-PLATEFORME-INVENTORY.md`. **À corriger avant
d'écrire la moindre ligne de Proxmox.**

### 1.2 Fuites d'identité fournisseur dans le métier

`reglages().fournisseur != "openstack"` est testé directement dans la couche service :

- `apps/synelia/synelia/modules/espaces/service.py` lignes **28, 139, 413, 535, 671**

Sémantiquement ces gardes veulent dire « seulement si un amont réel est branché », pas
« seulement si OpenStack ». Elles deviendraient *accidentellement* correctes sous Proxmox
(elles sauteraient), mais pour la mauvaise raison — et les lignes 139/535 (régénération d'AC
Keystone, rebranchement réseau) sont du **soin spécifique OpenStack** qui n'a pas de sens ailleurs.

---

## 2. Correspondance OpenStack → Proxmox VE

Proxmox VE 9.1 expose une seule API REST (`/api2/json`, port 8006), pas une constellation de
services. La granularité ne se superpose pas à celle d'OpenStack.

| Module | Amont OpenStack | Équivalent Proxmox (chemins constatés) | Verdict |
|---|---|---|---|
| `compute` | Nova | `/nodes/{n}/qemu`, `/lxc`, `+/clone`, `/migrate`, `/remote_migrate`, `/snapshot`, `/template`, `/agent` | **Bon** |
| **cloud-init / images** | Glance + metadata | `/qemu/{id}/cloudinit` (GET,PUT), `/cloudinit/dump`, `/storage/{s}/download-url`, `/oci-registry-pull` | **Bon** — cloud-init est de première classe ; `download-url` tire une image cloud depuis une URL directement dans le storage |
| `backup` | Karbor/Cinder backup | `/cluster/backup`, `vzdump`, PBS, `/storage/{s}/file-restore` | **Bon**, supérieur à l'existant (aujourd'hui `simule`) |
| **firewall / SG** | Neutron security groups | **45 chemins.** `/cluster/firewall/groups` (= SG), `/ipset` (= remote-group), `/aliases`, `/macros`, + niveaux vnet / VM / nœud | **Bon** — correspondance quasi 1:1, meilleure que prévu |
| **réseau** | Neutron net/subnet/port | **38 chemins.** `/cluster/sdn/zones` (types `evpn,vxlan,vlan,qinq,simple,faucet`), `/vnets`, `/vnets/{v}/subnets`, `/vnets/{v}/ips`, **`/ipams`**, **`/dns`** | **Bon** — modèle zone/vnet/subnet + IPAM + DNS/reverse-DNS intégrés |
| **routeur / SNAT sortant** | Neutron router | zone EVPN : `exitnodes`, `exitnodes-primary`, `exitnodes-local-routing` | **Partiel** — l'egress existe, la sémantique diffère |
| **IP flottante** | Neutron FIP (DNAT 1:1) | — | **Lacune** — voir ci-dessous |
| `block_storage` | Cinder | `/storage`, `/nodes/{n}/storage/{s}/content` | **Partiel** — voir ci-dessous |
| `identite` | Keystone (domaine/projet/quota/AC) | `/pools`, `/access/{users,groups,roles,acl,domains}`, tokens par utilisateur, **`/access/openid`** | **Difficile** — voir §4 |
| `network` (LB) | **Octavia** | — | **Lacune sèche** |
| `designate` | Designate | `/cluster/sdn/dns` (zone SDN) | Agnostique, mais recouvrement partiel |
| `magnum` | Magnum | — | **Lacune sèche** |

Inchangés et agnostiques (10 modules) : `acme`, `ssh`, `minio`, `zimbra`, `victoria`,
`relais_smtp`, `registrar`, `plateforme_k8s`, `k8s_workload`, `capi_kubeconfig`.

### 2.1 Les trois lacunes réelles (vérifiées, pas supposées)

1. **Load balancer — absent.** Recherche de `balanc|haproxy|vip|listener` sur les 433 chemins :
   **aucun résultat**. Impacte ~8 méthodes de `network.py` (`creer_load_balancer`, `creer_pool`,
   `ajouter_membre`, `creer_moniteur_sante`, `ajouter_regle_hote`…). Options : (a) HAProxy piloté
   dans une VM dédiée, (b) capacité `non_supporte` sur zone Proxmox.

2. **IP flottante — pas de primitive.** `/cluster/sdn/vnets/{vnet}/ips` est une association
   **IP↔MAC** (fixed IP / port binding), pas un DNAT 1:1. Et le firewall PVE est **filtrant
   seulement** : `type` ∈ `in,out,forward,group`, aucune action `snat`/`dnat` dans le schéma.
   Donc pas de FIP sans NAT externe sur l'exit-node. *Lacune que mon premier jet avait manquée.*

3. **Volume indépendant — impossible.** `POST /nodes/{n}/storage/{s}/content` (« Allocate disk
   images ») exige **`vmid` (`optional=0`) : « Specify owner VM »**. Un volume ne peut pas exister
   sans VM propriétaire — il n'y a pas de cycle de vie Cinder. De plus `POST .../content/{volume}`
   (copie de volume) est marqué dans le schéma : **« This is experimental code - do not use. »**

### 2.2 Corrections à mon premier jet

- **Firewall et SDN : je les avais sous-évalués.** J'avais écrit « SDN couvre réseaux/SG/NAT »
  en bloc. En réalité les security groups (`/cluster/firewall/groups` + `ipset`) sont une
  correspondance quasi directe de Neutron, et le SDN embarque IPAM et DNS. C'est le point fort
  du portage, pas une zone grise.
- **Cloud-init / images cloud : omis.** C'est natif et solide (`/cloudinit`, `download-url`).
- **IP flottante : j'avais implicitement supposé que le SDN la couvrait.** Non — c'est la lacune
  la plus insidieuse des trois, parce qu'elle ne se voit pas en lisant la liste des endpoints.
- **Identité : trop pessimiste sur un axe.** Il existe des rôles personnalisables
  (`/access/roles`), des ACL propagées, des **tokens d'API par utilisateur avec privilege
  separation**, et **OpenID Connect** (`/access/openid`) — donc la fédération d'identité est
  possible. Le fond du §4 tient quand même : un pool n'est pas une frontière réseau ni de quota.

---

## 3. Phases

### Phase 0 — rendre le dispatch pluggable (refactor pur, zéro changement de comportement)

Mesurable : `uv run pytest -q` identique avant/après, `uv run tools/contrat_diff.py` inchangé.

1. `config.py:51` → `fournisseur: Literal["simule", "openstack", "proxmox"]`, + réglages
   `pve_url`, `pve_token_id`, `pve_token_secret`, `pve_noeud`, `pve_verify_tls`.
2. **Casser l'héritage `Reel(Simule)`** : extraire un `Protocol` par domaine
   (`ComputeAmont`, `NetworkAmont`, …). `ComputeSimule` et `ComputeOpenStack` l'implémentent
   chacun, sans lien d'héritage. Toute méthode non implémentée lève
   `erreurs.amont_indisponible` — jamais de retour inventé.
   *C'est le gros du travail de la phase et la seule vraie dette à payer.*
3. `fabrique.fournisseur()` : de 2 arguments à un registre
   `fournisseur(ComputeAmont)` → résout via `reglages().fournisseur`.
4. Remplacer les 5 `fournisseur != "openstack"` d'`espaces/service.py` par des tests de
   capacité (`est_reel("identite.tenancy")`) ou par des crochets portés par l'amont lui-même
   (`amont.epurer_vm_orphelines()`, no-op par défaut).

### Phase 1 — socle Proxmox + matrice de capacités

5. Nouveau paquet `packages/proxmox/synelia_proxmox/` (parallèle à `openstack/`), dépendance
   `proxmoxer` en extra, connexion par **API token** (jamais mot de passe), timeout explicite
   calqué sur `_TIMEOUT_API_S = 45` — la note de `fabrique.py:10-16` sur le gel du process
   mono-worker s'applique identiquement.
6. Étendre `registre_capacites.py` : `classe_reel` devient `classes_reel: dict[str, str]`
   (`{"openstack": "ComputeOpenStack", "proxmox": "ComputeProxmox"}`), et `etat` devient un état
   **par fournisseur**. `synelia capacites` affiche la matrice. Une capacité absente chez un
   fournisseur est déclarée `non_supporte` — distinct de `simule`.

### Phase 2 — compute + backup (le cœur VPS, livrable utile)

7. `ComputeProxmox` : `gabarits()` (pas de flavors chez Proxmox → table locale ou mapping
   catalogue), `images()` (templates + ISO), `creer_serveur` (clone de template + cloud-init),
   cycle de vie, `instantane`, `console` (ticket noVNC), `statut_serveur`, `ip_privee_serveur`
   (agent QEMU), `capacite_plateforme()` (`/nodes/{n}/status`).
8. `BackupProxmox` : `vzdump` — fait passer `backup.plan` de `simule` à `reel`.

### Phase 3 — réseau

9. `NetworkProxmox` : SG sur `/cluster/firewall/groups` + `ipset` (correspondance directe),
   réseaux sur `/cluster/sdn/{zones,vnets,subnets}` avec `ipam`, `assurer_regle_ssh` /
   `assurer_regle_port` sur les règles firewall niveau VM.
10. **IP flottante** : trancher §2.1 point 2 — NAT sur l'exit-node (hors API PVE, donc script
    ou Ansible à maintenir), ou `non_supporte`.
11. **Load balancer** : trancher §2.1 point 1. Si non supporté, `non_supporte` et erreur de
    contrat propre.

### Phase 4 — tenancy

11. `IdentiteProxmox` — voir §4, la décision de modèle précède le code.

### Phase 5 — exposition catalogue

12. Rattacher le fournisseur à la **zone** (`zone_vps`, `zone_lb`… — 90 occurrences de
    `zone_vps`), pas au process : une offre pointe une zone, la zone porte son fournisseur.
    C'est ce qui permet OpenStack **et** Proxmox simultanément plutôt qu'un basculement global.

---

## 4. Le point dur : la multi-tenance

`IdentiteSimule` (`identite.py:11`) expose `creer_domaine` / `creer_projet` / `poser_quotas` /
`creer_application_credential` / `supprimer_projet`. Keystone donne une isolation *dure* :
un projet est une frontière d'autorisation, et une application credential est un secret
scopé à ce projet — c'est ce que l'Espace Cloud stocke en secrets chiffrés.

Proxmox n'a pas cela. Il a des **pools** (regroupement avec ACL), des **users/realms** et des
**API tokens**. Un pool n'est pas une frontière réseau ni une frontière de quota au sens Nova :
les quotas Proxmox sont par utilisateur, grossiers, et le SDN n'est pas découpé par pool.

Trois modèles possibles, par isolation décroissante :

1. **Un nœud/cluster Proxmox par client** — isolation vraie, coût matériel élevé.
2. **Un pool + un token par Espace Cloud, ACL par pool** — isolation applicative, portée par
   Proxmox mais plus faible que Keystone ; le réseau reste à cloisonner à la main via SDN.
3. **Mono-tenant** — Proxmox sert une zone interne (lab, préprod), pas des clients finaux.

**(2) est le compromis réaliste**, mais il change la garantie d'isolation vendue. À arbitrer
avec le métier, pas en implémentation. **C'est le seul point qui peut invalider le reste du plan**,
donc à trancher avant la phase 4 — et idéalement avant de chiffrer les phases 2-3.

---

## 5. Risques

| Risque | Portée | Parade |
|---|---|---|
| Faux réel par héritage `Simule` | **Élevé** — fausse donnée servie en 200 | Phase 0 point 2, non négociable |
| Isolation plus faible qu'OpenStack | **Élevé** — contractuel | §4, arbitrage métier |
| Pas d'IP flottante native | **Élevé** — casse le modèle d'exposition publique des hébergements | §2.1 pt 2, à trancher tôt |
| Pas de LB, pas de Magnum | Moyen | Capacité `non_supporte` explicite |
| Volume lié à une VM (`vmid` requis) | Moyen — le contrat expose des volumes détachables | §2.1 pt 3, mapper ou dégrader honnêtement |
| Appels PVE synchrones bloquants | Moyen — déjà vécu sur keystoneauth1 | Timeout explicite dès le 1er appel |
| Dérive du contrat `openapi.json` | Faible | `tools/contrat_diff.py` en CI |

---

## 6. Ordre recommandé

Phase 0 est un prérequis strict et se tient seule : elle améliore le code existant même si
Proxmox est abandonné ensuite. Phases 1-2 donnent le premier livrable démontrable (créer et
piloter une VM Proxmox via l'API Synelia). §4 doit être arbitré avant la phase 4.

Le VM `jekas-proxmox.dev01.smile.lan` (192.168.1.10:8006, PVE 9.1.1, virt. imbriquée active)
sert de cible de développement.
