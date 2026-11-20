# Facturation & Catalogue — inventaire des fonctionnalités (état réel, vérifié le 2026-09-24)

Source : lecture directe de `apps/synelia/synelia/modules/{facturation,catalogue,admin_catalogue,
applications,services_manages,deploiements}` + `uv run synelia capacites`. Pas d'appel réel à un
prestataire de paiement effectué dans cette session (voir note Paystack ci-dessous) — audit fait
par lecture de code, pas par test d'intégration, conformément à la consigne (zone à risque
financier). `docs/PLAN-DIRECTEUR.md` décrit une architecture Node.js/CinetPay/Stripe **obsolète** :
le code réel, en Python, n'utilise ni CinetPay ni Stripe — uniquement **Paystack**. Ne pas s'y fier.

Légende : **réel** = appelle un amont véritable ; **simulé** = classe/branche `*Simule`, données
fabriquées ; **partiel** = mélange selon l'endpoint ; **jamais implémenté** = endpoint/travail
enregistré mais aucun exécuteur réel derrière (retombe sur `Executeur` générique = succès simulé
silencieux).

## 0. Paystack (paiement réel) — statut des clés
`.env` contient `PAYSTACK_PUBLIC_KEY=pk_test_...` et `PAYSTACK_SECRET_KEY=sk_test_...` : ce sont des
clés **sandbox test** Paystack (préfixe `pk_test_`/`sk_test_`), pas des clés de production —
aucune charge réelle n'est possible avec ces identifiants même si un appel avait été fait. Aucun
appel n'a été effectué dans cette session (lecture de code seulement, par prudence).

## 1. facturation — cycle mensuel, factures, paiement, prépayé, SLA, souscriptions
Fichiers : `router.py` (530 lignes), `service.py`, `paystack.py`, `tarification.py`, `metrologie.py`.

### Paiement (le plus à risque)
- **Réel côté intégration Paystack** : `paystack.py` fait de vrais appels HTTP à
  `https://api.paystack.co/transaction/verify/{reference}` avec la clé secrète (`GET
  /facturation/paystack/verifier/{reference}`), vérifie une vraie signature HMAC-SHA512 sur le
  webhook (`POST /facturation/paystack/webhook`), et ne crédite **jamais** sans revérifier le
  montant payé contre `facture.total` (`confirmer_paiement`) — pas de confiance aveugle au client.
  Idempotence correcte : facture déjà `payee` → no-op ; prépaiement déterministe par UUID5 dérivé
  de la référence (évite un double crédit webhook+callback).
- **`POST /facturation/factures/{id}/paiement`** — ceci N'EST PAS un paiement Paystack : c'est un
  règlement manuel/direct qui crédite immédiatement et marque `payee`, sans passer par aucun
  prestataire. À utiliser seulement pour virement/espèces déclarés a posteriori. Il n'y a **aucun
  contrôle empêchant un appelant quelconque avec la permission `payment.update` de "payer" une
  facture sans qu'aucun argent n'ait réellement changé de main** — c'est le comportement attendu
  pour un règlement manuel mais le nom de l'endpoint (`paiement`, pas `paiement-manuel`) et
  l'absence de distinction dans le contrat prêtent à confusion avec un vrai paiement carte.
- **`/factures/{id}/paystack/initier`**, **`/paystack/prepayer`** — réels, préparent juste la
  référence + montant pour le popup Inline.js, pas de charge côté serveur.
