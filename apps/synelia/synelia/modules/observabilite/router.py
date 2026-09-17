from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_openstack import fournisseur
from synelia_openstack.victoria import VictoriaReel, VictoriaSimule

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.observabilite import service
from synelia.modules.observabilite.service import depot

router = APIRouter(prefix="/observabilite", tags=["Observabilité"])


def _victoria() -> VictoriaSimule:
    return fournisseur(VictoriaSimule, VictoriaReel)


@router.get(
    "/alertes", response_model=m.ObservabiliteAlertesGetResponse, response_model_exclude_none=True
)
async def lister_regles_alerte(
    page: Page,
    actif: bool | None = None,
    cible: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda r: (actif is None or r.actif == actif) and (not cible or r.cible == cible),
        tri_defaut="metrique",
    )


@router.post(
    "/alertes",
    response_model=m.RegleAlerte,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_regle_alerte(
    corps: m.RegleAlerteCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    regle = service.regle_vers_modele(corps, ctx)
    await depot.creer(ctx, regle)
    await journaliser(
        ctx,
        action="observabilite.alerte.creation",
        cible_type="regle_alerte",
        cible_id=regle.id,
        cible=regle.cible,
    )
    return regle


@router.get("/alertes/{alerteId}", response_model=m.RegleAlerte, response_model_exclude_none=True)
async def obtenir_regle_alerte(
    alerteId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, alerteId)


@router.patch("/alertes/{alerteId}", response_model=m.RegleAlerte, response_model_exclude_none=True)
async def modifier_regle_alerte(
    alerteId: str, corps: m.RegleAlerteCreation, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    regle = await depot.obtenir(ctx, alerteId)
    patch = {
        "cible": corps.cible,
        "metrique": corps.metrique,
        "seuil": corps.seuil,
        "canaux": corps.canaux,
    }
    if corps.plage is not None:
        patch["plage"] = corps.plage
    if corps.escalade is not None:
        patch["escalade"] = corps.escalade
    if corps.actif is not None:
        patch["actif"] = corps.actif
    updated = await depot.modifier(ctx, alerteId, patch)
    await journaliser(
        ctx,
        action="observabilite.alerte.modification",
        cible_type="regle_alerte",
        cible_id=alerteId,
        cible=regle.cible,
    )
    return updated


@router.delete("/alertes/{alerteId}", status_code=status.HTTP_204_NO_CONTENT)  # noqa: N803
async def supprimer_regle_alerte(
    alerteId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Response:  # noqa: N803
    regle = await depot.obtenir(ctx, alerteId)
    exiger_confirmation(regle.cible, confirmation)
    await depot.supprimer(ctx, alerteId, logique=True)
    await journaliser(
        ctx,
        action="observabilite.alerte.suppression",
        cible_type="regle_alerte",
        cible_id=alerteId,
        cible=regle.cible,
    )
    return Response(status_code=204)


@router.post(
    "/alertes/{alerteId}/test",
    response_model=m.ObservabiliteAlertesAlerteIdTestPostResponse,
    response_model_exclude_none=True,
)
async def tester_regle_alerte(alerteId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    regle = await depot.obtenir(ctx, alerteId)
    return {"envoye": True, "canaux": regle.canaux}


@router.get(
    "/evenements",
    response_model=m.ObservabiliteEvenementsGetResponse,
    response_model_exclude_none=True,
)
async def lister_evenements_supervision(  # noqa: PLR0913
    page: Page,
    gravite: str | None = None,
    ressourceId: str | None = None,  # noqa: N803
    site: str | None = None,
    depuis: datetime | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    from synelia.deps.pagination import filtrer_trier_paginer

    events = await service.evenements(ctx)
    if gravite:
        events = [e for e in events if e["gravite"] == gravite]
    if ressourceId:
        events = [e for e in events if ressourceId in e["ressource"]]
    return filtrer_trier_paginer(
        events, page, champs_recherche=("ressource", "message"), tri_defaut="ts"
    )


@router.get("/journaux", response_model=m.ExtraitLogs, response_model_exclude_none=True)
async def obtenir_journaux(
    ressourceId: str | None = None,
    niveau: str | None = None,
    depuis: datetime | None = None,
    recherche: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803
    lignes = _victoria().extrait_logs(
        ressourceId, niveau, depuis.isoformat() if depuis else None, recherche
    )
    return {
        "lignes": [m.LigneLog(**ligne) for ligne in lignes][:20],
        "tronque": len(lignes) > 20,
        "lienVictoriaLogs": _victoria().lien_logs(recherche or ressourceId),
    }


@router.get(
    "/metriques",
    response_model=m.ObservabiliteMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques(
    ressourceId: str | None = None,
    metriques: str | None = None,
    fenetre: str = "24h",
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803
    return service.metriques(fenetre, metriques.split(",") if metriques else None)
