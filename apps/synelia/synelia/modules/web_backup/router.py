"""Sauvegarde (Web Cloud) : liste, exécution, restauration, test de restauration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige
from synelia.modules.web_backup import service
from synelia.modules.web_backup.service import depot
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/backup", tags=["Web Cloud — sauvegarde"])


@router.get("", response_model=m.WebBackupGetResponse, response_model_exclude_none=True)
async def lister_sauvegardes_web(
    page: Page,
    hebergementId: str | None = None,
    actif: bool | None = None,
    site: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda s: (
            (not hebergementId or s.hebergementId == hebergementId)
            and (actif is None or s.actif == actif)
            and (not site or s.site == site)
        ),
        tri_defaut="nomServi",
    )


@router.get("/{sauvegardeId}", response_model=m.SauvegardeWeb, response_model_exclude_none=True)
async def obtenir_sauvegarde_web(sauvegardeId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, sauvegardeId)


@router.patch("/{sauvegardeId}", response_model=m.SauvegardeWeb, response_model_exclude_none=True)
async def modifier_sauvegarde_web(
    sauvegardeId: str,
    corps: m.WebBackupSauvegardeIdPatchRequest,
    ctx: Contexte = Depends(exige("backup.plan.write")),
) -> Any:  # noqa: N803
    s = await depot.obtenir(ctx, sauvegardeId)
    modifs = {
        k: v for k, v in corps.model_dump(mode="json", exclude_unset=True).items() if v is not None
    }
    if modifs.get("perimetre"):
        modifs["perimetre"] = {
            **s.perimetre.model_dump(mode="json"),
            **{k: v for k, v in modifs["perimetre"].items() if v is not None},
        }
    await depot.modifier(ctx, sauvegardeId, modifs)
    await journaliser(
        ctx,
        action="web.sauvegarde.modification",
        cible_type="web_sauvegarde",
        cible_id=s.id,
        cible=s.nomServi,
        details={k: v for k, v in modifs.items() if v is not None},
    )
    return await depot.obtenir(ctx, sauvegardeId)


@router.post(
    "/{sauvegardeId}/execution",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def executer_sauvegarde_web(
    sauvegardeId: str, ctx: Contexte = Depends(exige("backup.plan.write"))
) -> Any:  # noqa: N803
    s = await depot.obtenir(ctx, sauvegardeId)
    await journaliser(
        ctx,
        action="web.sauvegarde.execution",
        cible_type="web_sauvegarde",
        cible_id=s.id,
        cible=s.nomServi,
    )
    return await demarrer_travail(
        ctx, "web.backup.run", s.nomServi, cible_type="web_sauvegarde", cible_id=s.id
    )


@router.post(
    "/{sauvegardeId}/restauration",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def restaurer_sauvegarde_web(
    sauvegardeId: str,
    corps: m.WebBackupSauvegardeIdRestaurationPostRequest,
    ctx: Contexte = Depends(exige("backup.restore")),
) -> Any:  # noqa: N803
    s = await depot.obtenir(ctx, sauvegardeId)
    if not s.executions:
        raise erreurs.conflit(
            "Aucun point de restauration disponible.", code="aucun_point_disponible"
        )
    await journaliser(
        ctx,
        action="web.sauvegarde.restauration",
        cible_type="web_sauvegarde",
        cible_id=s.id,
        cible=s.nomServi,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "web.backup.restore",
        s.nomServi,
        cible_type="web_sauvegarde",
        cible_id=s.id,
        entree=corps.model_dump(mode="json"),
    )


@router.post(
    "/{sauvegardeId}/test-restauration",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def tester_restauration_web(
    sauvegardeId: str, ctx: Contexte = Depends(exige("backup.restore"))
) -> Any:  # noqa: N803
    s = await depot.obtenir(ctx, sauvegardeId)
    await journaliser(
        ctx,
        action="web.sauvegarde.test_restauration",
        cible_type="web_sauvegarde",
        cible_id=s.id,
        cible=s.nomServi,
    )
    return await demarrer_travail(
        ctx,
        "web.backup.testrestauration",
        s.nomServi,
        cible_type="web_sauvegarde",
        cible_id=s.id,
        etapes=service.ETAPES_TEST_RESTAURATION,
    )
