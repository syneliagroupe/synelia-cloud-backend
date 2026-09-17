# ADR 0001 — Persistance : un dépôt typé par le contrat, pas une table par ressource

Date : 2026-09-03. Statut : accepté (Phase 0).

## Contexte
Le plan (§6) liste ~120 tables. Livrer les 514 opérations vite, sur Vercel (SQLite éphémère) comme sur
Postgres, exigeait une persistance qui n'oblige pas à écrire une migration par ressource.

## Décision
Les tables **identité** (`utilisateurs`, `organisations`, `memberships`, `invitations`, `sessions_auth`,
`cles_api`), **audit** (append-only, hash chaîné — droits Postgres restreints, cf.
`docs/PLAN-ARCHITECTURE-SUITE.md` §3) et **travaux** (projection des workflows) sont dédiées.
Toutes les autres ressources du contrat vivent dans `ressources` (`type`, `org_id`, `nom`, `statut`,
`parent_id`, `donnees` JSON, `secrets` chiffrés), lues et écrites par `Depot[T]` où `T` est la classe
Pydantic **générée du contrat**. Le module reste propriétaire de ses règles ; le dépôt ne fait que ranger.

## Conséquences
- Aucun CRUD n'est exposé automatiquement : chaque route est écrite, testée, journalisée (le plan §13 est respecté).
- RLS Postgres s'applique à `ressources` par `org_id` ; SQLite n'a que le filtre applicatif.
- Filtrage/tri en mémoire par organisation (`ponytail:` plafond ~10⁴ lignes par type et par org ; passer
  à des index JSONB quand une liste dépasse ce seuil).
- Extraire une table dédiée plus tard = une migration + changer le dépôt d'un module, sans toucher aux routes.
