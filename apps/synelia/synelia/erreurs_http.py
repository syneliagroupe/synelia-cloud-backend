"""Gestionnaire unique : toute erreur sort dans la forme du contrat, avec `correlationId`."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from synelia_kernel import erreurs
from synelia_kernel.journal import journal

log = journal("http")

_CODES_HTTP = {
    400: "requete_invalide",
    401: "non_authentifie",
    403: "interdit",
    404: "introuvable",
    405: "methode_non_autorisee",
    409: "conflit",
    413: "trop_volumineux",
    415: "type_non_supporte",
    422: "validation",
    429: "trop_de_requetes",
}


def _cid(request: Request) -> str:
    return getattr(request.state, "correlation_id", "-")


def _reponse(
    request: Request, err: erreurs.AppError, headers: dict[str, str] | None = None
) -> ORJSONResponse:
    return ORJSONResponse(status_code=err.statut, content=err.corps(_cid(request)), headers=headers)


def installer(app: FastAPI) -> None:
    @app.exception_handler(erreurs.AppError)
    async def _app_error(request: Request, exc: erreurs.AppError) -> ORJSONResponse:
        if exc.statut >= 500:
            log.error("erreur_applicative", code=exc.code, detail=exc.detail)
        return _reponse(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> ORJSONResponse:
        champs: dict[str, str] = {}
        mal_forme = False
        for e in exc.errors():
            loc = [str(x) for x in e.get("loc", ()) if x not in ("body", "query", "path", "header")]
            if e.get("type") in {"json_invalid", "missing"} and not loc:
                mal_forme = True
            champs[".".join(loc) or "corps"] = e.get("msg", "invalide")
        if mal_forme or any(e.get("type") == "json_invalid" for e in exc.errors()):
            return _reponse(
                request,
                erreurs.invalide("Corps JSON illisible ou absent.", detail=json.dumps(champs)),
            )
        return _reponse(
            request,
            erreurs.validation("Validation échouée : vérifiez les champs signalés.", champs),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> ORJSONResponse:
        code = _CODES_HTTP.get(exc.status_code, "erreur_http")
        message: Any = exc.detail if isinstance(exc.detail, str) else "Requête refusée."
        if exc.status_code == 404:
            message = "Chemin inconnu de l'API."
        err = erreurs.AppError(code, exc.status_code, message)
        return _reponse(request, err, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _inconnue(request: Request, exc: Exception) -> ORJSONResponse:
        log.exception("erreur_interne", erreur=str(exc))
        return _reponse(request, erreurs.interne(detail=type(exc).__name__))
