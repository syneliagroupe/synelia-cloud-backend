# Plan d'exécution — suite de la revue d'architecture

Date : 2026-09-08. Auteur : Fable 5.1 (suite de `ARCHITECTURE-REVIEW-FABLE.md`, décisions #4, #6, #9).
Public : agents de codage (sonnet-5) qui exécutent ce plan tel quel. Chaque section = une décision prise,
puis des étapes ordonnées avec fichiers, changements, vérifications, et — pour tout ce qui touche le
Postgres réel de dev01 — un garde-fou et un retour arrière explicites.

## État constaté aujourd'hui (à re-vérifier avant de commencer, en lecture seule)

- dev01 : conteneur `synelia-backend-dev01-api-1`, **un seul processus** (`synelia api` → `uvicorn.run("synelia.asgi:app")`,
  sans `workers`). Hôte : 12 cœurs, 128 Go (114 utilisés par le lab), API 225 Mio, Postgres 18.6, `max_connections=100`,
  7 connexions ouvertes (6 `synelia_app`, 2 `synelia`). Pool SQLAlchemy : `pool_size=5` (+ `max_overflow` par défaut 10)
  **par processus**.
- Démarrage (`apps/synelia/synelia/app.py::_vie`) : `initialiser_schema()` (create_all + DDL RLS `ALTER TABLE ENABLE/FORCE`,
  `DROP POLICY IF EXISTS`, `CREATE POLICY` sur 7 tables, **à chaque boot**) puis `amorcage.amorcer()` (check-then-insert
  admin, `semer_catalogue_reel` check-then-insert par id, `semer_zone_vps` get-then-update). Aucun verrou. Le processus
  `synelia relais-smtp` (`relais_smtp.py::demarrer`) appelle **aussi** `initialiser_schema()` à son boot : deux processus
  concurrents existent déjà en pratique.
- Travaux : `moteur.demarrer_travail` → `asyncio.create_task(_executer_detache)` dans le processus API. **Sur dev01, 12 lignes
  `travaux` en `queued` et 1 en `running` datent des redéploiements du 2026-09-07** : tout job en vol est perdu à chaque
  `docker compose up -d api`. C'est le fait décisif de la section 1. Attention : parmi ces lignes figurent un `vm.delete`
  et neuf `vm.resize` — **un worker qui les reprendrait aveuglément exécuterait des opérations périmées.**
- Schéma : 9 tables, toutes possédées par `synelia_app` ; le schéma vivant est **identique** à `Base.metadata` (colonnes,
  nullabilité, index — vérifié table par table, y compris `ressources.cree_par_id` posé à la main dans `d5006a4`).
  `alembic>=1.14` est déclaré dans `packages/db/pyproject.toml` (1.19.1 verrouillé) mais **aucun** répertoire, script ni
  table `alembic_version` n'existe. Tests et Vercel tournent sur SQLite via `create_all` dans le lifespan.
- Audit : 1 618 lignes, 19 orgs, aucune ligne sans `hash`. `synelia_app` **possède** la table et détient
  `INSERT, SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER`. Aucun trigger (contrairement à `PLAN-DIRECTEUR.md` §410).
  Exports d'audit déposés dans le MinIO du même hôte, mêmes identifiants applicatifs — pas hors-boîte.
- Rappel opérationnel (mémoire `dev01-backend-hosting`) : toute commande compose dev01 doit porter
  `--env-file /home/jekas/.config/synelia/backend-dev01.env -f docker-compose.dev01.yml`. Postgres hors-ligne :
  `docker exec -it synelia-backend-dev01-postgres-1 psql -U synelia -d synelia` (superutilisateur, jamais utilisé par l'appli).

Ordre global : **§1 → §2 → §3**. §1(a) (verrou + DDL RLS idempotente) est un prérequis de §3 (transfert de propriété de
`audit`) ; §3 (ancrage périodique) s'appuie sur le processus worker de §1(c).

---

## 1. Isolation de l'exécution des travaux hors du processus API

### Décision

1. **(a) Démarrage multi-processus sûr = `pg_advisory_xact_lock` autour de `initialiser_schema()` et de `amorcer()`, plus
   une DDL RLS qui ne s'exécute que si elle manque.** Pas Alembic (rejeté en §2), pas d'étape one-shot séparée : le lifespan
   doit de toute façon continuer à créer le schéma pour SQLite (tests, Vercel), donc la seule solution qui ne crée pas un
   troisième chemin est de rendre le chemin existant sérialisable. Coût : ~25 lignes dans deux fichiers. Cela protège aussi,
   gratuitement, la course déjà présente API ↔ relais-smtp ↔ futur worker.
2. **(c) Oui, l'exécution des travaux sort du processus API — un `synelia worker` en moteur Local, poll de la table `travaux`.**
   Ce n'est plus seulement une question de blast-radius : les 13 lignes orphelines de dev01 prouvent que le
   `create_task` en processus **perd des travaux à chaque redéploiement**, et `--workers N` n'y change rien (chaque worker
   uvicorn perd ses propres tâches, et la perte se multiplie par N). Le code nécessaire existe à 80 % :
   `worker_ctx.executer_depuis_worker` (chemin Temporal) rejoue déjà un travail hors requête. Reste ~120 lignes : une boucle
   de réclamation atomique, une reprise au boot, un service compose.
3. **(b) `--workers 2` sur l'API, en dernier, une fois (c) en place.** Sa valeur résiduelle : un `to_thread` oublié dans un
   *gestionnaire de requête* (pas un job) ne gèle plus que la moitié de la capacité ; le superviseur uvicorn relance un worker
   mort sans redémarrage de conteneur. Il est volontairement **après** (c) : avant, il multiplierait les orphelins et
   relâcherait les limiteurs de débit en mémoire par N sans gain sur le vrai problème. Deux, pas plus : 2×15 + 15 (worker)
   + 15 (relais) = 60 connexions Postgres potentielles sur 97 disponibles.

Ce qui ne change pas : `SYNELIA_TRAVAUX_EN_LIGNE` (tests, Vercel) et le moteur Temporal gardent la priorité qu'ils ont
aujourd'hui dans `demarrer_travail`. `create_task` reste le repli par défaut d'un `synelia api` lancé seul (dev local),
pour ne rien casser hors dev01.

### Étapes

**Étape 1.1 — Verrou consultatif dans `initialiser_schema` + DDL RLS idempotente** (`packages/db/synelia_db/session.py`,
`packages/db/synelia_db/rls.py`).

- `rls.py` : ajouter `async def politiques_manquantes(conn) -> list[str]` qui lit en une requête, pour `TABLES_TENANT`,
  `pg_class.relrowsecurity`, `pg_class.relforcerowsecurity` et l'existence de `pg_policies.policyname = f"{table}_org"`,
  et ne renvoie que les DDL nécessaires (ENABLE si `relrowsecurity` faux, FORCE si `relforcerowsecurity` faux,
  `CREATE POLICY` si absente — **plus de `DROP POLICY` systématique**). Garder `sql_politiques()` (utilisé nulle part
  ailleurs ? vérifier par grep ; si oui le faire déléguer à la version filtrée). Documenter dans la docstring : « changer le
  texte d'une politique = la supprimer à la main (superutilisateur) puis redémarrer ».
- `session.py::initialiser_schema` : dans le `async with eng.begin() as conn`, **avant** `create_all`, si `r.est_postgres` :
  `await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLE_VERROU_SCHEMA})` avec
  `CLE_VERROU_SCHEMA = 7_130_001` en constante de module commentée. Le verrou est transactionnel : il couvre `create_all`
  et la DDL RLS et se relâche au commit. Remplacer la boucle `sql_politiques()` par `politiques_manquantes(conn)`.
- Vérifier : `uv run pytest -q packages apps/synelia/synelia/tests` (SQLite : le verrou est sauté). Puis, sur dev01
  **après** déploiement (étape 1.6), deux boots consécutifs sans erreur et `select count(*) from pg_policies` = 7 inchangé.

**Étape 1.2 — Verrou dans `amorcer`** (`apps/synelia/synelia/amorcage.py`).

- Au début du `async with fabrique()() as s:`, si `reglages().est_postgres` :
  `await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLE_VERROU_AMORCAGE})` (`7_130_002`, exporté par
  `session.py`). Puis **remplacer les trois `await s.commit()` intermédiaires par `await s.flush()`** et garder un seul
  `commit()` final : un `pg_advisory_xact_lock` est relâché au premier commit, donc l'amorçage doit être une seule
  transaction. Personne ne consomme ces lignes pendant le boot ; aucune perte de comportement.
- Exception connue et acceptée : la branche `espaces.service._provisionner_zone_vps` (jamais exécutée sur dev01, la ligne
  existe) commet à chaque étape via `demarrer_travail` en ligne. Documenter en commentaire dans `amorcer` : « sur un
  environnement neuf, lancer `synelia amorcer` une fois **avant** de démarrer plusieurs processus ». Ne pas réécrire
  cette branche.
- Vérifier : suite de tests complète (`uv run pytest -q`) — la fixture `client` passe par ce lifespan sur SQLite.

**Étape 1.3 — Mode « worker » côté API** (`apps/synelia/synelia/travaux/moteur.py`).

- Ajouter `def _mode_worker() -> bool` qui lit `os.environ.get("SYNELIA_TRAVAUX_WORKER", "").lower() in {"1","true","oui"}`
  — même style que `_en_ligne()` (variable lue à l'appel, pour que les tests puissent la basculer par `monkeypatch.setenv`).
- Dans `demarrer_travail`, `relancer`, `reprendre_apres_pause` : après le test Temporal et le test `_en_ligne()`, insérer
  `elif _mode_worker(): travail.statut = "queued"; await ctx.session.commit(); return …` **au lieu** de `create_task`.
  Pour `reprendre_apres_pause`, retirer en plus la clé `attente` de `travail.contexte` (réassigner un nouveau dict sans
  elle — la colonne est JSON, une mutation en place n'est pas détectée) : sa présence signifie « en pause » pour le worker
  (1.4) et doit disparaître dès que l'humain a tranché. Laisser la tâche courante en `running` : le worker recalcule le
  point de reprise (règle 1.4).
- Dans `demarrer_travail` uniquement, en mode worker, sérialiser le principal dans
  `travail.contexte["principal"] = {utilisateur_id, email, nom, org_id, role, equipe, role_equipe}` (aucun secret) pour que le
  worker rejoue le travail **avec le même acteur** que le `create_task` d'aujourd'hui (les lignes d'audit écrites par les
  exécuteurs portent l'e-mail de l'utilisateur, pas `worker`). `contexte` n'est pas exposé par `vers_contrat` — vérifier.
- Le chemin `create_task` existant reste inchangé (défaut hors dev01).

**Étape 1.4 — Le worker Local** (nouveau `apps/synelia/synelia/travaux/local.py`, ~120 lignes ; `worker_ctx.py` ;
`__main__.py`).

- `worker_ctx.py` : extraire `def contexte_travail(session, travail) -> Contexte` qui construit la fausse `Request` et le
  `Principal` — depuis `travail.contexte.get("principal")` s'il existe, sinon le principal synthétique actuel. Faire utiliser
  ce helper par `executer_depuis_worker` (chemin Temporal) pour n'avoir qu'une construction.
- `local.py` :
  - `async def reprendre_orphelins(session)` : au boot, toute ligne `statut='running'` **sans** `contexte["attente"]` est un
    travail interrompu (il n'y a qu'un worker ; s'il redémarre, rien ne tourne) → `statut='failed'`, tâche `running` →
    `failed` avec message « Interrompu par un redémarrage du worker », `erreur.suggestion` « Relancez : la reprise repart de
    l'étape échouée », `termine_le`. Ne **jamais** relancer automatiquement (un `vm.resize` à moitié fait n'est pas
    idempotent). Filtrer en Python (colonne `json`, pas `jsonb` ; quelques lignes au plus).
  - `async def reclamer(session, n) -> list[str]` : `select(Travail.id).where(statut=='queued').order_by(started_at).limit(n)`
    puis, par id, `update(Travail).where(id==…, statut=='queued').values(statut='running').returning(Travail.id)` — la
    réclamation conditionnelle est atomique même sans `SKIP LOCKED` (SQLite la supporte aussi, pour le test).
  - `async def executer_un(travail_id)` : session dédiée (`fabrique()()`), `rls.org_id_transaction.set(travail.org_id or "")`
    **dans la tâche** (reproduit le contexte RLS d'une requête, que `create_task` héritait via contextvars),
    `depuis = next((i for i,t in enumerate(taches) if t["statut"] != "ok"), 0)` (couvre : neuf → 0, relance → première
    tâche remise `pending`, reprise après pause → la tâche restée `running`), puis `await moteur._executer(ctx, travail, depuis)`
    et `commit()`. `PauseHumaine` est déjà gérée dans `_executer` (retour sans changer le statut) — ne rien ajouter.
  - `async def boucle(arret: asyncio.Event, concurrence=8, intervalle_s=1.0)` : sémaphore `concurrence`, réclame
    `slots_libres` ids, `create_task(executer_un)` par id, suit les tâches dans un `set`, dort `intervalle_s`. Sur `arret` :
    cesse de réclamer, `await asyncio.wait(en_cours, timeout=55)`. Une latence de 1 s est invisible face aux `dureeS` de l'UI.
  - `def demarrer()` : `routeurs_modules()` (importe les modules → enregistre `_EXECUTEURS`, exactement comme `worker` Temporal),
    `await initialiser_schema()` (sûr grâce à 1.1 ; **pas** `amorcer()`), `reprendre_orphelins`, handlers `SIGTERM`/`SIGINT` →
    `arret.set()`, `boucle()`, `fermer()`.
- `__main__.py::worker` : si `reglages().temporal_adresse` → chemin Temporal existant, sinon → `local.demarrer()`. Docstring :
  « Worker des travaux : Temporal si `SYNELIA_TEMPORAL_ADRESSE`, sinon moteur Local (poll de `travaux`) ». Pas de nouveau nom
  de commande : même façade, deux moteurs, comme le module `moteur.py` le promet déjà.
- Vérifications avant de passer à 1.5 :
  - Grep obligatoires, corriger si trouvé : `ctx.request`/`ctx.entete(` dans un exécuteur (la fausse requête n'a pas
    d'en-têtes) ; `est_admin_plateforme` dans un exécuteur (le principal rejoué le porte via `equipe`/`role_equipe`, vérifier
    que c'est sérialisé) ; `demarrer_travail(` appelé **depuis** un exécuteur (un sous-travail en mode worker part en file et
    s'exécute concurremment — vérifier qu'aucun parent n'en attend la fin par polling ; si un tel cas existe, le documenter
    dans ce plan avant de continuer).
  - Nouveau test `apps/synelia/synelia/tests/test_travaux_worker.py` : avec la fixture `client`, `monkeypatch.setenv(
    "SYNELIA_TRAVAUX_EN_LIGNE", "0")` et `"SYNELIA_TRAVAUX_WORKER", "1"` ; POST une création (ex. `/v1/vms`) → `202`,
    `statut == "queued"` ; `await local.reprendre_orphelins(...)` ne touche rien ; `ids = await local.reclamer(session, 10)`
    → 1 id ; `await local.executer_un(id)` → `GET /v1/travaux/{id}` renvoie `done` et la ligne d'audit de l'exécuteur porte
    l'e-mail admin (pas `worker`). Second test : une ligne `running` sans `attente` insérée à la main → `reprendre_orphelins`
    la passe `failed` ; une ligne `running` **avec** `attente` reste intacte.
  - `uv run ruff check --fix` + `uv run pytest -q` complets.

**Étape 1.5 — Compose dev01** (`docker-compose.dev01.yml`).

- Factoriser le bloc `environment` de `api` en ancre YAML `x-env-backend: &env-backend` (compose-go supporte `x-*` et
  `<<:`), pour que le worker reçoive **exactement** les mêmes variables (OpenStack, MinIO, Zimbra, LiteLLM, Qdrant…) sans
  copie divergente. `api.environment: { <<: *env-backend, SYNELIA_TRAVAUX_WORKER: "true" }`.
- Nouveau service `worker` : `build: .`, `command: ["worker"]`, `environment: *env-backend`, `depends_on` postgres + minio
  `service_healthy`, `restart: unless-stopped`, `stop_grace_period: 60s` (le worker attend ses jobs 55 s puis est tué ; les
  jobs tués deviennent `failed` au boot suivant par 1.4 — comportement documenté, préférable à la perte silencieuse actuelle).
  Pas de `ports`. Commentaire en tête du service : un seul réplica — `reprendre_orphelins` suppose qu'un `running` sans
  `attente` n'appartient à personne ; passer à 2 réplicas exigerait un bail/heartbeat.
- `docker compose … config` doit rendre un YAML valide où `worker.environment` == `api.environment` moins
  `SYNELIA_TRAVAUX_WORKER`.

**Étape 1.6 — Bascule sur dev01** (données réelles — suivre l'ordre à la lettre).

1. Pré-vol lecture seule : `select statut, count(*) from travaux group by 1` ; noter les lignes `queued`/`running` et leur
   `started_at`. Vérifier qu'aucun utilisateur n'a un travail légitime en cours (les lignes `queued` de plus d'une heure sont
   par construction mortes : l'API actuelle les aurait déjà exécutées).
2. `docker compose … build api` (l'image sert aux deux services).
3. **Neutraliser les orphelins AVANT tout démarrage du worker** (superutilisateur, `psql -U synelia -d synelia`) :
   ```sql
   BEGIN;
   UPDATE travaux
      SET statut = 'failed', termine_le = now(),
          erreur = '{"message": "Travail interrompu par un redémarrage de l''API (avant la mise en place du worker) : non exécuté ou exécution incomplète.", "suggestion": "Relancez l''opération depuis l''écran d''origine si elle est toujours souhaitée."}'::json
    WHERE statut IN ('queued', 'running')
      AND NOT (contexte::jsonb ? 'attente')
      AND started_at < now() - interval '30 minutes';
   -- attendu : ~12-13 lignes (recompter avant COMMIT ; si le nombre surprend, ROLLBACK)
   COMMIT;
   ```
   Les `taches` restent `pending` : une relance reprendra à l'étape 0, ce qui est le comportement voulu pour un travail
   jamais commencé. Retour arrière : les ids ont été notés en 1 ; `UPDATE travaux SET statut='queued', erreur=NULL,
   termine_le=NULL WHERE id IN (…)`.
4. `docker compose … up -d api worker` en **une** commande : le nouveau conteneur `api` porte `SYNELIA_TRAVAUX_WORKER=true`
   dès son premier octet, donc il n'existe aucune fenêtre où l'ancien `create_task` et le worker exécutent le même travail.
   (Ne jamais démarrer le worker pendant que l'**ancienne** image API tourne encore : double exécution.)
5. Vérifier : `docker logs synelia-backend-dev01-worker-1` montre `worker.demarre` puis `travaux.orphelins` = 0 ;
   `docker exec synelia-backend-dev01-api-1 python3 -c "import synelia.travaux.local"` (le code est bien dans l'image) ;
   smoke réel : connexion admin, `POST /v1/vms` (ou une opération simulée sans coût, ex. un export d'audit
   `POST /v1/audit/export`) → `202 queued` → `GET /v1/travaux/{id}` passe `running` puis `done` en quelques secondes ;
   `select count(*) from travaux where statut='queued'` revient à 0. `/healthz` de l'API répond pendant qu'un job tourne.
6. Retour arrière (si le worker ne réclame rien ou échoue en boucle) : `docker compose … stop worker`, retirer
   `SYNELIA_TRAVAUX_WORKER` de `api`, `up -d api` → l'API reprend le `create_task` d'avant. Aucune donnée n'est perdue : les
   lignes `queued` créées entre-temps sont visibles dans `/travaux` et relançables.

**Étape 1.7 — `--workers 2` sur l'API** (après 1.6 stabilisé au moins un cycle de tests réel).

- `__main__.py::api` : paramètre `travailleurs: int = int(os.environ.get("SYNELIA_API_WORKERS", "1"))`, passé à
  `uvicorn.run(..., workers=travailleurs)`. Refuser `rechargement and travailleurs > 1` (uvicorn les rend incompatibles).
- `apps/synelia/synelia/otel.py::configurer` : ajouter `"service.instance.id": f"{socket.gethostname()}:{os.getpid()}"` à la
  `Resource` — sans cela, deux processus émettent les mêmes séries et VictoriaMetrics reçoit des compteurs entrelacés.
- `apps/synelia/synelia/deps/limitation.py` et `modules/ia_agents/cles.py` : une ligne de commentaire chacun — « seau par
  processus : avec `SYNELIA_API_WORKERS=N`, la limite effective est ×N » (la docstring de `limitation.py` annonce déjà
  Valkey pour plus tard ; ne pas l'implémenter ici).
- `docker-compose.dev01.yml` : `SYNELIA_API_WORKERS: "2"` sur `api` seulement ; healthcheck `api` :
  `["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4000/healthz', timeout=3).status == 200 else 1)"]`
  (l'image purge `curl`).
- Déployer (`build api && up -d api`), vérifier : deux lignes `api.demarree` dans les logs, `select count(*) from
  pg_stat_activity where usename='synelia_app'` reste < 60, `docker exec … ps` montre 1 superviseur + 2 workers, un
  `kill -9` d'un worker est suivi de son remplacement sans redémarrage du conteneur, `/audit/integrite` et
  `/v1/vms` répondent. Retour arrière : `SYNELIA_API_WORKERS: "1"`.

### Définition de « fait »

Aucun `create_task` d'exécution de travail ne s'exécute dans le processus API de dev01 ; un `docker compose up -d api`
pendant un job long ne laisse **aucune** ligne `queued`/`running` orpheline (le job continue dans le worker) ; un
`docker compose restart worker` pendant un job le passe `failed` avec un message lisible, relançable ; deux boots
API + worker + relais concurrents n'émettent aucune erreur DDL.

---

## 2. Gestion du schéma : `create_all` fait foi, pas Alembic

### Décision

**Ne pas installer Alembic. Dire la vérité dans le code et la rendre vérifiable.** Motifs, dans l'ordre de poids :

1. Le repo a **une** table de documents (`ressources`, JSON) et huit tables relationnelles stables ; en cinq jours de
   production réelle il y a eu **un** changement de colonne, fait à la main en 4 lignes de diff (`d5006a4`). Une chaîne de
   migrations pour ça, c'est une seconde source de vérité (modèles **et** scripts) que rien n'exercerait : tests et Vercel
   resteraient sur `create_all`/SQLite, donc une dérive migration ↔ modèle passerait inaperçue jusqu'au déploiement — la
   classe exacte de bug que ce dépôt combat (« un mécanisme déclaré qui ne s'exécute pas »).
2. `create_all` au boot **doit** rester pour SQLite ; Alembic serait un troisième chemin Postgres-seulement, à sérialiser
   lui aussi entre processus (§1).
3. La baseline contre une base vivante est précisément l'opération risquée qu'on nous demande de sécuriser — pour un
   bénéfice nul aujourd'hui.

Ce que l'on adopte à la place : la convention déjà pratiquée (`d5006a4`), écrite noir sur blanc, plus un outil de diff qui
prouve à tout moment que le Postgres vivant = `Base.metadata`. Si un jour le schéma relationnel change chaque semaine ou
qu'il existe plusieurs Postgres à faire évoluer en séquence, ré-ouvrir la question — l'ADR le dit.

### Étapes

**Étape 2.1 — Docstring honnête** (`packages/db/synelia_db/session.py::initialiser_schema`).

Remplacer la docstring par (adapter la forme, garder le fond) :
> « Crée les tables **manquantes** ; ne modifie jamais une table existante (ni colonne, ni index). `create_all` fait foi
> sur tous les environnements — SQLite (dev, tests, Vercel) comme Postgres (dev01). Il n'y a pas de migrations Alembic :
> tout changement d'une table existante sur Postgres est un `ALTER TABLE` fait à la main, dans le même commit que le
> changement du modèle, vérifié par `tools/schema_diff.py` (voir ADR 0003). Sérialisé entre processus par
> `pg_advisory_xact_lock` (plusieurs workers uvicorn, relais SMTP, worker des travaux). »

Même correction dans la docstring de `__main__.py::amorcer` (« Crée les tables manquantes et les données d'amorçage ») et
dans le commentaire de tête de `ADR/0001-persistance-depot-typee.md` si Alembic y est cité (grep).

**Étape 2.2 — Retirer la fausse dépendance** (`packages/db/pyproject.toml`, `uv.lock`).

- Supprimer `"alembic>=1.14"` ; `uv lock` puis `uv sync --frozen --no-dev` (c'est ce que fait le Dockerfile) et `uv run
  pytest -q packages`. Si `uv lock` exige le réseau et échoue dans l'environnement de l'agent, **ne pas** laisser un lock
  incohérent : revenir sur le `pyproject.toml` et noter dans l'ADR que la dépendance inutilisée reste à retirer.

**Étape 2.3 — `tools/schema_diff.py`** (nouveau, ~60 lignes, lecture seule).

- Ouvre l'engine de l'appli (`synelia_db.session.engine()`, donc `SYNELIA_DATABASE_URL` du conteneur, rôle `synelia_app`),
  `import synelia_db.modeles`, puis via `conn.run_sync(lambda c: inspect(c))` compare, pour chaque table de `Base.metadata` :
  présence de la table ; ensemble des colonnes ; nullabilité ; type compilé (`str(col.type.compile(dialect))` contre
  `str(insp_col["type"])`, comparaison tolérante à la casse et aux longueurs `VARCHAR(n)`) ; ensemble des noms d'index
  (hors `*_pkey`). Imprime un diff lisible, sortie 0 si identique, 1 sinon. Tables présentes en base mais absentes des
  modèles : signalées, non bloquantes (ex. une future `alembic_version` qui n'existe pas…).
- Ajouter un test SQLite `packages/db/tests/test_schema_diff.py` : après `initialiser_schema()` sur une base neuve, le diff
  est vide (protège l'outil lui-même).
- Vérifier sur dev01 : `docker exec synelia-backend-dev01-api-1 python tools/schema_diff.py` → « identique » (c'est l'état
  constaté aujourd'hui ; si l'outil trouve un écart, c'est l'outil qu'il faut corriger, pas la base).

**Étape 2.4 — ADR 0003** (`docs/ADR/0003-schema-create-all-sans-migrations.md`, même gabarit que 0001/0002 : Contexte,
Décision, Conséquences, ~30 lignes).

Contenu obligatoire : la décision et ses trois motifs ci-dessus ; la **procédure** d'un changement de table existante sur
Postgres — (1) modifier le modèle, (2) écrire l'`ALTER TABLE`/`CREATE INDEX` correspondant dans le message de commit
(comme `d5006a4`), (3) l'exécuter sur dev01 **en tant que propriétaire de la table** (`synelia_app` pour les tables
applicatives, `synelia` pour `audit` après §3) avant de déployer l'image, (4) `tools/schema_diff.py` doit rendre 0 avant
et après le déploiement ; l'interdiction de renommer/supprimer une colonne sans plan de repli ; le seuil de ré-examen
(« plusieurs changements relationnels par mois, ou plusieurs Postgres à faire évoluer »). Une ligne dans `GUIDE-MODULE.md`
§« Où sont les choses », entrée Persistance : « Schéma : `create_all` fait foi, jamais d'ALTER automatique — ADR 0003. »
Ne pas réécrire `PLAN-DIRECTEUR-PYTHON.md` (§324 promettait Alembic en Phase 0) : l'ADR consigne l'écart.

### Sécurité dev01

Cette section ne touche la base qu'en lecture (`schema_diff.py`). Aucun retour arrière nécessaire.

---

## 3. Audit : de « tamper-evident par convention » à « append-only imposé par Postgres », plus un ancrage hors-rôle

### Décision

Option **(a), dans sa forme minimale, en deux mécanismes proportionnés**, plus les corrections de vocabulaire de (b) :

1. **Séparation de privilèges sur `audit`** : la table passe à `synelia` (superutilisateur hors-ligne) et `synelia_app` ne
   garde que `SELECT, INSERT`. Aujourd'hui l'appli peut `UPDATE`/`DELETE`/`TRUNCATE` son propre journal puis recalculer
   la chaîne — `verifier_chaine` dirait « intacte ». Après : une appli compromise peut encore **ajouter** des lignes (ce
   qu'un hash chaîné ne peut pas empêcher), mais ne peut plus **réécrire** l'histoire. Coût : trois ordres SQL et la garde
   DDL de §1.1 (sans elle, le boot tenterait `ALTER TABLE audit …` sans en être propriétaire et planterait). C'est le
   meilleur rapport valeur/coût de toute cette section, et il ne dépend d'aucune messagerie.
2. **Ancrage quotidien hors-rôle** : le worker (§1.4) émet une fois par jour, pour chaque organisation et la plateforme,
   `{org_id, nombre_de_lignes, empreinte_de_tete}` — (i) en ligne de journal structuré `audit.ancrage` (les logs Docker sont
   écrits par le démon sous root, hors de portée du rôle Postgres et de l'utilisateur `synelia` du conteneur : ce n'est pas
   hors-boîte, mais c'est hors-rôle), (ii) par courriel via `synelia_kernel.courriel.envoyer` à l'adresse
   `SYNELIA_AUDIT_ANCRAGE_EMAIL` si elle est définie (le compte SMTP admin existe déjà sur dev01 ; la délivrabilité externe
   depuis dev01 est incertaine, cf. mémoire Zimbra SPF/DKIM — donc best-effort, et la doc doit le dire). ~40 lignes. On
   **ne** construit **pas** d'export signé, de stockage WORM ni d'horodatage tiers : disproportionné pour 19 organisations
   de lab.
3. **Vocabulaire** : nulle part le code ne dit « tamper-proof », mais `verifier_chaine` promet qu'une modification est
   « immédiatement détectable » — vrai seulement si l'auteur ne peut pas recalculer la chaîne. On corrige pour dire
   exactement ce qui est garanti, et contre qui.

### Étapes

**Étape 3.1 — Prérequis** : §1.1 déployé et vérifié sur dev01 (deux boots propres avec la DDL RLS filtrée). Sans cela,
ne pas continuer : l'étape 3.2 ferait planter le boot.

**Étape 3.2 — Transfert de propriété de `audit` sur dev01** (superutilisateur, `psql -U synelia -d synelia`).

1. Pré-vol : `grep -rn "Audit" apps packages --include=*.py | grep -E "update\(|delete\(|\.delete|TRUNCATE"` doit être
   vide (le modèle le promet ; le vérifier). `select count(*) from audit` noté. `/v1/audit/integrite` (admin connecté)
   renvoie `intacte: true` — noter `empreinteFinale`.
2. ```sql
   BEGIN;
   ALTER TABLE audit OWNER TO synelia;
   REVOKE ALL ON TABLE audit FROM synelia_app;
   GRANT SELECT, INSERT ON TABLE audit TO synelia_app;
   -- les politiques RLS et FORCE restent attachées à la table (propriété ≠ politique) :
   SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'audit';   -- t | t
   SELECT policyname FROM pg_policies WHERE tablename = 'audit';                        -- audit_org
   COMMIT;
   ```
   Puis, **hors** de cette transaction (en psql, une erreur met la transaction courante en état d'échec — chaque test
   ci-dessous doit donc être son propre ordre, en autocommit) :
   ```sql
   SET ROLE synelia_app;
   UPDATE audit SET acteur = acteur WHERE false;   -- attendu : ERROR: permission denied for table audit
   DELETE FROM audit WHERE false;                  -- attendu : ERROR: permission denied for table audit
   INSERT INTO audit (id, date, acteur, action, resultat, details) VALUES ('test-droits', now(), 'test', 'test', 'succes', '{}');
                                                   -- attendu : INSERT 0 1 (puis, en RESET ROLE : DELETE FROM audit WHERE id = 'test-droits';)
   RESET ROLE;
   ```
   Vérifier `select privilege_type from information_schema.role_table_grants where grantee='synelia_app' and
   table_name='audit'` → exactement `SELECT`, `INSERT`.
3. `docker compose … restart api worker` → boot propre (la garde 1.1 ne voit rien à faire sur `audit`) ; une mutation réelle
   (ex. renommer une VM ou créer une clé d'API) ajoute une ligne d'audit ; `/v1/audit/integrite` toujours `intacte`.
4. Retour arrière : `ALTER TABLE audit OWNER TO synelia_app;` (le propriétaire retrouve tous les droits) puis `restart api`.
5. Conséquence à consigner (ADR 0003 §procédure) : tout futur `ALTER TABLE audit` se fait en tant que `synelia`.

**Étape 3.3 — Ancrage quotidien** (`apps/synelia/synelia/audit.py`, `apps/synelia/synelia/travaux/local.py`, `config.py`).

- `audit.py` : `async def tetes_de_chaine(session) -> list[dict]` — une requête `select org_id, count(*), max(date)` groupée,
  puis pour chaque org l'`hash` de la ligne la plus récente (même ordre que `journaliser` : `order_by(desc(date)).limit(1)`).
  `async def ancrer(session) -> None` : `log.info("audit.ancrage", tetes=…)` **toujours** ; puis, si
  `reglages().audit_ancrage_email`, `await courriel.envoyer(destinataire, sujet="[Synelia] Ancrage du journal d'audit —
  <date>", titre=…, paragraphes=[une ligne par org : "<org_id> · <n> lignes · <hash>"])`. Best-effort comme le reste de
  `courriel` : une panne SMTP est journalisée, jamais levée.
- `synelia_kernel/config.py` : `audit_ancrage_email: str | None = None` (`SYNELIA_AUDIT_ANCRAGE_EMAIL`).
- `local.py::boucle` : tâche compagne `ancrage_quotidien()` — au boot puis toutes les 24 h (`asyncio.sleep`), appelle
  `ancrer()` dans sa propre session. Pas de nouvelle ligne d'audit pour l'ancrage lui-même (ce serait circulaire).
- `docker-compose.dev01.yml` : `SYNELIA_AUDIT_ANCRAGE_EMAIL` dans l'ancre `&env-backend`, valeur lue depuis le fichier env
  (`${SYNELIA_AUDIT_ANCRAGE_EMAIL:-}` — optionnelle ; l'utilisateur décide de l'adresse, ne pas en inventer une).
- Vérifier : test SQLite `tetes_de_chaine` renvoie une tête par org de la fixture, égale à `empreinteFinale` de
  `verifier_chaine` pour cette org ; sur dev01, au boot du worker, la ligne `audit.ancrage` apparaît dans `docker logs` avec
  19 + 1 entrées, et — si l'adresse est configurée — le courriel **arrive réellement** (le confirmer ; sinon, le documenter
  comme non délivré et laisser le journal comme seul ancrage effectif).

**Étape 3.4 — Vocabulaire et modèle de menace** (aucune base touchée).

- `packages/db/synelia_db/modeles/audit.py` docstring : « Append-only imposé par Postgres : le rôle applicatif
  (`synelia_app`) n'a que `SELECT, INSERT` sur cette table (propriétaire : `synelia`, hors-ligne). Hash chaîné par
  organisation sur `hash_precedent`. »
- `apps/synelia/synelia/audit.py::verifier_chaine` docstring : garder l'explication, ajouter le périmètre : « Détecte toute
  altération faite **sans** le droit de réécrire la chaîne — c'est-à-dire tout ce que peut faire le rôle applicatif (insertion
  hors séquence exclue) et toute corruption accidentelle. Ne prouve rien contre un acteur disposant du superutilisateur
  Postgres ou de l'hôte : celui-ci peut recalculer la chaîne. Contre lui, seule la comparaison avec un ancrage externe
  (`ancrer`, journal `audit.ancrage` / courriel quotidien) fait foi. » Même phrase, condensée, dans la docstring de
  `modules/audit/router.py::verifier_integrite_audit`.
- `docs/GUIDE-MODULE.md`, entrée « Audit » de « Où sont les choses », une ligne : « Le journal est *tamper-evident*, pas
  *tamper-proof* : append-only imposé par les droits Postgres, chaîne vérifiable par `/audit/integrite`, tête de chaîne
  ancrée chaque jour hors du rôle applicatif ; un superutilisateur Postgres reste hors du modèle de menace. »
- `docs/ADR/0001-persistance-depot-typee.md` §11 (« audit (append-only, hash chaîné) ») : ajouter « droits Postgres
  restreints, cf. PLAN-ARCHITECTURE-SUITE §3 ». Ne pas toucher `PLAN-DIRECTEUR.md` (le trigger promis §410 est remplacé
  par le REVOKE, plus fort ; l'écart est consigné ici).

### Définition de « fait »

`SET ROLE synelia_app; UPDATE audit …` échoue sur dev01 ; l'API et le worker bootent et écrivent des lignes d'audit ;
`/audit/integrite` reste `intacte` ; une ligne `audit.ancrage` par jour dans les logs du worker ; docstrings et guide
disent exactement ce que la chaîne garantit et contre qui.

---

## Pièges transverses (lire avant de commencer)

- **Ordre** : 1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6 → 2.x → 3.1 → 3.2 → 3.3 → 3.4 → 1.7. Ne pas faire 1.7 avant 1.6 ; ne pas
  faire 3.2 avant 1.1 déployé.
- **Jamais** l'appli connectée en `synelia` (superutilisateur) — les commandes SQL de ce plan sont hors-ligne, via
  `docker exec … psql -U synelia`.
- Toute commande compose dev01 : `docker compose --env-file /home/jekas/.config/synelia/backend-dev01.env
  -f docker-compose.dev01.yml …` ; confirmer qu'un code est en ligne par `docker exec … python3 -c "import …"`, pas par
  l'uptime.
- Le lab OpenStack s'éteint vers 21:01 UTC : ne pas interpréter un job `failed` « amont indisponible » du soir comme une
  régression du worker.
- Les tests tournent avec `SYNELIA_TRAVAUX_EN_LIGNE=1` : le mode worker n'est couvert que par les tests explicites de 1.4 ;
  ne pas supprimer le chemin en ligne.
- Limitations **connues et conservées** (hors périmètre, ne pas « corriger » en passant) : `annuler()` ne stoppe pas une
  tâche en cours d'exécution (comme aujourd'hui) ; limiteurs de débit par processus ; `MinioSimule` en mémoire par processus
  (sans effet sur dev01 où MinIO est réel).
