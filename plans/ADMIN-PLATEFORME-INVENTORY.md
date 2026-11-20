# Admin & Plateforme — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/{admin,admin_jetons,support,transverses,docs,public,observabilite,tableau_de_bord}`
(routers + services complets) + `uv run synelia capacites` + lecture de `synelia.travaux` (moteur de jobs) et
`synelia.deps.rbac`. **Pas d'appel réel exécuté** dans cette session : `.env` porte des identifiants d'infra
vivante (`SYNELIA_FOURNISSEUR=openstack`, `SYNELIA_OS_AUTH_URL=...`), donc audit par lecture de code uniquement,
pas de `pytest` d'intégration lancé contre le lab.

Légende : **réel** = lit/écrit une vraie table DB ou appelle un amont véritable ; **simulé/synthétique** =
données statiques ou fabriquées en mémoire, aucun amont ; **dégradé honnête** = amont non configuré →
réponse vide/zéro plutôt qu'une donnée inventée ; **stub** = code présent mais qui ne fait rien
(`pass`, pas d'override, ou route sans exécuteur enregistré).

## Rappel RBAC (infrastructure commune)

`apps/synelia/synelia/deps/rbac.py` :
- `exige(action, lecture=)` : exige que le rôle **org** du principal ait la permission `action` (`rbac.autorise`). Utilisé pour les routes clients normales.
- `exige_admin(action=None)` : exige `principal.est_admin_plateforme` (équipe Synelia) ; si `action` est fourni, vérifie en plus `role_equipe` a cette permission. `exige_admin(None)` (7 endpoints dans `admin/router.py`) n'exige que l'appartenance à l'équipe, pas de permission fine — **pas un bug**, comportement documenté du helper.
- `Ctx` (bare, sans `Depends(exige(...))`) : authentifie seulement (org + utilisateur), **aucune vérification de permission**. C'est le principal écart trouvé dans cet audit (voir §3, §4, §7).
- `CtxPublic` : aucune authentification — normal pour `public/*` et 2 routes statiques (`docs/sections`).

Registre de jobs asynchrones (`synelia.travaux.moteur`) : `executeur_pour(type)` retombe **silencieusement**
sur la classe de base `Executeur` (aucune levée d'erreur) si aucun `@executeur("type")` n'est enregistré pour
ce type — la classe de base fait juste dormir puis termine chaque étape en succès (`simulation_duree_etape_ms`)
sans rien faire côté métier. Un appel `demarrer_travail(ctx, "type.inconnu", ...)` réussit donc en apparence
(`202`, job qui finit `ok`) sans jamais exécuter la logique attendue — **faux succès silencieux**, cf. bug trouvé en §1.

---

## 1. admin — back-office plateforme (équipe Synelia)

Fichiers : `admin/router.py` (1404 lignes, 45 endpoints), `admin/service.py` (327 lignes).
**RBAC : correct partout** — chaque route utilise `exige_admin(...)`, jamais `exige()` nu, jamais sans dépendance.
Aucune route `/admin/**` n'est accessible à un utilisateur organisation normal.

Amont : `amont()` (service.py:30) → `fournisseur(ComputeSimule, ComputeOpenStack)` (bascule sur `SYNELIA_FOURNISSEUR`/`SYNELIA_OS_AUTH_URL`, réel dans ce `.env`).

- [x] `GET /admin/audit` — réel (DB `Audit`/`Utilisateur`/`Organisation`).
- [x] `GET/PATCH /admin/backends`, `/admin/backends/{id}` — réel (Depot + `amont().capacite_plateforme()` via Nova).
- [x] `GET /admin/capacite` — réel, **mais** agrégation par site codée en dur `("ABJ", "GBM")` alors que seul ABJ existe réellement (voir bug ci-dessous).
- [x] `GET /admin/espaces` — réel (Depot, filtré `org_id is None`).
- [ ] `GET /admin/conformite` — **entièrement synthétique** : renvoie la liste statique `REFERENTIELS` (ISO 27001, SOC2, RGPD, HDS, SWIFT CSP, PCI DSS) codée en dur en tête de fichier, pas de source réelle de statut de conformité.
- [x] `GET/POST /admin/conformite/fenetres-patching` — réel (DB).
- [x] `POST /admin/conformite/tests-restauration` — démarre un `Travail` réel en DB, mais son exécuteur (`ExecuteurTestsRestauration`) est **un stub vide** (`class ExecuteurTestsRestauration(Executeur): pass`, service.py:300-302) : le job se termine "ok" sans jamais valider ni rapporter quoi que ce soit sur une restauration réelle. Les durées d'étapes affichées (18s/480s/90s/12s) sont elles-mêmes simulées.
- [x] `GET/POST/PATCH/DELETE /admin/equipe...`, `/elevation` — réel (DB `Utilisateur`), garde-fou "dernier super-admin" présent.
- [x] `GET/PATCH /admin/leads` — réel (DB).
- [x] `GET/POST /admin/marketplace/campagnes`, `.../lancement` — réel (DB) + job démarré via `ExecuteurMaj`, qui se contente de passer le statut à `"terminee"` (pas d'orchestration réelle derrière — automation simulée, documentée ici pour éviter toute confusion).
- [ ] `POST /admin/marketplace/campagnes/{id}/suspension` — **bug** : démarre un job `"admin.suspension"` (2 points d'appel, lignes 741 et 940) **sans exécuteur enregistré** → retombe sur la classe `Executeur` de base, le job finit "ok" en simulant des étapes génériques, sans jamais réellement suspendre la campagne au niveau infra (le champ DB `statut` est bien mis à `"suspendue"` avant l'appel, donc l'état visible côté UI est correct, mais tout traitement métier attaché au job — notifications, actions downstream — n'a jamais lieu).
- [x] `GET/POST /admin/migration/campagnes`, `.../lancement` — réel (DB) + `ExecuteurMigration` (statut → "terminee" seulement, même remarque que Maj).
- [ ] `POST /admin/migration/campagnes/{id}/rollback` — **bug identique** : démarre `"admin.migration.rollback"` (ligne 908-914), **aucun `@executeur("admin.migration.rollback")` nulle part dans le repo** (vérifié par grep global) → faux succès silencieux, malgré `exiger_confirmation` correctement posé en amont.
- [ ] `POST /admin/migration/campagnes/{id}/suspension` — même bug `"admin.suspension"` que ci-dessus (ligne 940-942).
- [x] `POST /admin/organisations/{orgId}/notification` — réel (DB, écrit une `Ressource` type notification).
- [x] `GET /admin/organisations/{orgId}/espaces|membres|tickets` — réel, lecture cross-org explicitement justifiée en commentaire (scope équipe Synelia).
- [x] `PUT /admin/placements` — réel (DB, delete+recreate).
- [x] `GET /admin/sante` — mixte réel : OpenStack (via `amont().capacite_plateforme()`), VictoriaLogs/Grafana (vrai GET HTTP si URL configurée), Temporal (vrai `Client.connect()`) ; **Centreon codé en dur à `"degrade"` en permanence** (aucun client jamais écrit, documenté en commentaire) ; `"alertes": []` toujours vide (aucune source d'alerte câblée).
- [ ] `GET /admin/sites` — **entièrement synthétique** : `SITES_PHYSIQUES` codé en dur, une seule entrée ABJ. Point positif : un commentaire (lignes 1103-1106) documente qu'un faux site "GBM" (Tier III, 800kW, ISO27001, latence 4ms inventés) a été **supprimé le 2026-09-09** — nettoyage volontaire d'une donnée fantôme, pas une régression.
- [x] `GET/POST /admin/statut/incidents`, `PUT /admin/statut/services` — réel (DB).
- [x] `GET /admin/tableau-de-bord` — réel (agrégations DB), sauf `"caMensuel": 0` codé en dur avec commentaire explicite (le module facturation n'agrège pas encore le CA récurrent — lacune documentée, pas cachée).
- [x] `GET/PATCH/POST /admin/tickets...` — réel (DB).
- [x] `GET /admin/travaux` — réel (`select(Travail)`).

**Bugs trouvés (admin)** :
1. **`"admin.suspension"` et `"admin.migration.rollback"` : jobs démarrés sans exécuteur enregistré** → faux succès silencieux (voir mécanisme décrit en préambule). 3 points d'appel : `admin/router.py:741-742`, `admin/router.py:940-941`, `admin/router.py:908-914`. Impact : un opérateur Synelia qui suspend/annule une campagne voit un `202` et un job qui finit "ok", en pensant qu'un traitement a eu lieu, alors que seule la ligne DB `statut` a changé — pas corrigé cette session (risque de fixer une sémantique métier que je ne connais pas sans plus de contexte sur ce qu'"admin.suspension" est censé faire réellement).
2. `GET /admin/capacite` agrège sur `("ABJ", "GBM")` en dur alors que "GBM" n'existe plus (cf. nettoyage `/admin/sites`) — incohérence mineure entre deux endpoits du même module, pas re-corrigée.
3. `ExecuteurTestsRestauration` est un stub vide — le "test de restauration" de conformité ne teste rien.

## 2. admin_jetons — jetons API plateforme (équipe Synelia)

Fichier : `admin_jetons/router.py` (220 lignes, 6 endpoints), pas de `service.py` séparé.
**RBAC : correct et cohérent** — toutes les routes utilisent `exige_admin("sso.configure")` (nom d'action un peu
trompeur pour de la gestion de jetons API, mais appliqué uniformément, pas un bug).

- [x] `GET/POST /admin/jetons`, `GET/PATCH/DELETE /admin/jetons/{id}`, `POST /admin/jetons/{id}/rotation` — tous réels (DB `CleApi`), secret haché (`hacher_jeton`), jamais relisible en clair après création/rotation.
- [x] Double garde-fou supplémentaire : `_role_emetteur` (le rôle équipe émetteur doit être dans `ROLES_EQUIPE`) et `_valider_portee` (un jeton émis ne peut jamais avoir une portée supérieure à celle du rôle émetteur) — empêche un rôle équipe restreint de fabriquer un jeton plus puissant que lui-même. `exiger_confirmation` posé sur la révocation.
- Aucun écart trouvé dans ce module.

## 3. support — tickets & base de connaissances

Fichiers : `support/router.py` (203 lignes, 9 endpoints), `support/service.py` (75 lignes).

- [x] `GET /support/base-connaissances`, `GET .../{id}` — `CtxPublic`, données statiques `ARTICLES_KB` en mémoire — cohérent, KB publique.
- [ ] `POST /support/pieces` — **RBAC manquant** : `ctx: Ctx` seul (authentifié, aucune action vérifiée), écrit en DB réelle (`Depot("ticket_piece").creer`).
- [ ] `GET/POST /support/tickets`, `GET/PATCH /support/tickets/{id}`, `POST .../escalade`, `POST .../messages` — **RBAC manquant sur les 7 endpoints** : tous utilisent `ctx: Ctx` nu, `exige`/`exige_admin` ne sont **même pas importés** dans ce fichier. Tout utilisateur authentifié de n'importe quel rôle peut lister/créer/modifier/escalader tous les tickets de l'organisation, sans aucune vérification de permission au-delà de l'authentification.

**Écart de premier principe** : c'est le seul module non-admin de l'univers où l'écriture de données réelles
(tickets client, potentiellement sensibles) n'est protégée par **aucune** permission d'action — juste
l'appartenance à l'org via l'authentification. À comparer avec `tableau_de_bord` qui a le bon pattern
(`exige("org.dashboard.view", lecture=True)`).

## 4. transverses — RBAC matrix, référentiels, onboarding, recherche

Fichier : `transverses/router.py` (208 lignes, 5 endpoints), pas de `service.py`, logique inline.
`exige`/`exige_admin` **non importés** ici non plus.

- [x] `GET /referentiels` — `CtxPublic`, statique — cohérent.
- [ ] `GET /rbac/matrice` — `Ctx` nu (authentifié seulement), données statiques (`rbac.matrice()`) — faible sensibilité mais pas de contrôle d'action.
- [ ] `GET /onboarding` — `Ctx` nu, lit l'état d'onboarding réel de l'org (DB) — pas de contrôle d'action.
- [ ] `PATCH /onboarding` — `Ctx` nu, **écrit** `Organisation.onboarding` — endpoint de mutation sans aucune vérification de permission au-delà de l'authentification ; utilise en plus un `assert o is not None` nu (lève une `AssertionError` → 500 non maîtrisé au lieu d'une erreur 404 propre si l'org est introuvable, et serait supprimé sous `python -O`).
- [ ] `GET /recherche` — `Ctx` nu, recherche cross-ressources réelle (`select(Ressource)` filtré sur `ctx.org_id` — pas de fuite cross-tenant, mais pas de contrôle par type de ressource/permission).

## 5. tableau_de_bord — dashboard organisation & copilote

Fichier : `tableau_de_bord/router.py` (206 lignes, 3 endpoints), pas de `service.py`.

- [x] `GET /tableau-de-bord` — **le seul endpoint bien gardé de ces 3 modules** : `exige("org.dashboard.view", lecture=True)`. Données réelles : comptages DB (`Depot.compter`), consommation (`metrologie.consommation`), SLA (`facturation_service.sla_engagements`), tickets, jobs en cours (`select(Travail)`). Aucun `random.randint`/liste statique trouvé pour les métriques.
- [ ] `POST /copilote` — **RBAC manquant, incohérence directe avec l'endpoint précédent** : `ctx: Ctx` nu, appelle la même agrégation interne `_synthese(ctx)` (mêmes données financières/consommation) que `GET /tableau-de-bord`, mais sans `exige("org.dashboard.view", ...)`. C'est un **contournement RBAC** vers la même donnée sensible (dépense mensuelle, prévision, quotas) via le chat copilote.
- [ ] `GET /copilote/suggestions` — `Ctx` nu, liste statique `SUGGESTIONS` — faible risque (chaînes fixes) mais toujours sans contrôle d'action.
- Champs notés `None` en dur dans `_synthese` (`siegesUtilises`, `siegesSouscrits`, `facturesEnAttente`, `prochainRdv`) — non implémentés, pas de commentaire `TODO` les signalant explicitement, à documenter comme "connu-incomplet".
- Point positif : `uptime30j`/`slaContractuel` ont un commentaire indiquant qu'un ancien chiffre fixe a été remplacé par un calcul réel (`facturation_service.sla_engagements`) — dette technique déjà résorbée.

## 6. docs — bac à sable, parcours de formation, progression

Fichiers : `docs/router.py` (174 lignes, 8 endpoints), `docs/service.py` (148 lignes).

- [x] `GET /docs/sections` — `CtxPublic`, statique — cohérent (contenu marketing/index).
- [ ] `GET/POST/DELETE /docs/bac-a-sable`, `GET /docs/parcours[/…]`, `POST /docs/parcours/.../completion`, `GET /docs/progression` — **RBAC manquant sur 7 endpoints** : `exige`/`exige_admin` ne sont importés nulle part dans ce fichier, seul `Ctx` (authentifié) protège des écritures réelles en DB, dont le démarrage d'un job de provisioning (`POST /docs/bac-a-sable` → `demarrer_travail`, exécuteur `ExecuteurBacASable` bien enregistré et fonctionnel cette fois). Pas de limitation de fréquence sur la création/suppression du bac à sable malgré la charge (job de provisioning) — un utilisateur authentifié pourrait créer/supprimer en boucle.
- [ ] `POST /docs/parcours/{slug}/modules/{slug}/completion` — génère une URL d'attestation `attestationUrl=f"/docs/certificats/{parcoursSlug}/{utilisateur_id}"` qui **ne correspond à aucune route existante** dans ce routeur (ni ailleurs trouvé par grep) — lien mort, fonctionnalité de certificat téléchargeable jamais implémentée côté API.
- [x] `GET /docs/progression` — correctement scopé à `ctx.utilisateur_id`, pas de fuite cross-utilisateur malgré l'absence de contrôle d'action fin.

## 7. public — vitrine publique (site marketing, sans authentification)

Fichiers : `public/router.py` (448 lignes, 18 endpoints), `public/service.py` (580 lignes).
Module intentionnellement 100% `CtxPublic` (documenté dans le docstring) — cohérent avec son rôle de vitrine.
Aucun `amont()`, aucune variable d'environnement `SYNELIA_*` référencée dans `service.py` (confirmé par grep) :
tout le contenu est statique ou dérivé de packages internes (`synelia_catalogue`, `synelia_openstack.compute.GABARITS`).

- [x] Contenu statique/marketing (`/catalogue/services`, `/couverture`, `/datacenters`, `/etudes-cas`, `/pages-legales`, `/sla`, `/souverainete`, `/tarifs`, `/simulateur`) — cohérent, public par nature, rien à corriger.
- [x] `GET /public/offres[/{slug}]`, `GET /public/statut` — DB réelle en premier (`detenteur_offres`/`detenteur_statut`), repli sur données statiques si la table est vide — comportement honnête, pas de simulation déguisée.
- [x] `GET /public/disponibilite-domaine` — requête DB réelle (`select(Ressource)` type `web_domaine`) + set figé `{"google.com", "synelia.ci"}` pour les cas connus ; ne renvoie qu'un booléen, pas de fuite de données tenant.
- [ ] `POST /public/contact`, `POST /public/devis` — écrivent des **leads réels en DB avec PII** (nom/email/téléphone/organisation/message), **aucune limitation de fréquence ni CAPTCHA visible** dans le routeur ou le service — surface d'abus classique (spam/flood) pour un formulaire public sans authentification.
- [ ] `POST /public/devis` — le champ `configurationSimulee` (estimation de prix) est **fourni par le client et persisté tel quel**, sans revalidation serveur — un lead pourrait contenir un prix fabriqué côté client ; impact limité (n'affecte pas la facturation réelle, juste l'enregistrement du lead), mais à noter.
- [x] `GET /public/statut/incidents/{incidentId}` — vérifié : `Depot.obtenir()` (apps/synelia/synelia/depot.py:138-142) lève déjà `erreurs.introuvable(...)` si la ligne n'existe pas → **pas un bug** (le sous-agent qui a fait la première passe avait flaggé un risque de 500 ici, infirmé après lecture de `Depot.obtenir`).

---

## 8. observabilite — métriques/logs (VictoriaMetrics/VictoriaLogs)

Fichiers : `observabilite/router.py` (194 lignes, 9 endpoints), `observabilite/service.py` (208 lignes),
`packages/openstack/synelia_openstack/victoria.py`.

D'après `uv run synelia capacites` : `victoria.observabilite  simule  SYNELIA_VICTORIAMETRICS_URL  Dégradé en [] sans URL`.
**Vérifié en détail, confirmé exact** : `amont()` (`victoria()` en service.py:25-26) sélectionne `VictoriaReel` au lieu de
`VictoriaSimule` dès que `SYNELIA_FOURNISSEUR=openstack` + creds OS sont posés (c'est le cas dans ce `.env`) —
mais `VictoriaReel.serie()`/`.valeur()`/`.extrait_logs()` vérifient **en plus** individuellement
`os.environ.get("SYNELIA_VICTORIAMETRICS_URL"/"SYNELIA_LOGS_URL")` et renvoient `[]`/`0.0` immédiatement si
absent — sans jamais fabriquer de donnée. Toute erreur HTTP (`httpx.HTTPError`) est aussi absorbée vers le même
`[]`/`0.0`, jamais vers une valeur plausible inventée. `SYNELIA_VICTORIAMETRICS_URL` **n'est pas posée** dans ce
`.env` (seulement requise dans `docker-compose.dev01.yml`). **Conclusion : observabilite ne fabrique aucune
donnée de graphique — dégradation honnête confirmée par lecture de code et par un test de régression existant
(`test_metriques_zero_remplies`)**.

- [x] `GET/POST/PATCH/DELETE /observabilite/alertes[...]` — réel (DB `Depot`), `exige("network.manage")` en écriture / `exige("org.dashboard.view", lecture=True)` en lecture — RBAC correct.
- [ ] `POST /observabilite/alertes/{id}/test` — **stub** : `exige(None)` (n'importe quel utilisateur authentifié), ne contacte aucun canal réel, renvoie juste `{"envoye": True, "canaux": regle.canaux}` codé en dur — prétend avoir envoyé une notification de test sans jamais le faire.
- [x] `GET /observabilite/evenements` — `exige(None)`, lit la table `Travail` (jobs de provisioning), pas Victoria — réel mais nom trompeur ("événements" ≠ métriques d'observabilité au sens strict).
- [x] `GET /observabilite/journaux` — `exige(None)`, `_victoria().extrait_logs()` — dégrade honnêtement en `[]` si non configuré.
- [x] `GET /observabilite/metriques` — `exige(None)`, `service.metriques()` → séries `victoria().serie()` par métrique + 3 tuiles `victoria().valeur()`. Seul point "généré" : un squelette de points à `valeur: 0.0` (pas aléatoire) quand Victoria ne renvoie rien, pour que le composant `SparkChart` front ait toujours le bon nombre de points — comportement documenté en commentaire et couvert par test.
- Remarque RBAC : `evenements`, `journaux`, `metriques`, et le test d'alerte utilisent `exige(None)` (authentification seule, pas de permission fine) alors que les routes CRUD `alertes` sont bien gardées par `network.manage`/`org.dashboard.view` — incohérence mineure de granularité, pas une fuite cross-org (tout reste scopé par `ctx`).

---

## Plan de travail (dans l'ordre)

1. **Corriger les 3 jobs admin sans exécuteur** (`admin.suspension` ×2, `admin.migration.rollback` ×1, `admin/router.py:741,908,940`) — soit enregistrer un vrai `@executeur(...)`, soit faire échouer explicitement le job (`failed` + message clair) plutôt que de laisser le faux succès silencieux de la classe `Executeur` de base. Même pattern que le fix déjà fait sur `web.backup.restore` (granularité invalide → `failed`, cf. WEB-CLOUD-INVENTORY.md §8).
2. **Ajouter les contrôles `exige(...)` manquants** sur les modules `support` (7 endpoints, dont écriture de tickets), `docs` (7 endpoints, dont provisioning bac-à-sable), `transverses` (4 endpoints, dont mutation `onboarding`), et `tableau_de_bord.POST /copilote` (contournement direct du RBAC de `GET /tableau-de-bord` vers les mêmes données financières). Prioriser `POST /copilote` et `support` (écriture de données client) en premier — impact le plus direct.
3. Remplacer `class ExecuteurTestsRestauration(Executeur): pass` par une implémentation qui vérifie réellement quelque chose (ou documenter/retirer l'endpoint `/admin/conformite/tests-restauration` s'il n'est pas prêt).
4. Ajouter une limitation de fréquence basique sur les formulaires publics `POST /public/contact` et `POST /public/devis` (spam/flood sur des PII réelles).
5. Remplacer l'`assert o is not None` de `PATCH /transverses/onboarding` par une erreur 404 gérée proprement.
6. Optionnel : réconcilier `GET /admin/capacite` (référence encore "GBM") avec le nettoyage déjà fait sur `/admin/sites`.
7. Optionnel : documenter ou retirer le lien mort `attestationUrl` (`/docs/certificats/...`) tant que la route n'existe pas.
