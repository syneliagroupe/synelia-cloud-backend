"""Protège `tools/schema_diff.py` lui-même : sur une base SQLite neuve, juste après
`initialiser_schema()` (donc `create_all`), le schéma vivant doit être *identique* à
`Base.metadata` par construction — si ce test échoue, c'est l'outil de comparaison qu'il faut
corriger, pas la base (cf. `docs/ADR/0003-schema-create-all-sans-migrations.md`)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[3]


@pytest.fixture
async def base_sqlite_neuve() -> AsyncIterator[None]:
    d = tempfile.mkdtemp(prefix="synelia-schema-diff-")
    os.environ["SYNELIA_DATABASE_URL"] = f"sqlite+aiosqlite:///{d}/test.sqlite3"
    from synelia_kernel import config

    config.reglages.cache_clear()
    from synelia_db import session as db

    await db.fermer()
    try:
        yield
    finally:
        await db.fermer()
        os.environ.pop("SYNELIA_DATABASE_URL", None)
        config.reglages.cache_clear()
        shutil.rmtree(d, ignore_errors=True)


async def test_schema_vivant_egale_base_metadata(base_sqlite_neuve: None) -> None:
    sys.path.insert(0, str(RACINE / "tools"))
    import schema_diff
    from synelia_db.session import initialiser_schema

    await initialiser_schema()
    ecarts = await schema_diff.diff()
    bloquants = [e for e in ecarts if "non bloquant" not in e]
    assert bloquants == [], bloquants
