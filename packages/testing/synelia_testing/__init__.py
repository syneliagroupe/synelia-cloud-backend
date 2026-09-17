"""Outils de test : application sur SQLite éphémère, client authentifié, aides."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

ADMIN_EMAIL = "admin@synelia.cloud"
ADMIN_MDP = "Synelia!2026"


def configurer_env() -> str:
    d = tempfile.mkdtemp(prefix="synelia-test-")
    os.environ["SYNELIA_ENV"] = "test"
    os.environ["SYNELIA_DATABASE_URL"] = f"sqlite+aiosqlite:///{d}/test.sqlite3"
    os.environ["SYNELIA_SEED_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["SYNELIA_SEED_ADMIN_MOT_DE_PASSE"] = ADMIN_MDP
    os.environ["SYNELIA_TRAVAUX_EN_LIGNE"] = "1"
    os.environ.setdefault("SYNELIA_SEED_DEMO", "true")
    return d


class ClientApi(httpx.AsyncClient):
    """Client ASGI avec `Authorization` posé une fois ; `.v1("/vms")` préfixe les chemins."""

    jeton: str | None = None
    org_id: str | None = None

    def v1(self, chemin: str) -> str:
        return f"/v1{chemin}"

    async def connecter(
        self, email: str = ADMIN_EMAIL, mot_de_passe: str = ADMIN_MDP
    ) -> dict[str, Any]:
        r = await self.post("/v1/auth/connexion", json={"email": email, "motDePasse": mot_de_passe})
        assert r.status_code == 200, r.text
        corps = r.json()
        self.jeton = corps["accessToken"]
        self.org_id = corps.get("organisationActive")
        self.headers["Authorization"] = f"Bearer {self.jeton}"
        return corps


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[ClientApi]:
    """Application neuve (base SQLite éphémère), admin connecté."""
    d = configurer_env()
    from synelia_kernel import config

    config.reglages.cache_clear()
    from synelia import amorcage
    from synelia_db import session as db

    amorcage._AMORCE = False
    await db.fermer()
    from synelia.app import creer_app

    try:
        app = creer_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with ClientApi(transport=transport, base_url="http://test") as c:
                await c.connecter()
                yield c
        await db.fermer()
    finally:
        shutil.rmtree(d, ignore_errors=True)
