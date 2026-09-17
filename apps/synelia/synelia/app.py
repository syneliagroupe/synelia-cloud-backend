"""Fabrique FastAPI : middlewares, routeurs des modules, cycle de vie."""

from __future__ import annotations

import importlib
import pkgutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, ORJSONResponse
from synelia_kernel.config import reglages
from synelia_kernel.dates import iso
from synelia_kernel.journal import configurer, journal

from synelia import erreurs_http, modules, otel
from synelia.deps.correlation import CorrelationMiddleware

log = journal("app")

_SCALAR = """<!doctype html><html><head><meta charset="utf-8"><title>Synelia Cloud — API</title></head>
<body><script id="api-reference" data-url="{url}"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script></body></html>"""


def routeurs_modules() -> list[APIRouter]:
    """Découvre `synelia.modules.<x>.router` : un module s'ajoute sans toucher à ce fichier."""
    routeurs: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(modules.__path__), key=lambda i: i.name):
        mod = importlib.import_module(f"{modules.__name__}.{info.name}")
        candidats = [getattr(mod, "router", None), *(getattr(mod, "routers", []) or [])]
        for r in candidats:
            if isinstance(r, APIRouter) and not any(r is x for x in routeurs):
                routeurs.append(r)
    return routeurs


def creer_app() -> FastAPI:
    r = reglages()
    configurer(json=r.env != "local")

    @asynccontextmanager
    async def _vie(app: FastAPI):
        from synelia_db.session import fermer, initialiser_schema

        from synelia import amorcage

        await initialiser_schema()
        await amorcage.amorcer()
        log.info(
            "api.demarree",
            env=r.env,
            base="sqlite" if r.est_sqlite else "postgres",
            fournisseur=r.fournisseur,
        )
        yield
        await fermer()

    app = FastAPI(
        title=r.nom,
        version=r.version,
        description="Backend de Synelia Cloud — contrat `openapi.json` du portail, servi sur OpenStack.",
        default_response_class=ORJSONResponse,
        openapi_url=f"{r.prefixe_api}/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=_vie,
        servers=[{"url": r.url_publique + r.prefixe_api}],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=r.cors_origines,
        allow_credentials=r.cors_origines != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-Id"],
    )
    app.add_middleware(CorrelationMiddleware)
    erreurs_http.installer(app)
    otel.configurer(app, version=r.version, env=r.env)

    v1 = APIRouter(prefix=r.prefixe_api)
    for routeur in routeurs_modules():
        v1.include_router(routeur)
    app.include_router(v1)

    @app.get("/healthz", include_in_schema=False)
    @app.get(f"{r.prefixe_api}/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"statut": "ok", "version": r.version, "env": r.env, "date": iso()}

    @app.get("/", include_in_schema=False)
    async def racine() -> dict[str, Any]:
        return {
            "nom": r.nom,
            "version": r.version,
            "contrat": f"{r.prefixe_api}/openapi.json",
            "docs": f"{r.prefixe_api}/docs",
        }

    if r.docs_actives:

        @app.get(f"{r.prefixe_api}/docs", include_in_schema=False)
        async def docs() -> HTMLResponse:
            return HTMLResponse(_SCALAR.format(url=f"{r.prefixe_api}/openapi.json"))

    return app