- **Remboursements/annulations : n'existent pas.** Aucun endpoint, aucune fonction `rembourser`
  dans `paystack.py` ou `service.py`. Une facture `payee` ne peut être ni annulée ni recréditée en
  négatif par l'API — le seul mécanisme de crédit inverse serait un `crediter()` manuel avec un
  montant négatif, jamais exposé. **Écart de premier principe** : un produit qui accepte des
  paiements réels (même en sandbox aujourd'hui) sans aucun chemin de remboursement est un gap
  fonctionnel majeur avant mise en production, pas un détail.

### Cycle de facturation (génération des factures)
- `POST /admin/facturation/cycle` → `demarrer_travail(ctx, "facturation.cycle", ...)` →
  `@executeur("facturation.cycle")` **réellement implémenté** (`service.py:230`) : parcourt les
  organisations actives, construit une facture par org via `construire_facture` (lignes
  abonnement + consommation, TVA via `argent.ttc`), échecs par org capturés sans bloquer le cycle,
  résultat agrégé écrit sur le `CycleFacturation`. Numérotation séquentielle globale
  `SYN-{année}-{n:06d}` correcte (comptage `Ressource.type == "facture"`).
- Notification email best-effort après émission (`_notifier_facture_emise`), échec d'envoi
  n'échoue jamais le cycle — comportement honnête, documenté en commentaire.

### Corrigé : `facturation.export` — travail sans exécuteur (faux succès silencieux)
`POST /facturation/consommation/export` appelait `demarrer_travail(ctx, "facturation.export", ...)`
sans aucun `@executeur` enregistré — le travail passait `done` sans jamais produire de fichier,
et l'URL `/v1/travaux/{id}/export` rendue par l'API ne menait nulle part (route inexistante).
**Corrigé** : ajout de `ExecuteurFacturationExport` (génère un CSV/JSON réel depuis
`metrologie.consommation`, dépose via MinIO) et de la route `GET /v1/travaux/{id}/export`
(servait le fichier déposé). **Vérifié de bout en bout** par un test qui poste l'export, attend
`done`, télécharge l'URL rendue, et vérifie le contenu CSV réel.

### Endpoints (checklist)
- [x] `GET /facturation/consommation` — réel (calcule depuis VM/volumes/LB réellement provisionnés, `metrologie.py`).
- [x] `POST /facturation/consommation/export` — **corrigé cette session**, réel de bout en bout (voir ci-dessus).
- [x] `GET /facturation/devis`, `POST /devis/{id}/acceptation` — réel (DB + crée une souscription).
- [x] `POST /facturation/estimation` — réel, calcul déterministe (`tarification.py`), pas d'amont externe à appeler (c'est un simulateur de prix, pas un provisioning).
- [x] `GET /facturation/factures`, `GET /factures/{id}`, `GET /factures/{id}/pdf` — réel (DB). Le PDF est un gabarit minimal fabriqué à la volée (`_PDF` template littéral, pas de moteur PDF) — fonctionnel mais rudimentaire (pas de mise en page réelle, une seule police).
- [x] `POST /factures/{id}/paiement` — réel mais **paiement manuel**, voir note ci-dessus.
- [x] `POST /factures/{id}/paystack/initier`, `POST /paystack/prepayer`, `GET /paystack/verifier/{ref}`, `POST /paystack/webhook` — réel, intégration Paystack sandbox correctement câblée (vérif serveur-side systématique, jamais de confiance au client).
- [x] `GET/POST/PATCH/DELETE /moyens-paiement` — réel (DB), pas de tokenisation carte réelle (juste `•••• {4 derniers chiffres}` stockés tels quels dans `numero`) — acceptable pour une démo, pas pour du PCI-DSS réel.
- [x] `POST /prepaye/rechargement` — réel (crédite directement, pas de lien avec un paiement Paystack confirmé — un appelant avec `payment.update` peut créditer un solde prépayé sans jamais avoir payé). Même remarque que `/paiement` manuel.
- [x] `GET /facturation/sla` — réel mais engagement contractuel de repli honnête : si aucune opération mesurée sur 30j, affiche l'engagement (99.9%) plutôt qu'un chiffre inventé (voir commentaire `sla_engagements`, ligne 117-119).
- [x] `POST /sla/reclamations` — **corrigé cette session** : exige maintenant un manquement réel (`constate < dispo` sur `sla_engagements`, 30 derniers jours) avant de créditer, `409 sla_non_manque` sinon, `422` pour un composant inconnu. Testé avec 3 cas (aucun manquement → 409, composant inconnu → 422, manquement réel simulé via des `Travail` en échec → 201).
- [x] `GET/PATCH/DELETE /souscriptions` — réel (DB).
- [x] `GET /facturation/ventilation` (showback) — réel, calcule depuis les ressources provisionnées réelles.

## 2. catalogue — gabarits (flavors) & images système
Backend : `fournisseur(ComputeSimule, ComputeOpenStack)` — sélecteur générique du kernel
OpenStack, **réel** quand `SYNELIA_FOURNISSEUR=openstack` est posé (`compute.serveur: reel` dans
`capacites`), simulé sinon. Cache mémoire process 10 min (`_memo`).
- [x] `GET /catalogue/gabarits`, `GET /catalogue/images` — réel/simulé selon l'environnement, correctement gaté. Rien à signaler.

