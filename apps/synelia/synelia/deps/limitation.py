"""Limitation de débit : seau à jetons en mémoire par jeton/clé/IP (Valkey partagé plus tard)."""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.requests import Request
from synelia_kernel import erreurs
from synelia_kernel.config import reglages

# Seau par processus : avec `SYNELIA_API_WORKERS=N` (plusieurs workers uvicorn, étape 1.7 de
# docs/PLAN-ARCHITECTURE-SUITE.md), chaque worker a son propre dict — la limite effective est
# ×N. Pas corrigé ici (la docstring de ce module annonce déjà Valkey pour plus tard).
_seaux: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
CAPACITE = 600.0
DEBIT_PAR_S = 10.0


def cle_client(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("x-api-key")
    if auth:
        return "j:" + auth[-32:]
    return "ip:" + (request.client.host if request.client else "?")


def verifier(request: Request) -> None:
    if reglages().env == "test":
        return
    cle = cle_client(request)
    jetons, dernier = _seaux[cle]
    now = time.monotonic()
    jetons = min(CAPACITE, jetons + (now - dernier) * DEBIT_PAR_S) if dernier else CAPACITE
    if jetons < 1:
        _seaux[cle] = (jetons, now)
        raise erreurs.trop_de_requetes()
    _seaux[cle] = (jetons - 1, now)
