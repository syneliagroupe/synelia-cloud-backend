# Prompt agent — portage Proxmox VE (fork parallèle)

> À donner à un agent de dev. Plan de référence : `plans/PROXMOX-SUPPORT-PLAN.md`
> (verdicts vérifiés contre le schéma d'API PVE 9.1.1 — 433 chemins, 646 opérations).
> **Lis ce plan en entier avant d'écrire une ligne.**

---

## -1. Où travailler — à faire en tout premier

Tu travailles **sur dev01** (`ssh dev01`, utilisateur `atlas`, sudo sans mot de passe),
dans `/var/synelia-cloud`. C'est là que l'API Proxmox est joignable (§-0) ; depuis
ailleurs elle ne l'est pas. `/var` a ~900 Go libres.

```bash
sudo mkdir -p /var/synelia-cloud && sudo chown "$USER" /var/synelia-cloud
cd /var/synelia-cloud
git clone https://github.com/syneliagroupe/synelia-cloud-backend.git
git clone https://github.com/syneliagroupe/synelia-cloud.git            # portail Next.js
git clone https://github.com/syneliagroupe/synelia-cloud-architecture.git plans
cd synelia-cloud-backend && uv sync
```

Tout le travail se fait dans ces clones. **Ne travaille pas dans `/opt/synelia/*`** :
ce sont les copies de travail d'un humain, avec des `.env` pointant l'infra vivante.

> Si `git clone` demande une authentification : les remotes ne portent plus de jeton
> en dur (il était expiré et a été retiré). Utilise ta propre credential GitHub.

## -0. Accès à l'instance Proxmox

Une VM Proxmox VE est déjà déployée **sur dev01** pour ce chantier.

| | |
|---|---|
| Domaine libvirt | `jekas-proxmox.dev01.smile.lan` |
| API | `https://192.168.1.10:8006/api2/json` (certificat auto-signé → `verify=False`) |
| UI | `https://192.168.1.10:8006` |
| Version | PVE **9.1.1**, nœud `pve` |
| Ressources | 8 vCPU, 16 Go, disque 200 Go, virtualisation imbriquée **active** |
| Réseau | `eth1` sur le bridge `smile301` (192.168.1.0/24) ; `vmbr0` = NAT vagrant-libvirt |

**Joignable uniquement depuis dev01** (bridge de lab, pas routé ailleurs).
Vérification : `curl -sk https://192.168.1.10:8006/api2/json/version` → `{"data":{...}}`.

**Identifiants** : un jeton d'API dédié existe déjà, `root@pam!synelia-agent`
(privsep désactivé). Le secret n'est **pas** dans le dépôt — il est sur dev01 :

```bash
sudo cat /etc/synelia/proxmox-agent.env     # root:exploit, 0640
```

Ce fichier porte directement les réglages attendus (`SYNELIA_PVE_*`). Le charger dans
le `.env` local du clone, jamais le committer (I4). Pour en créer un autre :
`sudo pveum user token add root@pam <nom> --privsep 0`.

Accès shell à la VM si besoin :
```bash
ssh dev01
cd /var/lib/vagrant/project/jekas-proxmox && sudo -E vagrant ssh
```

Le schéma d'API complet est sur la VM (`/usr/share/pve-docs/api-viewer/apidoc.js`,
3,9 Mo) — c'est la source de vérité des formes de réponse, à extraire en JSON pour
écrire les doubles de test.

> **dev01 est une machine partagée et sa RAM est saturée** (~0 Go libre, swap 248 Go,
> ~25 VM dont le lab OpenStack de la démo). Vérifie `free -g` avant de créer quoi que
> ce soit, et ne crée pas de VM de test sans nécessité : préfère les doubles de test.

---

## 0. Mission

Faire de Synelia Cloud un portail **multi-fournisseurs** où Proxmox VE est un amont de
premier rang à côté d'OpenStack — pas un mode dégradé, pas un greffon.

**Le mètre-étalon, c'est Multiportal.** On ne cherche pas la parité fonctionnelle avec
OpenStack : on cherche à être nettement meilleur qu'un panneau Proxmox classique sur
trois axes précis, et c'est **cela** qui définit « way better » :

1. **Un seul contrat d'API** (`openapi.json`, 514 opérations) pour les deux amonts.
   Le client ne sait pas ce qui tourne derrière. Multiportal est mono-amont.
