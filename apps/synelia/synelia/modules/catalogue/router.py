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
    # Ne pas utiliser (0.0, None) comme défaut : `time.monotonic()` est l'uptime, souvent
    # < 600 s sur un runner CI frais, ce qui renvoyait None sans jamais appeler la fabrique.
    entre = _cache.get(cle)
    if entre is None or time.monotonic() - entre[0] > 600:
        v = fabrique()
        _cache[cle] = (time.monotonic(), v)
        return v
    return entre[1]


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
