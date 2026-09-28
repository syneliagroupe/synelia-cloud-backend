# Web Cloud — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24/25)

Source : lecture directe de `apps/synelia/synelia/modules/web_*` + `uv run synelia capacites` +
tests réels exécutés dans cette session contre le lab OpenStack (`192.168.26.0/24`) et l'API OVH.
Ne pas supposer un statut : chaque ligne a été vérifiée dans le code ou par un appel réel.

Légende : **réel** = appelle un amont véritable (OpenStack/OVH/Designate/Zimbra/SSH) ;
**simulé** = classe `*Simule`, aucun appel externe, données fabriquées ; **partiel** = mélange
des deux selon l'endpoint ; **non vérifié** = pas encore audité dans cette session.

## 1. web_domaines — domaines & registrar
Backend : `RegistrarOvh` (nouveau, cette session) via `SYNELIA_REGISTRAR_URL` + 3 identifiants OVH.
**Statut global : réel**, vérifié par un achat effectif (`synelia-test-check-9284.xyz`, €1.89, ordre OVH 259106580).

- [x] `GET /web/domaines/disponibilite` — **corrigé cette session** : appelle maintenant `registrar.verifier()` réel. Bug trouvé et corrigé en cours de route : OVH renvoie `orderable: true` aussi pour un domaine déjà pris ailleurs (`action: "transfer"` au lieu de `"create"`) — sans filtrer sur `action`, `google.com` ressortait "disponible". Revérifié en direct après fix : `google.com` → indisponible, notre domaine possédé → indisponible, un nom neuf → disponible avec vrai prix OVH.
- [x] `POST /web/domaines` (commander) — réel, corrigé cette session (l'exécuteur n'appelait jamais `amont().commander()` avant ; maintenant si).
- [x] `GET /web/domaines`, `GET /web/domaines/{id}` — réel (lecture DB + agrégats).
- [x] `PATCH /web/domaines/{id}` — réel (DB).
- [x] `POST /web/domaines/{id}/code-auth` — réel (appelle `amont().code_auth()`).
- [x] `POST /web/domaines/{id}/renouvellement` — réel, corrigé cette session (idem commander, jamais appelé l'amont avant).
- [x] `POST /web/domaines/transferts` — réel, corrigé cette session (idem).

## 2. web_dns — zones & enregistrements DNS
Backend : `DesignateOpenStack` via `SYNELIA_FOURNISSEUR=openstack`. **Statut : réel**, vérifié (zone créée, enregistrement A ajouté, réellement via Designate — NS renvoyés : `ns1.lab.local.`, interne au lab, pas délégué publiquement).

- [x] `GET /web/dns`, `POST /web/dns` (créer zone) — réel, testé.
- [x] `GET /web/dns/modeles` — statique (catalogue de modèles DNS courrier/DMARC/etc.), pas d'amont à tester.
- [x] `GET/DELETE /web/dns/{zoneId}` — réel.
- [x] `PUT /web/dns/{zoneId}/dnssec` — testé et réel (corps `{"actif": bool}`, pas `"active"`).
- [x] `POST /web/dns/{zoneId}/enregistrements`, `PUT .../enregistrements` (bulk) — les deux testés et réels.
- [x] `PATCH/DELETE /web/dns/{zoneId}/enregistrements/{id}` — les deux testés et réels (PATCH attend le corps complet de l'enregistrement, pas un patch partiel).
- [x] `POST /web/dns/{zoneId}/modeles/{modeleId}` — testé et réel (chemin correct sans `/application` — corrigé dans cet inventaire ; corps JSON requis même vide `{}`).
- **Écart** : NS publics ≠ OVH — délégation réelle (pointer les NS OVH du domaine vers ceux de Designate) non faite, nécessite un appel OVH `/domain/zone/{domain}/nameServers` ou équivalent, non implémenté.

## 3. web_hebergement — VPS hosting, bases, sites, fichiers, tâches
Backend : `ComputeOpenStack`/`NetworkOpenStack`/`SshReel`/`IdentiteOpenStack` via `SYNELIA_FOURNISSEUR=openstack`.
**Statut : réel — bug réseau racine trouvé et corrigé, validé indépendamment (ping + TCP 9443)** :
`o-hm0` (interface santé Octavia sur ctrl1) avait une adresse MAC différente de son port Neutron
enregistré → la table anti-spoofing de `br-int` jetait silencieusement tout son trafic, aucun
ARP n'atteignait jamais l'amphore. Voir `scripts/fix-octavia-hm0-mac.sh` (corrige le MAC) et
`scripts/fix-octavia-lb.sh` (nettoie un LB bloqué en PENDING_UPDATE côté DB Octavia). Ce n'était
**pas** un problème de tunnel VXLAN (confirmé sain pour d'autres VNI via tcpdump sur le pont de
l'hyperviseur dev01) ni de firewalld/hyperviseur (bridges en zone "trusted", vérifié).

Un hébergement `en_ligne` réel a été obtenu et confirmé cette session (serveur Nova vérifié
indépendamment via openstacksdk — pas juste "le job dit done").

- [x] `POST /web/hebergements` (créer) — réel, VM créée à plusieurs reprises avec succès ; le routage LB (étape 3) fonctionne maintenant que `o-hm0` est corrigé (plus jamais rebloqué sur "immutable" après le fix MAC). Corrigé cette session : capacité disque comp1 épuisée (+200 Go ajoutés côté hyperviseur dev01).
- [x] `GET /web/hebergements`, `GET/{id}` — réel + reconcile-on-read testé (a correctement détecté et nettoyé plusieurs lignes orphelines pendant cette session, y compris un cas de "faux succès" — voir bug ci-dessous).
- [x] `PATCH /web/hebergements/{id}` — testé, réel (DB).
- [x] `DELETE /web/hebergements/{id}` — testé indirectement (nettoyage), a échoué une fois sur "introuvable" car la ligne était déjà auto-nettoyée par reconcile-on-read — comportement correct, pas un bug.
- [x] `PUT /web/hebergements/{id}/acces` — testé, 200, **corrigé cette session**. Écart trouvé : DB uniquement, aucun appel amont. Or `assurer_regle_ssh` ouvre le port 22 **sans condition** à la création de la VM (`service.py`, étape 1 de `ExecuteurHebergementCreer`) — le flag `acces.ssh` n'avait donc aucun effet réel côté réseau. Corrigé : ajout de `NetworkOpenStack.retirer_regle_ssh` (symétrique de `assurer_regle_ssh`) + `service.appliquer_acces_ssh`, branché dans le routeur. **Vérifié de bout en bout** : `ssh:false` supprime réellement la règle du security group (confirmé via l'API réseau), `ssh:true` la repose.
- [x] `POST /web/hebergements/{id}/attachement-domaine` — testé (deuxième domaine semé localement, sans achat réel — le contrôle `_exiger_domaine_detenu` ne vérifie que la base locale), **corrigé cette session**. Écart trouvé : DB uniquement — le champ `domaine` change, mais la règle L7 Traefik/Octavia posée à la création utilise `domaineProvisoire`, jamais réécrite : attacher un nouveau domaine ne routait aucun trafic réel vers lui. Corrigé : `appliquer_attachement_domaine` pose une vraie policy L7 Octavia (`host-<domaine>` → même pool que `domaineProvisoire`), nettoyée à la suppression de l'hébergement (Octavia refuse sinon de supprimer le pool). **Vérifié de bout en bout** : policy `host-synelia-attach-test-4471.xyz` confirmée réellement créée côté Octavia avec la bonne règle `HOST_NAME`.
- [x] `GET/POST .../comptes-fichiers` — testés, réels (create renvoie 201 direct, `appliquer_comptes_fichiers` real SSH gate confirmé en lecture de code).
- [x] `GET .../metriques` — testé, répond une série mais toutes les valeurs sont `0.0` — cohérent avec le comportement honnête documenté ailleurs (dégrade à vide/zéro sans observabilité configurée, jamais de donnée inventée).
- [x] `PUT .../php` — testé, **corrigé cette session**. Écart trouvé : DB uniquement, pas de job (`demarrer_travail`), aucun SSH vers le serveur réel — la version PHP "changeait" en base sans jamais toucher le vrai php-fpm de la VM. Corrigé : `appliquer_version_php` exécute un vrai `sed` sur `docker-compose.yml` + `docker compose up -d site` par SSH. **Vérifié de bout en bout, deux fois** : (1) bascule 8.4→8.2 confirmée à la fois dans le fichier compose et sur le conteneur réellement en cours d'exécution (`synelia-site-1` → `php:8.2-apache`) via script direct ; (2) re-vérifié via un vrai navigateur (agent-browser) contre le déploiement distant réel `api.cloud.dev01.ovh.smile.ci` — bascule 8.3→8.1 confirmée par `curl` indépendant, et un second essai renvoie correctement `409 version_php_identique`.
- [x] `POST .../redemarrage` — testé, réel (`amont().action(sid, "redemarrage")`, vrai reboot Nova).
- [x] `GET .../services-partages` — testé, catalogue statique (Messagerie/Drive/etc.), pas d'amont attendu ici — cohérent avec son rôle.
- [x] `POST/POST .../taches` (créer + exécuter) — testés, création réelle (DB), **corrigé cette session**. Écart trouvé : `ExecuteurTacheExecution.terminer` marquait la tâche `ok` sans jamais exécuter réellement `commande` par SSH sur le serveur — même famille de faux succès silencieux que ailleurs dans la plateforme. Corrigé : appelle maintenant `executer_commande_vps` (même discipline no-op simulé / 424 franc que le reste du module) et rapporte `echec` sur un vrai échec au lieu de toujours `ok`.
- [x] Sites (`POST /web/sites`, installer une app) — testé, **réel confirmé de la manière la plus convaincante** : SSH réel vers la VM, `docker compose up -d` réellement exécuté, a échoué une fois sur un vrai pull d'image Docker Hub (probable sortie internet/DNS de la zone VPS privée à vérifier séparément) — et l'échec est rapporté honnêtement, jamais maquillé en succès.
- [x] Bases de données (`GET /web/bases`, `POST /{serveurId}/bases`) — **testées en direct, réelles, vérifiées jusqu'au bout** : création de `testdb1` sur le serveur MariaDB partagé via l'API, puis confirmée par `SHOW DATABASES` en SSH direct sur le conteneur `synelia-bases-mariadb-1` de la VM — pas juste "l'API répond 201".

**Bug de conception réel, significatif, trouvé et corrigé** : `ExecuteurHebergementCreer.compensable = True`.
Quand l'étape 3 (routage LB) échoue, le moteur (`travaux/moteur.py::_executer`) appelle
automatiquement `compenser()`, qui **supprime le serveur Nova et l'IP flottante déjà créés**
(étape 2). Mais `relancer()` (`POST /travaux/{id}/relance`) reprenait **à l'étape qui a échoué**
(`depuis = index de l'étape failed`), donc rejouait directement l'étape 3 **sans jamais recréer
le serveur que la compensation venait de détruire**. Reproduit en direct sur ce lab : un job qui
échoue à l'étape 3, se fait relancer, et route bien un domaine sur le load balancer partagé —
pour un serveur qui n'existe plus. Le job se termine `done`, l'hébergement semble `en_ligne`,
et ce n'est qu'à la relecture suivante (reconcile-on-read) que la ligne redescend correctement
à `suspendu`/`maintenance` en détectant l'orphelin.
**Corrigé** : dans `travaux/moteur.py::relancer`, si `travail.statut == "rolled_back"` (la
compensation a réellement défait les étapes précédentes), on repart de l'étape 0 au lieu de
l'étape échouée. Sinon (`failed` simple, compensation absente ou elle-même en échec),
comportement inchangé. **Vérifié précisément** par un test dédié qui intercepte l'argument
`depuis` passé à `_executer` : `rolled_back` → `depuis=0` ; `failed` → `depuis=2` (l'étape
échouée).

## 4. web_ssl — certificats
Backend : `AcmeReel` via `SYNELIA_ACME_URL`. **Confirmé cette session (vérification directe, pas juste le registre)** : `grep SYNELIA_ACME_URL .env` → absente. **Statut : simulé, confirmé de première main.**

- [ ] Tous les endpoints (`/web/ssl/offres`, commander, lister, obtenir, patch, supprimer, renouveler, valider) — tournent en simulé (confirmé), comportement fonctionnel non re-testé cette session.

## 5. web_emails — messagerie (Zimbra)
Backend : `zimbra.ZimbraReel` via `SYNELIA_ZIMBRA_URL`+identifiants. **Vérifié cette session** : `SYNELIA_ZIMBRA_URL=https://zimbra:7071` et les identifiants sont bien présents dans `.env` (branché réel selon le registre). **Mais** : `zimbra` est un nom d'hôte Docker-compose, injoignable depuis ce sandbox (`vm-admin`) — `getent hosts zimbra` échoue. **Statut : configuré réel, non vérifiable en direct depuis cet environnement** — à retester depuis le bon hôte plutôt que d'affirmer "ça marche" sans l'avoir vu.

- [ ] Tous les endpoints (créer/lister/modifier/supprimer messagerie, alias, comptes, ouverture SSO) — non re-testés cette session (hôte injoignable depuis ici).

## 6. web_smtp — relais SMTP sortant
Backend : `relais_smtp.RelaisSmtpReel` via `SYNELIA_RELAIS_SMTP_HOTE` (correction : une version antérieure de cet inventaire citait `SYNELIA_ZIMBRA_SMTP_HOTE` par erreur — confusion avec une variable différente de `.env.example`, non lue par le code ; vérifié dans `packages/openstack/synelia_openstack/relais_smtp.py:32`). **Vérifié cette session** : `SYNELIA_RELAIS_SMTP_HOTE=relais-smtp` présent dans `.env` → branché réel. Même limite que web_emails : `relais-smtp` est un nom Docker-compose injoignable depuis ce sandbox. **Statut : configuré réel, non vérifiable en direct depuis cet environnement.**

- [ ] Tous les endpoints (relais, clés SMTP, test, webhooks) — non testés cette session (hôte injoignable depuis ici).

## 7. web_drive — stockage collaboratif (type Nextcloud sur le VPS)
Backend : `SshReel` (déploiement via SSH sur le VPS d'hébergement, pas un service séparé). **Statut : réel, débloqué et testé cette session** (hébergement `en_ligne` disponible).

- [x] `POST /web/drive` (activer) — réel : installe vraiment Nextcloud par SSH sur le VPS, route réellement le sous-domaine `drive.<domaine>` sur le LB partagé (mêmes étapes que `web_hebergement`, donc sujet à la même flakiness Octavia occasionnelle).
- **Bug réel trouvé et corrigé** : `ExecuteurDriveActivate.compenser` appelait `depot.definir_statut(ctx, did, "suspendu")` — mais le dépôt Drive a `champ_statut="actif"`, un booléen dans le contrat, pas une chaîne. Résultat : la compensation elle-même **plantait** (`Input should be a valid boolean`) dès qu'une étape échouait après la création de la ligne, empêchant le travail d'atteindre proprement `rolled_back`. Corrigé en `depot.modifier(ctx, did, {"actif": False})` (symétrique de `terminer()`). Un test (`test_drive_refuse_sans_hebergement`) attendait encore l'ancien comportement cassé (`statut == "failed"`) — corrigé pour attendre `rolled_back`, le comportement désormais correct.
- [ ] `GET/DELETE/PATCH /web/drive/{id}`, sièges, ouverture SSO — non testés en direct cette session (code lu, cohérent avec le reste du module).

## 8. web_backup — sauvegardes
Backend : délègue à `web_hebergement.service.amont()` (Nova/Glance), pas d'`amont()` propre.
**Statut : réel et honnête**, audité en détail cette session (lecture complète de `service.py`) :
- `web.backup.run` : crée un vrai snapshot Glance de la VM (`amont().instantane`), stocke l'id en secret. Sans serveur réel (démo), dégrade proprement sans inventer un succès.
- `web.backup.testrestauration` : vérifie que l'image Glance existe toujours et est `active` — pas juste "on suppose que oui".
- `web.backup.restore` : restaure réellement via `amont().restaurer` — mais **seulement en granularité `"complete"`** (VM entière). Le registre de capacités le disait "partiel", et c'est confirmé : `perimetre` (fichiers/bases/configuration/messagerie séparés) est un champ du contrat qui **ne correspond à aucune implémentation** — seule une restauration VM entière existe.

- [x] Restauration avec `granularite != "complete"` — corrigé : job `failed` + message explicite (plus de faux succès silencieux).

## 9. Interface web (synelia-cloud, `/opt/synelia/synelia-cloud`)
**Correction d'une supposition erronée en cours de session** : voir des imports `from '@/lib/mock'`
dans les fichiers Web Cloud avait fait conclure trop vite à un frontend "pas branché". En réalité,
ces imports ne sont que la **graine de repli hors-ligne/démo** d'un hook générique
(`useCollection(nom, graine)`, `src/components/app/atelier.tsx`) qui appelle le vrai backend
(`endpointDe()`, `src/lib/api/collections.ts`, déjà mappé sur `/web/hebergements`, `/web/domaines`,
`/web/dns`, `/web/drive`, `/web/ssl`, `/web/smtp/*`) dès que `NEXT_PUBLIC_API_URL` est posée
(c'est le cas : `https://api.cloud.dev01.ovh.smile.ci/v1`, la même URL publique que
`SYNELIA_URL_PUBLIQUE` côté backend).

Vérification faite d'abord par lecture directe du code appelant (`requete()`/`creerRessource()`)
comparé aux endpoints et formes de corps réellement attendues par le backend, **puis confirmée
par un vrai navigateur** (`agent-browser`, Chrome installé localement, session connectée en tant
qu'admin sur le déploiement réel partagé `api.cloud.dev01.ovh.smile.ci`) :
- **PHP** (`[id]/vue.tsx`) : `PUT .../php` avec `{versionDefaut, extensionsActivees, limites}` — correspond exactement. Testé en direct dans le navigateur : bascule 8.3→8.1 confirmée par `curl` indépendant après l'action UI, avec un `409` correct au second essai identique.
- **Accès SSH/FTP** : `PUT .../acces` avec `{ftp, sftp, ftps, ssh, portSsh}` — correspond, et bénéficie directement du correctif backend (le bouton SSH ouvre/ferme réellement le port 22 maintenant).
- **Installation d'application** (WordPress/PrestaShop/statique/PHP) : `POST /web/sites` avec `{hebergementId, site: {hote, type, phpVersion, creerBase, ssl}}` — correspond (`ssl` est un champ en trop, ignoré silencieusement côté Pydantic, sans risque).
- **Bases de données** : `POST /web/bases/{serveurId}/bases` avec `{nom, jeuCaracteres, utilisateur}` — correspond, testé en direct côté backend (MariaDB réelle).
- **Drive (Nextcloud)** : `POST /web/drive` avec `{domaine, palier, sieges}` — correspond exactement à `WebDrivePostRequest`.
- **Tâches planifiées (cron)** : exécution câblée sur `POST .../taches/{id}/execution` — bénéficie du correctif backend (exécute vraiment la commande par SSH désormais).
- **Achat/disponibilité de domaine** : `GET/POST /web/domaines*` — correspond exactement aux endpoints corrigés cette session.
- **DNS (zone/enregistrements/DNSSEC/modèles)** : éditeur complet (`src/components/business/editeur-zone.tsx`, monté depuis l'onglet « Zone » de la fiche domaine). Vérifié précisément sur le point le plus fragile (le chemin sans `/application` que j'avais corrigé côté backend) : `POST /web/dns/{zone}/modeles/{modele}` avec `corps: {}` — correspond exactement. CRUD enregistrements, DNSSEC, tout branché.

**Écart réel trouvé et corrigé** : `POST /web/hebergements/{id}/attachement-domaine` existait côté
backend (et fonctionne, vérifié en direct) mais **n'avait aucun point d'entrée dans l'interface** —
impossible pour un utilisateur d'attacher un domaine déjà possédé à un hébergement, seul l'achat
d'un nouveau domaine était proposé. Ajouté : un bouton « Attacher un domaine possédé » dans le
bandeau d'avertissement « nom provisoire » de `[id]/vue.tsx`, visible dès qu'un domaine de
l'organisation n'est attaché à aucun hébergement (`!d.hebergementId`), appelant le bon endpoint
avec le bon corps. `tsc --noEmit` propre sur tout le projet après l'ajout.

**Leçon tirée en testant via navigateur réel** : une première tentative de vérification PHP a
montré un "Failed to fetch" reproductible en ajoutant l'en-tête `X-Organisation-Id` — creusé à
fond (comparaison `curl` direct vs `fetch()` de page, préflight CORS vérifié permissif), puis
**disparu à la ré-exécution** : network flakiness réelle et transitoire sur ce déploiement
partagé (multi-sessions concurrentes visibles : comptes `e2e-*`/`bat-web-*`), pas un bug
d'application ni de CORS. Documenté pour ne pas re-paniquer sur le même symptôme plus tard.

**Bilan frontend** : sur l'ensemble des flux Web Cloud vérifiés (hébergement, PHP, accès SSH/FTP, installation d'app, bases de données, Drive, tâches cron, achat/disponibilité de domaine, zone DNS/enregistrements/DNSSEC/modèles), **un seul écart réel trouvé** (attachement de domaine sans bouton), corrigé. Le reste était déjà correctement câblé au vrai backend.

---

## Plan de travail (dans l'ordre)

1. Tester `attachement-domaine` avec un deuxième domaine réellement acheté (pas seulement semé en DB).
2. Re-tester web_ssl, web_emails, web_smtp pour confirmer leur statut réel/simulé depuis un hôte qui peut réellement les joindre (pas ce sandbox).
3. Optionnel (discuté, pas encore autorisé) : délégation DNS publique réelle vers OVH pour le domaine de test.
4. Vérification visuelle complémentaire (captures d'écran) des flux non encore cliqués en direct : web_ssl, web_emails, web_smtp, page Drive complète.
