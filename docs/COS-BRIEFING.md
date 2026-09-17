# COS briefing — synelia-cloud-backend

Read-this-first map of the Python FastAPI backend that serves the Synelia Cloud portal contract. Companion to `README.md`, `docs/GUIDE-MODULE.md`, and `docs/PLAN-DIRECTEUR-PYTHON.md`.

**Repo:** `jvkassi/synelia-cloud-backend` (uv workspace, Python 3.13 / `.python-version`, `requires-python >= 3.12`).  
**Contract:** OpenAPI 3.0.3 — **514 operations**, **364 paths**, **218 schemas**, **40 tags**.  
**Portal:** [synelia-cloud](https://github.com/jvkassi/synelia-cloud) (Next.js). This backend is the Python variant of the director plan (no Blesta; OpenStack lab as production IaaS).

Battery on `master` (`ddfbd4b`) is at the end of this file.

---

## 1. Architecture

### 1.1 Shape

Modular monolith, three CLI roles from one image:

| Process | Command | Role |
|---|---|---|
| API | `synelia api` | FastAPI / uvicorn, default port **4000**, prefix `/v1` |
| Worker | `synelia worker` | Temporal worker, queue `synelia-travaux` |
| Scheduler | `synelia scheduler` | Idempotent Temporal Schedules (billing, ACME, reconciliation…) |

Entry points: `apps/synelia/synelia/__main__.py` (Typer), `asgi.py` / `creer_app()`, Vercel `api/index.py`.

```
portal (Next.js) ──HTTP /v1──► FastAPI
                                 ├─ modules/* (one package per contract domain)
                                 ├─ Depot[T] ──► SQLAlchemy ressources + identity/audit/travaux
                                 ├─ travaux moteur ──► local inline | Temporal
                                 └─ synelia_openstack.fournisseur() ──► Simule | OpenStack/httpx
```

### 1.2 Workspace packages

| Package | Path | Responsibility |
|---|---|---|
| `synelia` | `apps/synelia` | App: routers, deps, depot, travaux, auth crypto |
| `synelia-kernel` | `packages/kernel` | Config (`SYNELIA_*`), contract errors, UUID v7, FCFA integers, AES-GCM secrets, structlog |
| `synelia-contract` | `packages/contract` | Copied `openapi.json`, generated `modeles.py` / `operations.py`, `rbac.json`, `workflows.json` |
| `synelia-db` | `packages/db` | SQLAlchemy 2 asyncio, models, RLS hooks, `create_all` |
| `synelia-catalogue` | `packages/catalogue` | 13 managed-service config sheets (`configurations.json`) |
| `synelia-openstack` | `packages/openstack` | Simule/real pairs; **no** knowledge of DB or contract |
| `synelia-testing` | `packages/testing` | pytest plugin: ephemeral SQLite, authenticated `ClientApi` |

Root `pyproject.toml` wires the uv workspace. Dev group: ruff, pyright, pytest, schemathesis, hypothesis, respx, datamodel-code-generator. App extras (not in default sync): `temporal`, `openstack`, `pdf`, `paiements`.

### 1.3 `apps/synelia` modules

Routers are **auto-discovered** (`pkgutil` in `creer_app()`): a package under `synelia/modules/` that exports `router` or `routers` is mounted under `/v1`. Do not edit `app.py` to add a domain.

| Package | HTTP prefixes (under `/v1`) |
|---|---|
| `auth` | `/auth` |
| `compte` | `/moi` |
| `organisations` | `/organisations`, `/utilisateurs` |
| `membres` | `/membres`, `/invitations` |
| `securite` | `/securite` |
| `audit` | `/audit` |
| `tableau_de_bord` | `/tableau-de-bord`, `/copilote` |
| `travaux` | `/travaux` |
| `espaces` | `/espaces` (reference module — see `GUIDE-MODULE.md`) |
| `vms` | `/vms` |
| `catalogue` | `/catalogue` (VM flavors/images) |
| `kubernetes` | `/kubernetes` |
| `reseau` | `/reseaux`, `/ips`, `/groupes-securite`, `/load-balancers`, `/vpn` |
| `stockage` | `/volumes`, `/cles-s3`, `/buckets` |
| `bases` | `/bases` |
| `sauvegarde`, `pra` | `/sauvegarde`, `/pra` |
| `applications` | Applications PaaS (mixed prefixes) |
| `deploiements` | `/deploiements` |
| `projets` | `/projets`, `/domaines-applicatifs`, `/zone-applicative`, `/routage` |
| `modeles` | `/modeles` |
| `observabilite` | `/observabilite` |
| `services_manages` | `/services`, `/catalogue` (managed SaaS) |
| `web_domaines`, `web_dns` | `/web/domaines`, `/web/dns` |
| `web_hebergement` | `/web/hebergements`, `/web/sites`, `/web/bases` |
| `web_emails`, `web_drive`, `web_ssl`, `web_backup`, `web_smtp` | `/web/{emails,drive,ssl,backup,smtp}` |
| `facturation` | `/facturation` |
| `support` | `/support` |
| `docs` | `/docs` |
| `admin`, `admin_catalogue` | `/admin/**` |
| `public` | `/public` |
| `conformite` | mixed (dashboard / audit tags) |
| `transverses` | RBAC matrix, référentiels, onboarding, search |

Shared layers:

- `deps/` — `Contexte` (session + principal + org), `exige` / `exige_admin` (RBAC), pagination, destructive `confirmation`, in-memory rate limit, correlation id.
- `depot.py` — `Depot[T]` over table `ressources`, typed by generated Pydantic models.
- `securite.py` — argon2id passwords, EdDSA JWT, TOTP, JWKS.
- `erreurs_http.py` — all failures as `{ erreur: { code, message, correlationId } }`.
- `amorcage.py` / `demo.py` — seed admin + demo org (`admin@synelia.cloud` / `Synelia!2026`).

### 1.4 Contract package

- Source of truth is the **frontend** OpenAPI. Sync: `uv run tools/contrat_sync.py ../synelia-cloud` or `synelia contrat sync`.
- `operations.py` is generated: method, path, `nom_python`, RBAC action, success code, body/response model names.
- Coverage tool: `uv run python tools/contrat_diff.py [--strict]` — **served paths ⊇ contract** (CI `--strict`).
- RBAC: **38 actions × 10 roles** (`rbac.json`). Nine `ia.*` actions exist in the matrix but have **no OpenAPI paths** (IA & Agents is out of contract until the portal extends it).
- Workflows: **41** catalog ids in `workflows.json`. Modules also register extra `@executeur("…")` types that fall back to generic 3-step simulation when not in the catalog.

### 1.5 Database (`packages/db`)

[ADR 0001](ADR/0001-persistance-depot-typee.md): dedicated tables for identity / audit / jobs; everything else in typed `ressources`.

| Table | Notes |
|---|---|
| `organisations`, `utilisateurs`, `memberships`, `invitations`, `sessions_auth`, `cles_api` | Tenancy, argon2, rotating refresh, API keys with scope |
| `audit` | Append-only, chained hash |
| `travaux` | Projection of provisioning jobs (`id` = workflow id) |
| `ressources` | `type` + `org_id` + JSON `donnees` + encrypted `secrets`; platform rows have `org_id` NULL |

Engine: SQLAlchemy 2 asyncio. SQLite (`aiosqlite`) without `SYNELIA_DATABASE_URL` (or Vercel `/tmp`). Postgres 18 + `asyncpg` when URL is set. **RLS** (`SET LOCAL app.org_id`) on tenant tables for Postgres only.

Schema: `initialiser_schema()` → `Base.metadata.create_all`. Alembic is a **dependency** (`synelia-db`) but **there is no `alembic/` tree**. Production Postgres is supposed to treat Alembic as source of truth; that pipeline is not in the repo yet.

### 1.6 OpenStack connectors (`packages/openstack`)

`fournisseur(Simule, Reel)` picks implementation when `SYNELIA_FOURNISSEUR=openstack` **and** `os_cloud` or `os_auth_url` is set; otherwise **simule**. Real OpenStack uses extra `openstacksdk` via lazy `connexion()`.

| File | Domain |
|---|---|
| `identite.py` | Keystone projects / application credentials |
| `compute.py` | Nova + Glance (flavors, images, servers) |
| `network.py` | Neutron / Octavia-shaped APIs |
| `block_storage.py` | Cinder |
| `objet.py` | Object store / RGW |
| `magnum.py` | Kubernetes clusters |
| `trove.py` | Managed DBs |
| `designate.py` | DNS |
| `backup.py` | Backup jobs |
| `placement.py` | Placement / capacity |
| `plesk.py`, `registrar.py` | Shared hosting / domains |
| `stalwart.py`, `postal.py`, `nextcloud.py`, `acme.py` | Mail, SMTP relay, drive, certificates |
| `victoria.py` | Metrics/logs links |
| `plateforme_k8s.py` | Argo + image registries |
| `connecteurs_services.py` | Generic managed-service provision (httpx if `SYNELIA_SERVICES_BASE_URL`) |

Mappers live in **modules**, not in this package. Tests never hit a real cloud.

### 1.7 Temporal and jobs

`synelia.travaux.moteur`:

- Default **local** engine. On Vercel (`VERCEL=1`) or tests (`SYNELIA_TRAVAUX_EN_LIGNE` / `SYNELIA_ENV=test`), steps run **inline** before the `202` so the payload already has a terminal status.
- If `SYNELIA_TEMPORAL_ADRESSE` is set, `temporal.lancer()` starts workflow `TravailWorkflow` on queue `synelia-travaux`. Worker extra: `uv sync --extra temporal`.
- `@executeur("type")` classes implement `etape` / `terminer` / optional compensation.

Scheduler (`planification.py`) declares cron schedules that start **`TravailPlanifieWorkflow`**. The worker currently registers **`TravailWorkflow` only** — scheduled types (`facturation.cycle`, `web.ssl.renew`, …) are not wired as a distinct workflow.

Compose: Temporal `1.28` auto-setup on the same Postgres, UI on **8233**.

Valkey 8 is in compose and `SYNELIA_VALKEY_URL`; rate limiting is still an **in-process token bucket** (`deps/limitation.py`, “Valkey later”).

---

## 2. Deploy

### 2.1 Local

```bash
uv sync
uv run synelia api --rechargement    # http://localhost:4000/v1 · docs /v1/docs
uv run pytest -q
uv run python tools/contrat_diff.py --strict
docker compose up -d                 # postgres 18, valkey 8, temporal+ui, mailpit, api, worker
```

Without `SYNELIA_DATABASE_URL`: SQLite file `./synelia.sqlite3`. Seed: `SYNELIA_SEED_*` (see `.env.example`).

### 2.2 Vercel (preview / API-only)

[ADR 0002](ADR/0002-vercel-et-travaux-en-ligne.md) + runbook [`docs/runbooks/deploiement-vercel-github.md`](runbooks/deploiement-vercel-github.md).

- `vercel.json`: rewrite all traffic to `api/index.py` (1024 MB, 60 s). `installCommand`: `uv sync --no-dev`. Env `VERCEL=1`, `SYNELIA_ENV=preview`.
- SQLite in `/tmp` unless Neon (or similar) `SYNELIA_DATABASE_URL`.
- Simulated upstream; no Temporal worker; jobs complete in the request.
- Git integration (recommended) or Actions `.github/workflows/vercel.yml` (`VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`). Native GitHub workflow is gated on the token being non-empty.
- Point the portal at `NEXT_PUBLIC_API_URL=https://<preview>/v1`.

### 2.3 Docker / Dokploy / Kubernetes (production near the lab)

Single `Dockerfile`: `python:3.13-slim`, `uv sync --frozen --no-dev --extra temporal`, non-root `synelia`, `ENTRYPOINT ["synelia"]`, `CMD ["api"]`. Worker: `command: ["worker"]`.

### 2.4 CI (`.github/workflows/ci.yml`)

On **push to `main`** and **all pull requests**:

1. `uv sync --frozen`
2. `uv run ruff check .`
3. `uv run ruff format --check .`
4. `uv run pytest -q`
5. `uv run python tools/contrat_diff.py --strict`
6. `docker build -t synelia-cloud-backend:ci .`

**Not in CI (but in the Python plan):** pyright, schemathesis, oasdiff. Default git branch in this clone is **`master`**; the workflow `push` filter is **`main` only**, so pushes to `master` do not run CI unless they are PRs.

---

## 3. Runbooks (short)

### Add a contract module

Follow `docs/GUIDE-MODULE.md`. Copy `modules/espaces/`. Name route functions `nom_python`. Persist with `Depot`. Mutations: `journaliser` + RBAC `exige`. Long ops: `demarrer_travail`. Upstream: Simule/OpenStack pair. Check: ruff + pytest on the module, then `contrat_diff` for the tag.

### Sync the contract from the portal

Need a sibling clone of `synelia-cloud`. `uv run tools/contrat_sync.py ../synelia-cloud` copies OpenAPI, RBAC, workflows, catalogue JSON and regenerates Pydantic models.

### Switch simulated → real OpenStack

Set `SYNELIA_FOURNISSEUR=openstack` plus `SYNELIA_OS_AUTH_URL` and application credential id/secret (or `SYNELIA_OS_CLOUD`). Run **near the lab** (Docker/K8s), not on Vercel. Install extra `openstack`.

### Enable Temporal

Set `SYNELIA_TEMPORAL_ADRESSE`. Image already includes the extra. Run `synelia worker` and optionally `synelia scheduler`. Until `TravailPlanifieWorkflow` exists, treat scheduler as incomplete.

### Auth smoke

```bash
curl -s http://localhost:4000/healthz
curl -s -X POST http://localhost:4000/v1/auth/connexion \
  -H 'content-type: application/json' \
  -d '{"email":"admin@synelia.cloud","motDePasse":"Synelia!2026"}'
```

---

## 4. Gaps vs the director plan

Intentional or current deltas (not a commitment to fix in this PR):

| Area | Plan | Repo today |
|---|---|---|
| Contract coverage | 514 ops | **514/514 served** (`contrat_diff --strict`) |
| Persistence | Alembic migrations | `create_all` only; Alembic unused |
| Jobs | Temporal in prod, inline on Vercel | Local + inline work; Temporal client/worker present; **scheduler workflow name mismatch** |
| Valkey | Shared rate limit / locks / cache | Compose + setting; limiter is memory-only |
| Identity federation | Authlib OIDC/SAML both ways | Local argon2 + TOTP + JWT; SSO fields stored; no Authlib/python3-saml/ldap3 |
| Observability | OTel + Prometheus → Victoria | structlog + Victoria **link** connector; no `/metrics` instrumentation |
| Contract tests | Schemathesis + oasdiff in CI | schemathesis in **dev deps only**; coverage is path presence, not response property tests |
| Types | pyright strict in CI | pyright in pyproject, **not** a CI step; mode `basic` |
| Lint | ruff green | **ruff check + format fail on `master`** (see battery) |
| Docker extra | temporal in image | yes (`--extra temporal`); openstacksdk **not** in the image |
| IA domain | last phase, needs frontend contract | 9 `ia.*` RBAC actions, zero paths |
| Tests | testcontainers Postgres/Temporal | SQLite per test + inline jobs; no live OpenStack |
| CI branch | `main` | clone default **`master`**; `main` also exists on origin |
| Payments / PDF | Stripe extra, WeasyPrint extra | extras declared; not in default/Vercel install |
| Filter scale | JSONB indexes when lists grow | in-memory filter/sort per org (ADR 0001 ceiling ~10⁴ rows) |

Simulation quality: many `@executeur` implementations succeed after a sleep (or instantly) and then mark the resource `running`. That is enough for portal previews; it is **not** production provisioning.

---

## 5. Test battery

Environment: Cloud Agent VM, 2026-09-17. Installed `uv 0.12.15`, CPython **3.13.15** via `uv python install`, then `uv sync` (lock already resolved, 93 packages, **no extras**). No application code changes. Docker image job **not** run (heavy; CI-only).

| Step | Command | Result |
|---|---|---|
| Sync | `uv sync` | **PASS** |
| Lint | `uv run ruff check .` (CI) | **FAIL** — 21 errors (UP046, PLR0917×9, PLW0127, E741×3, E731, PLR1714, S110, E402×3, RUF046, UP047). No autofix without `--unsafe-fixes`. |
| Format | `uv run ruff format --check .` (CI) | **FAIL** — would reformat `api/index.py` and a code fence in `docs/GUIDE-MODULE.md`. 238 files already formatted. |
| Tests | `uv run pytest -q` | **PASS** — **201 passed**, 845 warnings, **348 s**. Warnings: FastAPI `ORJSONResponse` deprecation (per-request noise). |
| Contract | `uv run python tools/contrat_diff.py --strict` | **PASS** — **514/514 (100 %)** all 40 tags. |
| Docker | `docker build` (CI) | **NOT RUN** |

Pytest collection: 201 tests under `apps/synelia` (module `tests/` plus `synelia/tests/test_socle.py` and `test_espaces.py`). Harness: `conftest.py` loads `synelia_testing`; each `client` fixture gets a fresh SQLite file, schema + seed, admin session, inline jobs.

**Implication:** a PR against this repo that only adds this file will still go **red on GitHub Actions** because ruff is already red on `master`. Fixing ruff is out of scope for this briefing-only change.

### contrat_diff tag table (this run)

```
✓   5/5   Super admin — pilotage
✓  11/11  Super admin — infrastructure
✓  15/15  Super admin — produit
✓  18/18  Super admin — exploitation
✓   6/6   Super admin — finance
✓   2/2   Super admin — clients
✓   9/9   Tableau de bord
✓  23/23  Applications
✓   6/6   Audit
✓  11/11  Authentification
✓  10/10  Bases managées
✓  18/18  Stockage
✓  24/24  Services managés
✓  21/21  Machines virtuelles
✓   8/8   Déploiements
✓   8/8   Documentation & formation
✓  29/29  Projets applicatifs
✓   8/8   Espaces Cloud
✓  20/20  Facturation
✓  37/37  Réseau
✓  11/11  Membres & rôles
✓  13/13  Kubernetes
✓   3/3   Modèles applicatifs
✓  11/11  Compte & organisation active
✓   9/9   Observabilité
✓   8/8   Organisations
✓  21/21  Sauvegarde & PRA
✓  18/18  Vitrine publique
✓  14/14  Sécurité & accès
✓   9/9   Support
✓   4/4   Travaux de provisioning
✓   6/6   Web Cloud — sauvegarde
✓  10/10  Web Cloud — bases
✓  19/19  Web Cloud — domaines & DNS
✓   7/7   Web Cloud — drive
✓  10/10  Web Cloud — emails
✓  20/20  Web Cloud — hébergement
✓  10/10  Web Cloud — applications web
✓  14/14  Web Cloud — relais SMTP
✓   8/8   Web Cloud — SSL
Couverture : 514/514 opérations (100 %)
```

---

## 6. Suggested next engineering (not this PR)

1. Make ruff check/format match CI on `master` (or pin/ignore the new rules if the bump was unintentional).
2. Add Alembic migrations before any production Postgres.
3. Implement `TravailPlanifieWorkflow` or point schedules at `TravailWorkflow`.
4. Put Schemathesis (or equivalent) on a subset of tags; path coverage is already complete.
5. Align CI `push` branches with the actual default (`master` vs `main`).
