"""Admin catalogue tests fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import synelia.app as app_mod
from fastapi import APIRouter
from synelia import modules


def _routeurs_importables() -> list[APIRouter]:
    import importlib
    import pkgutil

    routeurs: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(modules.__path__), key=lambda i: i.name):
        try:
            mod = importlib.import_module(f"{modules.__name__}.{info.name}")
        except Exception:  # noqa: BLE001 - module d'un autre agent encore incomplet
            continue
        r = getattr(mod, "router", None)
        if isinstance(r, APIRouter):
            routeurs.append(r)
        for extra in getattr(mod, "routers", []) or []:
            if isinstance(extra, APIRouter):
                routeurs.append(extra)
    return routeurs


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from synelia_kernel import config
    from synelia_testing import ClientApi, configurer_env

    configurer_env()
    config.reglages.cache_clear()
    from synelia import amorcage
    from synelia_db import session as db

    amorcage._AMORCE = False
    await db.fermer()
    original = app_mod.routeurs_modules
    app_mod.routeurs_modules = _routeurs_importables
    try:
        app = app_mod.creer_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with ClientApi(transport=transport, base_url="http://test") as c:
                await c.connecter()
                yield c
    finally:
        app_mod.routeurs_modules = original
    await db.fermer()