## 3. admin_catalogue — offres, familles, fiches de service, modèles applicatifs, facturation plateforme
- [x] `GET/POST/PATCH/DELETE /admin/catalogue/offres`, `/publication` — réel (DB), règles de garde correctes (pas de suppression d'une offre publiée ou souscrite).
- [x] `GET /admin/catalogue/familles` — réel, agrégat calculé depuis les offres.
- [x] `PUT /admin/catalogue/services/{slug}`, `POST .../versions` — réel (DB).
- [x] `PUT /admin/modeles/{slug}` — réel (DB).
- [x] `POST /admin/facturation/cycle` — réel, lance `facturation.cycle` (voir §1, exécuteur réellement implémenté).
- [x] `GET /admin/facturation/cycles`, `/factures`, `/impayes` — réel (DB + calcul de retard sur échéance).
- [ ] **`POST /admin/facturation/impayes/relances`** — **faux relances**. Incrémente juste un compteur `relances` sur la facture (`router.py:406`) ; **aucun email, aucune notification, aucun appel `courriel.envoyer` n'est déclenché**. L'admin croit avoir "relancé" des clients en impayé, rien n'est envoyé. Écart de premier principe pour un module dunning.
- [ ] **`GET /admin/facturation/marges`** — **données 100% fictives, câblées en dur**. `lister_marges_backends` renvoie une liste statique `coutInfra=0, revenu=0, marge=0.0` pour chaque backend (OpenStack/Ceph/Postgres/K8s/Réseau), sans lire aucune donnée réelle de coût ni de revenu (`router.py:421-432`). L'endpoint existe et répond 200 mais n'a **jamais été implémenté** — c'est un stub qui donne l'illusion d'un tableau de marge fonctionnel.

## 4. applications — module vide
`apps/synelia/synelia/modules/applications/` ne contient **ni `router.py`, ni `service.py`, ni
`__init__.py`** — seulement un dossier `tests/` vide (pas de fichier de test non plus, juste un
`__pycache__`). Aucune route n'est enregistrée pour ce module ; si un contrat/frontend s'attend à
un module `applications` distinct de `deploiements`/`services_manages`, il n'existe pas. À
clarifier : soit le module a été renommé/fusionné dans `deploiements` (probable, vu le chevauchement
fonctionnel avec `ApplicationPaas`/`app.deploy`) et le dossier mort devrait être supprimé, soit
c'est un module prévu jamais commencé.

## 5. services_manages — catalogue de services managés + cycle de vie souscription
Backend catalogue : statique en dur dans le code (`_NOMS`, `_SOLUTION_OSS`, `_CATEGORIE`, `_MODES`
— 13 services : Drive Pro/Nextcloud, E-mail Pro/Stalwart, Visio/Jitsi, GED/Mayan, ERP/Odoo,
CRM/EspoCRM, WordPress, PrestaShop, BI/Metabase, Forge/GitLab CE, Coffre/Vaultwarden,
Automatisation/n8n, Analytics/Matomo). Pas de provisioning réel d'aucune de ces solutions open
source — aucune trace d'appel Ansible/Helm/Terraform ni d'API de déploiement de conteneurs dans ce
module. C'est un catalogue déclaratif, le "provisioning" est simulé par les exécuteurs.

- [x] `souscrire` → `@executeur("service_manage.subscribe")` **implémenté mais superficiel** : passe juste le statut à `operationnel` et fabrique une URL `https://{slug}-{id8}.apps.synelia.cloud` (`_url_native`, pure concaténation de chaîne, **aucune vérification que ce domaine/service existe réellement** — pas d'appel DNS, pas de déploiement, pas de test de disponibilité).
- [x] `résilier` → `@executeur("service_manage.resilier")` implémenté : suppression logique DB uniquement, pas de désinscription/nettoyage d'une ressource externe réelle (cohérent avec le fait qu'aucune ressource externe n'a jamais été créée).
- [x] **`exporter` → `@executeur("service_manage.export")` — corrigé cette session.** Dépose maintenant un export JSON réel (configuration/métadonnées du service — seule donnée dont ce module dispose, pas de contenu applicatif fabriqué) via MinIO, et fait progresser la ligne `ExportService` de `en_cours` à `pret`/`echec`. Testé de bout en bout (`test_cycle_souscription`).
- [x] `mise_a_jour` → `@executeur("service_manage.mise_a_jour")` implémenté : change juste le champ `version` en DB, pas de mise à jour réelle du logiciel sous-jacent (cohérent, il n'y a pas de logiciel réel derrière). **Complété cette session** : mémorise désormais la version quittée (secret `version_precedente`) pour que le rollback ait quelque chose à restaurer.
- [x] **`rollback` → `@executeur("service_manage.rollback")` — corrigé cette session.** Restaure réellement la version mémorisée par la dernière mise à jour ; échoue franchement (422 `non_porte`) si aucune mise à jour n'a jamais eu lieu, plutôt que de prétendre avoir restauré quelque chose. Testé de bout en bout : mise à jour puis rollback, version confirmée revenue à l'originale.
- [x] `GET /services-manages`, `/{id}`, `PATCH`, `DELETE`, `/configuration`, `/metriques`, `/sieges*`, `/sso`, `/versions` — endpoints DB standards, cohérents avec l'absence de backend réel (le module ne prétend piloter aucun service externe, c'est assumé par construction).

## 6. deploiements — CI/CD applicatif (build/scan/provision/deploy, environnements, canary)
Backend : `fournisseur(DepotsSimule, DepotsReel)` (dépôts Git, via `synelia_openstack.plateforme_k8s`).
D'après `capacites` : `argo.application: simule` (« ArgoReel lève `amont_indisponible`, non
implémenté ») et `k8s.workload: simule` (pas de cluster PaaS par défaut). **Statut global : simulé
de bout en bout** — pas de build réel, pas de scan de vulnérabilités réel (le message "Analyse des
vulnérabilités terminée, aucun correctif bloquant" à l'étape 1 de `app.deploy` est **toujours**
renvoyé, sans scanner quoi que ce soit — `service.py:76`), pas de déploiement K8s réel, pas de
canary réel (juste une progression d'état en DB).
- [x] `@executeur("app.deploy")`, `app.rollback`, `environnement.delete` — tous implémentés mais 100% simulation d'état (transitions de statut DB, aucun appel Argo/K8s réel tant que les gates capacites restent simulées).
- [x] `GET /deploiements/*/branches` — lève honnêtement `erreurs.amont_indisponible("github", ...)` si le dépôt n'est pas configurable/accessible plutôt que d'inventer des branches — bon point, comportement honnête plutôt qu'un faux succès.
- [x] Reste des endpoints (approbation, canary, journaux, promotion, annulation, environnements CRUD) — cohérents avec la simulation assumée du module, rien d'anormal trouvé au-delà du gate `argo.application`/`k8s.workload` déjà documenté par `capacites`.

---

## Constats "premier principe" — synthèse

1. **Pricing/showback** : la math de tarification (`tarification.py`, `metrologie.py`) est
   correcte et cohérente avec ce qui est réellement provisionné (VM/volumes/LB OpenStack réels) —
   pas de données hardcodées stale pour le calcul de consommation. **Sauf** `admin/facturation/marges`
   qui est entièrement fictif (voir §3).
2. **Remboursements/annulations** : **n'existent pas du tout** côté Paystack ni côté paiement
   manuel. Gap majeur avant toute mise en production avec de vrais paiements.
3. **Relances impayés** : n'envoient rien (§3) — le compteur `relances` s'incrémente dans le vide.
4. **Réclamation SLA** : crédite sans vérification contre les engagements mesurés (§1) — chemin de
   fraude interne trivial pour quiconque a `invoice.view`.
5. **Deux travaux enregistrés-mais-vides** (`service_manage.export`, `service_manage.rollback`) et
   **un travail totalement non enregistré** (`facturation.export`) : trois façons différentes
   d'obtenir un "succès" en base sans que rien de réel ne se soit produit — pattern répété, pas un
   accident isolé.
6. **Module `applications` vide** : dossier mort ou lacune de développement à clarifier.

## Plan de travail (dans l'ordre)

1. **Sécuriser le paiement** : ajouter un chemin de remboursement/annulation réel (Paystack
   `refund` API existe côté fournisseur) avant tout passage en clés de production. Distinguer
   clairement dans le contrat `POST /factures/{id}/paiement` (règlement manuel) de la voie
   Paystack, ou restreindre son usage à un rôle compta dédié.
2. **Corriger `facturation.export`** : soit implémenter un `@executeur("facturation.export")` réel
   (génération CSV/PDF), soit faire échouer explicitement le travail tant qu'il n'existe pas (même
   traitement que le bug `web.backup.restore`/`granularite` déjà corrigé dans `WEB-CLOUD-INVENTORY.md`
   — ne pas laisser un travail sans exécuteur passer en `done`).
3. **Implémenter ou faire échouer explicitement** `service_manage.export` et
   `service_manage.rollback` (actuellement `pass` silencieux).
4. **Ajouter une vérification métier** à `POST /facturation/sla/reclamations` (comparer contre
   `sla_engagements` avant de créditer).
5. **Implémenter réellement `POST /admin/facturation/impayes/relances`** (envoi email via
   `courriel.envoyer`, sur le modèle de `_notifier_facture_emise`) ou renommer/documenter l'endpoint
   comme un simple compteur interne s'il ne doit pas notifier.
6. **Remplacer `lister_marges_backends`** par un calcul réel (coût infra vs revenu offre/consommation)
   ou marquer clairement l'endpoint comme non implémenté dans le contrat plutôt que renvoyer des zéros.
7. **Clarifier le module `applications`** : supprimer le dossier mort ou implémenter le module prévu.
