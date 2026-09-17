from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from synelia_kernel.config import reglages

from synelia_db import rls
from synelia_db.base import Base

_engine: AsyncEngine | None = None
_fabrique: async_sessionmaker[AsyncSession] | None = None

# Verrous consultatifs Postgres (`pg_advisory_xact_lock`) : sérialisent le démarrage entre
# processus concurrents (API, worker des travaux, relais SMTP — cf. `docker-compose.dev01.yml`).
# Deux clés distinctes pour ne pas bloquer inutilement l'amorçage (`amorcage.py`) derrière le
# schéma (`initialiser_schema`) d'un *autre* processus déjà passé cette étape — un verrou
# transactionnel se relâche au premier commit qui suit son acquisition, donc chacun doit couvrir
# exactement une seule transaction logique. Constantes arbitraires (espace de clés `bigint`
# 64 bits Postgres, aucune collision connue avec un autre usage de ce cluster).
CLE_VERROU_SCHEMA = 7_130_001
CLE_VERROU_AMORCAGE = 7_130_002


def engine() -> AsyncEngine:
    global _engine, _fabrique
    if _engine is None:
        r = reglages()
        options: dict = {"echo": r.echo_sql}
        if r.est_sqlite:
            options["connect_args"] = {"timeout": 30}
        else:
            options["pool_pre_ping"] = True
            options["pool_size"] = 5
        _engine = create_async_engine(r.database_url, **options)
        if r.est_postgres and r.rls_active:
            rls.brancher(_engine)
        _fabrique = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def fabrique() -> async_sessionmaker[AsyncSession]:
    engine()
    assert _fabrique is not None
    return _fabrique


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with fabrique()() as s:
        yield s


async def initialiser_schema() -> None:
    """Crée les tables **manquantes** ; ne modifie jamais une table existante (ni colonne, ni
    index). `create_all` fait foi sur tous les environnements — SQLite (dev, tests, Vercel)
    comme Postgres (dev01). Il n'y a pas de migrations Alembic : tout changement d'une table
    existante sur Postgres est un `ALTER TABLE` fait à la main, dans le même commit que le
    changement du modèle, vérifié par `tools/schema_diff.py` (voir ADR 0003). Sérialisé entre
    processus par `pg_advisory_xact_lock` (plusieurs workers uvicorn, relais SMTP, worker des
    travaux)."""
    import synelia_db.modeles  # noqa: F401  — enregistre les tables

    r = reglages()
    eng = engine()
    async with eng.begin() as conn:
        if r.est_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        if r.est_postgres:
            # Transactionnel : couvre `create_all` et la DDL RLS ci-dessous, se relâche au
            # commit qui termine ce `async with eng.begin()`. Protège aussi, gratuitement, la
            # course déjà présente API ↔ relais-smtp ↔ worker des travaux (tous appellent
            # `initialiser_schema()` à leur boot).
            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLE_VERROU_SCHEMA})
        await conn.run_sync(Base.metadata.create_all)
        if r.est_postgres and r.rls_active:
            for ddl in await rls.politiques_manquantes(conn):
                await conn.execute(text(ddl))


async def fermer() -> None:
    global _engine, _fabrique
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _fabrique = None
