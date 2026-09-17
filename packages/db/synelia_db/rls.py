"""Row-Level Security : `SET LOCAL app.org_id` posé à l'ouverture de chaque transaction Postgres.

Le filtre applicatif par `org_id` existe aussi ; la RLS est la ceinture en plus des bretelles —
mais seulement si le rôle Postgres qui exécute les requêtes n'est ni superutilisateur ni
BYPASSRLS, et que `FORCE ROW LEVEL SECURITY` est posé (sinon le propriétaire de la table
contourne aussi sa propre politique). Voir `docker-compose.dev01.yml` : le conteneur `api` se
connecte avec un rôle applicatif dédié (`synelia_app`, NOSUPERUSER NOBYPASSRLS), jamais avec le
superutilisateur `synelia` réservé à l'accès hors-ligne.
Sur SQLite (dev, Vercel sans Postgres) seule la couche applicative s'applique."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

org_id_transaction: ContextVar[str | None] = ContextVar("org_id_transaction", default=None)

TABLES_TENANT = (
    "ressources",
    "travaux",
    "audit",
    "memberships",
    "invitations",
    "cles_api",
    "sessions_auth",
)


def brancher(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "begin")
    def _poser_org(conn) -> None:  # type: ignore[no-untyped-def]
        org = org_id_transaction.get()
        conn.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": org or ""})


async def poser(session: AsyncSession, org_id: str | None) -> None:
    """Pose `app.org_id` directement sur la transaction déjà ouverte de `session`, sans
    attendre l'écouteur `begin` (`_poser_org` ci-dessus), qui ne s'exécute qu'une seule fois,
    à l'ouverture de la transaction Postgres.

    Nécessaire partout où l'`org_id` définitif d'une requête n'est connu qu'*après* la
    première requête SQL de sa transaction — ce qui est le cas courant, pas l'exception :
    résoudre le principal d'une requête HTTP authentifiée exige de lire `sessions_auth`/
    `utilisateurs`/`memberships` (voir `deps/contexte.py::contexte`), et un travail de fond
    doit lire sa propre ligne `travaux` pour connaître son `org_id` (voir
    `travaux/local.py::executer_un`) — dans les deux cas, ces lectures ouvrent déjà la
    transaction, avec `org_id_transaction` encore à sa valeur par défaut (`None`), avant que
    l'appelant sache quoi y mettre. Sans cet appel explicite après coup, `app.org_id` reste
    figé à `''` pour le reste de la transaction (toutes les lignes visibles, RLS no-op) même
    si `org_id_transaction.set(...)` est appelé ensuite : l'écouteur `begin` ne se redéclenche
    pas sur une transaction déjà ouverte.

    No-op hors Postgres (SQLite n'a pas `set_config`, et `TABLES_TENANT` n'y est de toute
    façon filtré que par la couche applicative)."""
    from synelia_kernel.config import reglages

    if not reglages().est_postgres:
        return
    await session.execute(
        text("SELECT set_config('app.org_id', :org, true)"), {"org": org_id or ""}
    )


@asynccontextmanager
async def sans_org(session: AsyncSession) -> AsyncIterator[None]:
    """Lève temporairement le filtre RLS par organisation sur la transaction déjà ouverte de
    `session`, pour une requête qui doit volontairement traverser plusieurs organisations —
    ex. `appartenances()` qui liste toutes les organisations d'un utilisateur pour le
    sélecteur : sans ceci, la RLS de `memberships` (ceinture en plus des bretelles, cf.
    module) ne laissait voir que l'organisation active de la session, et un utilisateur
    multi-organisation ne voyait jamais ses autres organisations dans le sélecteur (constaté
    en direct : `admin@synelia.cloud`, `org_admin` sur deux organisations, n'en voyait qu'une).

    S'appuie sur `poser()` ci-dessus (même mécanisme direct, pas l'écouteur `begin`), restauré
    à la sortie. Reste nécessaire même après la correction du bug général de `poser()` en
    aval de `contexte()` : cette fonction lève *volontairement* le filtre pour une portée plus
    large que l'organisation active du principal, ce n'est pas le même besoin."""
    org = org_id_transaction.get() or ""
    await poser(session, "")
    try:
        yield
    finally:
        await poser(session, org)


def _sql_politique(table: str) -> str:
    return (
        f"CREATE POLICY {table}_org ON {table} USING ("
        f"  org_id IS NULL OR current_setting('app.org_id', true) = '' "
        f"  OR org_id = current_setting('app.org_id', true))"
    )


def sql_politiques() -> list[str]:
    """DDL complet des politiques RLS (Postgres), inconditionnel — conservé pour compatibilité ;
    préférer `politiques_manquantes(conn)` qui ne rejoue que ce qui manque réellement. **Changer
    le texte d'une politique existante** ne passe pas par ce module (un `CREATE POLICY` échoue
    si la politique existe déjà) : il faut la supprimer à la main, en tant que superutilisateur
    (`DROP POLICY <table>_org ON <table>`), puis redémarrer un processus pour qu'il la recrée."""
    ddl: list[str] = []
    for table in TABLES_TENANT:
        ddl += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            # Sans FORCE, le propriétaire de la table (le rôle applicatif lui-même) contourne
            # la RLS comme le ferait un superutilisateur — FORCE l'applique aussi à ce rôle,
            # seul un filet de sécurité contre un bug applicatif, pas contre le rôle admin
            # hors-ligne (superutilisateur `synelia`, jamais utilisé par l'appli en marche).
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {table}_org ON {table}",
            _sql_politique(table),
        ]
    return ddl


async def politiques_manquantes(conn: AsyncConnection) -> list[str]:
    """Comme `sql_politiques()`, mais lit l'état réel (`pg_class`, `pg_policies`) et ne renvoie
    que les ordres nécessaires — plus de `DROP POLICY` systématique. Sûr à rejouer à chaque boot
    de chaque processus (API, worker, relais SMTP) sous le verrou consultatif de
    `session.py::initialiser_schema`."""
    lignes = (
        (
            await conn.execute(
                text(
                    "SELECT c.relname AS table, c.relrowsecurity, c.relforcerowsecurity,"
                    "       EXISTS(SELECT 1 FROM pg_policies p"
                    "              WHERE p.tablename = c.relname AND p.policyname = c.relname || '_org')"
                    "         AS a_politique"
                    "  FROM pg_class c"
                    " WHERE c.relname = ANY(:tables)"
                ),
                {"tables": list(TABLES_TENANT)},
            )
        )
        .mappings()
        .all()
    )
    ddl: list[str] = []
    for ligne in lignes:
        table = ligne["table"]
        if not ligne["relrowsecurity"]:
            ddl.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        if not ligne["relforcerowsecurity"]:
            ddl.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        if not ligne["a_politique"]:
            ddl.append(_sql_politique(table))
    return ddl
