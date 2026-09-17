"""Catalogue IaaS : gabarits (flavors) et images système (Glance), cache 10 min."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from synelia_contract import modeles as m
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule

from synelia.deps import Ctx

router = APIRouter(prefix="/catalogue", tags=["Machines virtuelles"])
_cache: dict[str, tuple[float, Any]] = {}


def _memo(cle: str, fabrique):  # type: ignore[no-untyped-def]
    # L'absence d'entrée se teste sur le dictionnaire, jamais sur un horodatage sentinelle :
    # `time.monotonic()` compte depuis le démarrage de la machine, donc sur un hôte fraîchement
    # démarré (runner de CI) `monotonic() - 0.0 > 600` est faux et un sentinelle `0.0` renvoyait
    # la valeur vide sans jamais appeler l'amont (vécu en CI : `TypeError` sur un `None`).
    entree = _cache.get(cle)
    if entree is not None and time.monotonic() - entree[0] <= 600:
        return entree[1]
    valeur = fabrique()
    _cache[cle] = (time.monotonic(), valeur)
    return valeur


@router.get("/gabarits", response_model=list[m.Gabarit], response_model_exclude_none=True)
async def lister_gabarits(ctx: Ctx, site: str | None = None, famille: str | None = None) -> Any:
    gabarits = _memo("gabarits", fournisseur(ComputeSimule, ComputeOpenStack).gabarits)
    return [
        g
        for g in gabarits
        if (not famille or g["famille"] == famille)
        and (not site or site in (g.get("sitesDisponibles") or [site]))
    ]


@router.get("/images", response_model=list[m.ImageSysteme], response_model_exclude_none=True)
async def lister_images_systeme(
    ctx: Ctx, site: str | None = None, famille: str | None = None
) -> Any:
    images = _memo("images", fournisseur(ComputeSimule, ComputeOpenStack).images)
    return [
        i
        for i in images
        if (not famille or i["famille"] == famille) and (not site or site in i["sitesDisponibles"])
    ]