2. **Dégradation honnête.** Une capacité absente chez un fournisseur répond
   `non_supporte` avec un code de contrat propre — **jamais** une donnée inventée,
   jamais un `200` menteur.
3. **Multi-tenance réelle** adossée au modèle Espace Cloud existant (orgs, RBAC 38
   actions × 10 rôles, quotas, facturation FCFA), pas des ACL Proxmox à nu.

### La règle d'arbitrage, à appliquer sans hésiter

**La priorité absolue est que la chaîne complète fonctionne sur Proxmox — quitte à
réduire les fonctionnalités.** Ce n'est pas une concession temporaire à contrecœur,
c'est la consigne. Un parcours client étroit mais réellement opérationnel de bout en
bout sur Proxmox vaut mieux qu'une couverture large à moitié branchée.

Donc, face à un arbitrage, dans cet ordre :

1. **Réduire le périmètre** — déclarer la capacité `non_supporte` et avancer.
2. **Mapper honnêtement** sur la notion Proxmox la plus proche, même imparfaite, en le
   documentant.
3. **Émuler** — en dernier recours seulement, et jamais si l'émulation produit une
   donnée que le client croirait réelle (I1).

Quand une notion OpenStack n'a pas de sens chez Proxmox, on ne l'émule pas à grands
frais : on la déclare non supportée et on avance. Lis bien §2.1 du plan — les trois
lacunes (load balancer, IP flottante, volume indépendant) sont **vérifiées**, pas
supposées. Ne perds pas de temps à les contourner.

**Le jalon qui compte** : commander une offre VPS depuis le portail, et obtenir une VM
Proxmox réellement démarrée, jointe en SSH, avec son réseau privé et son pare-feu.
Tout ce qui ne sert pas ce jalon est secondaire et peut être réduit.

---

## 1. Invariants — non négociables

**I1. Aucune donnée inventée servie comme réelle.** C'est la règle qui prime sur
toutes les autres. Aujourd'hui `class ComputeOpenStack(ComputeSimule)` : la classe
réelle **hérite** de la simulée, donc une méthode non écrite retourne silencieusement
le faux (`ip_privee_serveur()` → `10.0.0.{hash}`, `statut_serveur()` → `"ACTIVE"`).
Reproduire ce schéma pour Proxmox livrerait des VM fantômes en `200`. Interdit.

**I2. Le contrat ne bouge pas.** `uv run tools/contrat_diff.py` doit rester vert
(servi ⊇ contrat). Aucun champ ajouté au contrat pour faire plaisir à Proxmox.

**I3. OpenStack ne régresse pas.** `uv run pytest -q` au vert avant/après chaque PR.
L'infra OpenStack du lab est vivante et sert la démo — on ne la casse pas.

**I4. Pas de secret en clair.** Tokens d'API Proxmox via réglages chiffrés, jamais de
mot de passe (`/access/users/{id}/token/{tid}`, avec privilege separation). Ne jamais
committer `.env`, `.secrets/`.

**I5. Timeout explicite sur tout appel amont.** Voir le commentaire de
`fabrique.py:10-16` : un seul appel HTTP bloqué gèle tout le process (mono-worker,
appels amont synchrones). Calquer `_TIMEOUT_API_S = 45`. Vécu en prod, pas théorique.

---

## 2. Hors scope — on réduit délibérément

À ne **pas** développer dans ce chantier. Si tu penses qu'un de ces points est
indispensable, remonte-le, ne le code pas.

| Exclu | Raison |
|---|---|
| **SSO / OIDC** | Explicitement reporté. `securite.sso` reste `persiste`. Ne pas toucher à `/access/openid` ni au module `securite`, même si Proxmox le permet. |
| **Load balancer sur Proxmox** | Aucun équivalent dans l'API (0 résultat sur `balanc\|haproxy\|vip\|listener`). → `non_supporte`. Pas de HAProxy maison dans ce lot. |
| **Magnum / K8s managé sur Proxmox** | Aucun équivalent. → `non_supporte`. |
| **IP flottante sur Proxmox** | Pas de primitive (le firewall PVE est filtrant seul : `in,out,forward,group`, pas de `snat`/`dnat`). Décision produit requise avant tout code — voir §5. |
| **Migration de l'existant OpenStack → Proxmox** | Hors sujet. Les deux coexistent. |
| **LXC** | Se limiter à QEMU/KVM d'abord. LXC est un lot ultérieur. |

---

## 3. Phase 0 — prérequis bloquant, séquentiel, un seul agent

