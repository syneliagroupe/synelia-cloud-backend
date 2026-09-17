# ADR 0003 — Gestion du schéma : `create_all` fait foi, pas de migrations Alembic

Date : 2026-09-08. Statut : accepté.

## Contexte
`alembic>=1.14` était déclaré dans `packages/db/pyproject.toml` mais aucun répertoire, script ni table
`alembic_version` n'existait nulle part — une dépendance inutilisée qui laissait croire à un mécanisme de
migration réel. Le dépôt a une table de documents (`ressources`, JSON) et huit tables relationnelles
stables ; en cinq jours de production réelle il y a eu **un** changement de colonne, fait à la main en
4 lignes de diff (`d5006a4`, `ressources.cree_par_id`). Tests et Vercel tournent sur SQLite via `create_all`
dans le lifespan (`synelia.app::_vie`) ; Postgres (dev01) exécute le même `create_all` au boot.

## Décision
**Ne pas installer Alembic. Dire la vérité dans le code et la rendre vérifiable.** Trois motifs, dans
l'ordre de poids :

1. Une chaîne de migrations pour un schéma qui change une fois tous les cinq jours crée une seconde
   source de vérité (modèles **et** scripts) que rien n'exercerait : tests et Vercel resteraient sur
   `create_all`/SQLite, donc une dérive migration ↔ modèle passerait inaperçue jusqu'au déploiement —
   exactement la classe de bug que ce dépôt combat (« un mécanisme déclaré qui ne s'exécute pas »).
2. `create_all` au boot **doit** rester pour SQLite (dev, tests, Vercel) ; Alembic serait un troisième
   chemin, Postgres-seulement, à sérialiser lui aussi entre processus (§1 de
   `docs/PLAN-ARCHITECTURE-SUITE.md` — verrou `pg_advisory_xact_lock`, déjà nécessaire pour API + worker
   + relais SMTP concurrents).
3. La baseline d'une chaîne de migrations contre une base vivante (dev01) est précisément l'opération
   risquée qu'on cherche à éviter — pour un bénéfice nul aujourd'hui.

Adopté à la place : la convention déjà pratiquée (`d5006a4`), écrite noir sur blanc, plus un outil de diff
qui prouve à tout moment que le Postgres vivant = `Base.metadata`.

### Procédure d'un changement de table existante sur Postgres
1. Modifier le modèle SQLAlchemy (`packages/db/synelia_db/modeles/`).
2. Écrire l'`ALTER TABLE`/`CREATE INDEX` correspondant, dans le message du même commit (comme `d5006a4`).
3. L'exécuter sur dev01 **en tant que propriétaire de la table** — `synelia_app` pour les tables
   applicatives, `synelia` (superutilisateur, hors-ligne) pour `audit` depuis §3 du plan d'architecture
   (privilège séparé : `synelia_app` n'a plus que `SELECT, INSERT` sur `audit`) — **avant** de déployer
   l'image qui porte le nouveau modèle.
4. `tools/schema_diff.py` doit rendre 0 (« identique ») avant et après le déploiement.

Interdiction : renommer ou supprimer une colonne sans plan de repli explicite (double-écriture ou fenêtre
de dépréciation documentée).

### Seuil de ré-examen
Si le schéma relationnel change plusieurs fois par mois, ou s'il existe plusieurs Postgres à faire évoluer
en séquence (plusieurs environnements durables au-delà de dev01), ré-ouvrir cette décision — Alembic
redevient alors proportionné.

## Conséquences
- `packages/db/synelia_db/session.py::initialiser_schema` documente exactement ceci dans sa docstring.
- `tools/schema_diff.py` (lecture seule, comparaison `Base.metadata` ↔ catalogue Postgres vivant) rend le
  fait vérifiable à la demande, sur dev01 comme en local ; testé sur SQLite
  (`packages/db/tests/test_schema_diff.py`) pour se protéger lui-même d'une régression.
- `alembic` retiré de `packages/db/pyproject.toml` et de `uv.lock`.
- `PLAN-DIRECTEUR-PYTHON.md` §324 promettait Alembic en Phase 0 socle : cet ADR consigne l'écart
  délibéré, le document n'est pas réécrit.
