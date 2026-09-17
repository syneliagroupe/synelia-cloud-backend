# synelia-cloud-backend

Backend Python de [synelia-cloud](https://github.com/jvkassi/synelia-cloud), le portail Next.js de
Synelia Cloud. Il sert **le contrat `openapi.json` du portail** (514 opérations, 364 chemins, 218 schémas)
sans Blesta, sur l'OpenStack de Synelia — variante Python du plan directeur
([`docs/PLAN-DIRECTEUR-PYTHON.md`](docs/PLAN-DIRECTEUR-PYTHON.md)).

## Démarrer

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv + Python 3.13
uv sync
uv run synelia api --rechargement                         # http://localhost:4000/v1 · docs : /v1/docs
uv run pytest -q                                          # tests
uv run python tools/contrat_diff.py                       # couverture du contrat (servie ⊇ contrat)
```

Compte d'amorçage : `admin@synelia.cloud` / `Synelia!2026` (variables `SYNELIA_SEED_*`). Sans
`SYNELIA_DATABASE_URL`, SQLite local ; avec Postgres 18 (`docker compose up -d postgres`), RLS par organisation.

```bash
docker compose up -d            # postgres 18, valkey 8, temporal + ui (8233), mailpit (8025), api (4000), worker
```

## Architecture (résumé)

- `apps/synelia` — l'application, trois rôles : `synelia api | worker | scheduler`. Un module par domaine du
  contrat dans `synelia/modules/` (routeur découvert automatiquement), dépendances `deps/` (contexte, RBAC,
  pagination, confirmation), moteur des travaux `travaux/` (local en ligne ou Temporal).
- `packages/contract` — `openapi.json` copié du frontend, `modeles.py` **généré** (datamodel-codegen),
  `operations.py` (index), `rbac.json` (38 actions × 10 rôles), `workflows.json` (41 travaux).
  Re-synchroniser : `uv run tools/contrat_sync.py ../synelia-cloud`.
- `packages/db` — SQLAlchemy 2 asyncio ; tables identité/audit/travaux dédiées, `ressources` typée par le
  contrat pour le reste ([ADR 0001](docs/ADR/0001-persistance-depot-typee.md)).
- `packages/openstack` — paires `Simule` / `OpenStack` (openstacksdk) par domaine ; connecteurs amont.
- `packages/kernel` — erreurs du contrat, configuration, UUID v7, argent (FCFA entiers), chiffrement, journal.

## Déploiement

- **Vercel** (préversions par branche, API seule, amont simulé) : `api/index.py` + `vercel.json` ;
  voir [`docs/runbooks/deploiement-vercel-github.md`](docs/runbooks/deploiement-vercel-github.md) et
  [ADR 0002](docs/ADR/0002-vercel-et-travaux-en-ligne.md).
- **Docker / Dokploy / Kubernetes** (production, près du lab) : `Dockerfile` unique, `docker-compose.yml`.
- **CI** : `.github/workflows/ci.yml` (ruff, pytest, couverture du contrat, image) ; `vercel.yml` (déploiement
  par Actions si l'intégration Git native n'est pas utilisée).

## État réel (2026-09-07)

Tout l'univers Infrastructure provisionne de vraies ressources OpenStack (`SYNELIA_FOURNISSEUR=openstack`) :
VM/Espaces/Kubernetes/LB/Réseau/Volumes/Bases/Buckets, Web Cloud (DNS Designate, SSL, hébergements réels avec
Docker+Traefik, messagerie Zimbra, relais SMTP réel) et le PaaS (`projets` cible `k8s` sur Magnum, cible `vm`
sur une VM dédiée). La facturation produit de vraies factures et les notifie par email. Restent simulés par
décision : `services_manages` (phase 7 non construite), le registrar de domaines (pas de partenaire TLD), et
le pipeline `/applications`+`/deploiements`. Sauvegardes/PRA échouent honnêtement (Karbor absent du lab).
Détails et historique : [`docs/runbooks/lab-openstack.md`](docs/runbooks/lab-openstack.md).

## Écrire un module

[`docs/GUIDE-MODULE.md`](docs/GUIDE-MODULE.md). Module de référence : `modules/espaces/`. Sa section
« Invariants durs » est obligatoire : `asyncio.to_thread` sur tout appel SDK synchrone (sinon gel de l'API
pour tous les tenants), statuts écrits limités aux Literals du contrat, réconciliation à la lecture.