**Rien ne se forke avant que ceci soit mergé.** Toutes les pistes du §4 en dépendent.

1. `packages/kernel/synelia_kernel/config.py:51` →
   `fournisseur: Literal["simule", "openstack", "proxmox"]` ; ajouter `pve_url`,
   `pve_token_id`, `pve_token_secret`, `pve_noeud`, `pve_verify_tls`.
2. **Casser l'héritage `Reel(Simule)`** (I1). Pour chacun des 17 domaines, extraire un
   `Protocol` (`ComputeAmont`, `NetworkAmont`, …). `XxxSimule` et `XxxOpenStack`
   l'implémentent **sans lien d'héritage**. Toute méthode non implémentée lève
   `erreurs.amont_indisponible`. C'est le gros du travail de la phase.
3. `fabrique.fournisseur()` : de 2 arguments (`simule, reel`) à une résolution par
   registre sur `reglages().fournisseur`.
4. Neutraliser les 5 fuites d'identité fournisseur dans le métier —
   `apps/synelia/synelia/modules/espaces/service.py` lignes **28, 139, 413, 535, 671**
   (`reglages().fournisseur != "openstack"`). Elles signifient « si un amont réel est
   branché », pas « si OpenStack ». Les remplacer par un test de capacité ou par un
   crochet porté par l'amont (`amont.epurer_vm_orphelines()`, no-op par défaut).
5. `registre_capacites.py` : `classe_reel: str | None` →
   `classes_reel: dict[str, str]`, et `etat` devient un état **par fournisseur**.
   Nouvel état `non_supporte`, distinct de `simule`. `synelia capacites` affiche la matrice.

**Critère de sortie :** `pytest -q` et `contrat_diff.py` identiques à avant. Zéro
changement de comportement observable. C'est un refactor pur — s'il change quelque
chose côté API, il est raté.

---

## 4. Le fork — 5 pistes parallèles après Phase 0

Chaque piste = une branche, un agent, un périmètre de fichiers **disjoint** pour que
les merges ne se marchent pas dessus. Le paquet `packages/proxmox/synelia_proxmox/`
est créé en Phase 0 (squelette vide + connexion) ; ensuite chaque piste n'écrit que
*son* module.

### Piste A — compute + images cloud · `proxmox/compute.py`, `images.py`
Le livrable démontrable. À faire en premier si les agents ne sont pas tous dispo.
- `ComputeProxmox` : `creer_serveur` (clone de template + cloud-init), cycle de vie,
  `redimensionner`, `migrer`, `instantane`, `restaurer`, `statut_serveur`,
  `ip_privee_serveur` (agent QEMU), `console` (ticket noVNC), `capacite_plateforme`.
- Endpoints : `/nodes/{n}/qemu`, `/clone`, `/migrate`, `/snapshot`, `/template`,
  `/agent`, `/status/current`.
- **Images cloud** (point fort, cf. plan §2) : `/qemu/{id}/cloudinit` GET/PUT,
  `/cloudinit/dump`, et `/storage/{s}/download-url` pour tirer une image depuis une URL.
- `gabarits()` : Proxmox n'a pas de flavors → table locale adossée au catalogue.
- **Fini quand** : créer/démarrer/arrêter/snapshoter/supprimer une VM Proxmox depuis
  l'API Synelia, cloud-init appliqué, IP remontée, console ouvrable.

### Piste B — stockage + sauvegarde · `proxmox/block_storage.py`, `backup.py`
- `BackupProxmox` sur `/cluster/backup` + vzdump + PBS + `/storage/{s}/file-restore`.
  Fait passer `backup.plan` de `simule` à `reel` — **gain net sur OpenStack**.
- `BlockStorageProxmox` : attention, `POST /nodes/{n}/storage/{s}/content` exige
  `vmid` (`optional=0`, « Specify owner VM »). **Un volume ne peut pas exister sans VM
  propriétaire.** Pas de cycle de vie Cinder. `POST .../content/{volume}` (copie) est
  marqué « experimental code - do not use » → ne pas l'utiliser.
- **Fini quand** : sauvegarde planifiée + restauration de fichier opérationnelles ; le
  volume détaché est soit mappé honnêtement, soit `non_supporte` — jamais simulé.

### Piste C — réseau + pare-feu · `proxmox/network.py`
Le plus de valeur par rapport à Multiportal.
- **Security groups** : correspondance quasi 1:1 — `/cluster/firewall/groups` (= SG),
  `/ipset` (= remote-group), `/aliases`, `/macros`. 45 chemins disponibles.
  `assurer_regle_ssh` / `assurer_regle_port` → règles firewall niveau VM.
- **Réseaux** : `/cluster/sdn/zones` (`evpn,vxlan,vlan,qinq,simple,faucet`), `/vnets`,
  `/vnets/{v}/subnets`, `/vnets/{v}/ips`, **`/ipams`**, **`/dns`**.
- **Egress** : zone EVPN `exitnodes` / `exitnodes-primary`.
- LB et IP flottante : **`non_supporte`**, pas de contournement (§2).
- **Fini quand** : réseau privé + SG + règles SSH/port fonctionnels sur une VM Proxmox.

### Piste D — tenance · `proxmox/identite.py`
**Ne pas démarrer avant l'arbitrage §5.** Le modèle conditionne tout le code.
- `/pools`, `/access/{users,groups,roles,acl}`, tokens par utilisateur avec privsep.
- Mapper Espace Cloud → pool + token, ACL par pool. Quotas : Proxmox est grossier,
  les quotas durs restent côté Synelia.
- **Pas de `/access/openid`** (hors scope, §2).

### Piste E — catalogue et zones · `apps/synelia/.../catalogue`, `espaces`
- Rattacher le fournisseur à la **zone** (`zone_vps`, `zone_lb`… — 90 occurrences de
  `zone_vps`), pas au process. Une offre pointe une zone, la zone porte son fournisseur.
- C'est ce qui permet OpenStack **et** Proxmox en même temps plutôt qu'un basculement
  global — donc c'est la piste qui rend le multi-fournisseur visible au client.
- Exposer la matrice de capacités par zone dans l'API (`x-etat`).

**Ordre si tout ne peut pas être parallèle :** A → C → E → B → D.

---

## 5. Arbitrages produit requis — à remonter, pas à trancher seul

Deux décisions ne sont pas techniques et peuvent invalider du code déjà écrit :

1. **Isolation multi-tenant (bloque la piste D).** Keystone donne une frontière
   d'autorisation dure et une credential scopée par projet. Proxmox n'a pas
   d'équivalent : un pool n'est ni une frontière réseau ni une frontière de quota.
   Options : (a) un cluster par client — isolation vraie, coût matériel ; (b) un pool
   + un token par Espace, ACL par pool — isolation applicative, **plus faible que ce
   qui est vendu aujourd'hui** ; (c) Proxmox réservé aux zones internes (lab, préprod).
2. **IP flottante.** Sans primitive PVE, l'exposition publique d'un hébergement passe
   par du NAT sur l'exit-node, hors API Proxmox (script/Ansible à maintenir). Ou bien
   `non_supporte` et les offres Proxmox ne proposent pas d'IP publique dédiée.

---

## 6. Protocole de travail

- Une branche par piste : `claude/proxmox-<piste>`. PR vers `master`.
- **Chaque PR** : `uv run pytest -q` vert, `uv run tools/contrat_diff.py` vert,
  `uv run ruff check` propre. Une PR qui touche un fichier d'une autre piste est à
  re-découper.
- Tests : chaque méthode réelle a un test contre un double du schéma PVE
  (`/tmp/pveapi.json` est la source de vérité des formes de réponse). **Ne pas tester
  contre `jekas-proxmox` en CI** — c'est un lab partagé.
- Après modification de code : `graphify update .` (cf. `AGENTS.md`).
- Cible de dev : `jekas-proxmox.dev01.smile.lan` — `192.168.1.10:8006`, PVE 9.1.1,
  virtualisation imbriquée active. **dev01 est une machine partagée et sa RAM est
  saturée** (~0 Go libre, swap 248 Go) : ne pas y créer de VM sans vérifier `free -g`.

## 7. Première action attendue

1. Cloner les dépôts dans `/var/synelia-cloud` sur dev01 (§-1) et faire `uv sync`.
2. Vérifier l'accès Proxmox (§-0) :
   `curl -sk https://192.168.1.10:8006/api2/json/version` doit rendre `9.1.1`.
3. Lire `plans/PROXMOX-SUPPORT-PLAN.md` en entier.
4. Produire **la Phase 0 seule**, en une PR, sans toucher à Proxmox. Annoncer
   explicitement le résultat de `pytest -q` et de `contrat_diff.py` avant/après.

Le fork §4 ne démarre qu'après le merge de la Phase 0.
