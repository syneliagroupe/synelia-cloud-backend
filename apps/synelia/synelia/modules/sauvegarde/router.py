from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.sauvegarde import service
from synelia.modules.sauvegarde.service import depot, points, restaurations
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/sauvegarde", tags=["Sauvegarde & PRA"])


async def _ressources_protegees(ctx: Contexte, corps: m.PlanSauvegardeCreation) -> int:
    if corps.scope.type == "ressource":
        return 1
    vms = await Depot("vm", m.Vm).tous(ctx)
    if corps.scope.type == "espace":
        return sum(1 for v in vms if v.espaceId == corps.scope.valeur and v.statut != "error")
    return len([v for v in vms if v.statut != "error"])


@router.get("/plans", response_model=m.SauvegardePlansGetResponse, response_model_exclude_none=True)
async def lister_plans_sauvegarde(
    page: Page,
    frequence: str | None = None,
    dernierResultat: str | None = None,  # noqa: N803
    ressourceId: str | None = None,  # noqa: N803
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda p: (
            (not frequence or p.frequence == frequence)
            and (not dernierResultat or p.dernierResultat == dernierResultat)
            and (not ressourceId or p.scope.valeur == ressourceId)
        ),
        tri_defaut="nom",
    )


@router.post(
    "/plans",
    response_model=m.PlanSauvegarde,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_plan_sauvegarde(
    corps: m.PlanSauvegardeCreation, ctx: Contexte = Depends(exige("backup.plan.write"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.nom)
    plan = service.plan_vers_modele(corps, ctx, await _ressources_protegees(ctx, corps))
    await depot.creer(ctx, plan)
    await journaliser(
        ctx,
        action="sauvegarde.plan.creation",
        cible_type="plan_sauvegarde",
        cible_id=plan.id,
        cible=plan.nom,
    )
    return plan


@router.get("/plans/{planId}", response_model=m.PlanSauvegarde, response_model_exclude_none=True)
async def obtenir_plan_sauvegarde(
    planId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, planId)


@router.patch("/plans/{planId}", response_model=m.PlanSauvegarde, response_model_exclude_none=True)
async def modifier_plan_sauvegarde(
    planId: str,
    corps: m.PlanSauvegardeCreation,
    ctx: Contexte = Depends(exige("backup.plan.write")),
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, planId)
    if corps.nom != plan.nom:
        await depot.exiger_nom_libre(ctx, corps.nom)
    updated = await depot.modifier(ctx, planId, corps)
    await journaliser(
        ctx,
        action="sauvegarde.plan.modification",
        cible_type="plan_sauvegarde",
        cible_id=planId,
        cible=plan.nom,
    )
    return updated


@router.delete("/plans/{planId}", status_code=status.HTTP_204_NO_CONTENT)  # noqa: N803
async def supprimer_plan_sauvegarde(
    planId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("backup.plan.write")),
) -> Response:  # noqa: N803
    plan = await depot.obtenir(ctx, planId)
    exiger_confirmation(plan.nom, confirmation)
    await depot.supprimer(ctx, planId, logique=True)
    await journaliser(
        ctx,
        action="sauvegarde.plan.suppression",
        cible_type="plan_sauvegarde",
        cible_id=planId,
        cible=plan.nom,
    )
    return Response(status_code=204)


@router.post(
    "/plans/{planId}/execution",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def executer_plan_sauvegarde(
    planId: str, ctx: Contexte = Depends(exige("backup.plan.write"))
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, planId)
    await journaliser(
        ctx,
        action="sauvegarde.plan.execution",
        cible_type="plan_sauvegarde",
        cible_id=plan.id,
        cible=plan.nom,
    )
    return await demarrer_travail(
        ctx,
        "backup.run",
        plan.nom,
        cible_type="plan_sauvegarde",
        cible_id=plan.id,
        entree={},
        etapes=[
            {"nom": "Valider l'état du plan", "dureeS": 5},
            {"nom": "Créer le snapshot", "dureeS": 40},
            {"nom": "Répliquer vers la destination", "dureeS": 90},
            {"nom": "Contrôler l'intégrité", "dureeS": 12},
        ],
    )


@router.get(
    "/points", response_model=m.SauvegardePointsGetResponse, response_model_exclude_none=True
)
async def lister_points_restauration(
    page: Page,
    planId: str | None = None,  # noqa: N803
    ressourceId: str | None = None,  # noqa: N803
    immuables: bool | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    return await points.lister(
        ctx,
        page,
        filtre=lambda p: (
            (not planId or p.planId == planId)
            and (not ressourceId or p.resourceId == ressourceId)
            and (immuables is None or (p.immuableJusquau is not None) == immuables)
        ),
        tri_defaut="date",
    )


@router.delete("/points/{pointId}", status_code=status.HTTP_204_NO_CONTENT)  # noqa: N803
async def supprimer_point_restauration(
    pointId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("backup.plan.write")),
) -> Response:  # noqa: N803
    point = await points.obtenir(ctx, pointId)
    exiger_confirmation(point.resourceNom, confirmation)
    await service.supprimer_snapshots_reels(ctx, pointId)
    await points.supprimer(ctx, pointId, logique=True)
    await journaliser(
        ctx,
        action="sauvegarde.point.suppression",
        cible_type="point_restauration",
        cible_id=pointId,
        cible=point.resourceNom,
    )
    return Response(status_code=204)


@router.post(
    "/points/{pointId}/verification",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def verifier_point_restauration(
    pointId: str, ctx: Contexte = Depends(exige("backup.restore"))
) -> Any:  # noqa: N803
    point = await points.obtenir(ctx, pointId)
    await journaliser(
        ctx,
        action="sauvegarde.point.verification",
        cible_type="point_restauration",
        cible_id=point.id,
        cible=point.resourceNom,
    )
    return await demarrer_travail(
        ctx,
        "backup.verify",
        point.resourceNom,
        cible_type="point_restauration",
        cible_id=point.id,
        etapes=[
            {"nom": "Monter le point en lecture seule", "dureeS": 18},
            {"nom": "Comparer les blocs et le catalogue", "dureeS": 42},
            {"nom": "Solder le rapport de vérification", "dureeS": 5},
        ],
    )


@router.get(
    "/restaurations",
    response_model=m.SauvegardeRestaurationsGetResponse,
    response_model_exclude_none=True,
)
async def lister_restaurations(
    page: Page,
    statut: str | None = None,
    ressourceId: str | None = None,  # noqa: N803
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    return await restaurations.lister(
        ctx,
        page,
        filtre=lambda r: (
            (not statut or r.statut == statut)
            and (not ressourceId or r.ressourceNom == ressourceId)
        ),
        tri_defaut="demandeeLe",
    )


@router.post(
    "/restaurations",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_restauration(
    corps: m.DemandeRestauration, ctx: Contexte = Depends(exige("backup.restore"))
) -> Any:
    point = await points.obtenir(ctx, corps.pointId)
    if corps.cible == "origine" and corps.nomCible:
        exiger_confirmation(corps.nomCible, corps.confirmation or "")
    restauration = m.Restauration(
        id=nouvel_id(),
        pointId=point.id,
        ressourceNom=corps.nomCible or point.resourceNom,
        granularite=corps.granularite,
        cible=corps.cible,
        demandeePar=ctx.utilisateur_id,
        demandeeLe=maintenant(),
        statut="queued",
        elements=corps.chemins,
    )
    await restaurations.creer(ctx, restauration)
    await journaliser(
        ctx,
        action="sauvegarde.restauration",
        cible_type="restauration",
        cible_id=restauration.id,
        cible=restauration.ressourceNom,
    )
    return await demarrer_travail(
        ctx,
        "backup.restore",
        restauration.ressourceNom,
        cible_type="restauration",
        cible_id=restauration.id,
        entree=corps.model_dump(mode="json"),
    )


@router.get(
    "/restaurations/{restaurationId}",
    response_model=m.Restauration,
    response_model_exclude_none=True,
)
async def obtenir_restauration(restaurationId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await restaurations.obtenir(ctx, restaurationId)


@router.get(
    "/conformite",
    response_model=m.SauvegardeConformiteGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_conformite_sauvegarde(
    page: Page,
    protection: str | None = None,
    type: str | None = None,
    ctx: Contexte = Depends(exige("audit.view", lecture=True)),
) -> Any:  # noqa: N803
    lignes = await service.conformite(ctx)
    if protection:
        lignes = [ligne for ligne in lignes if ligne["protection"] == protection]
    if type:
        lignes = [ligne for ligne in lignes if ligne["type"] == type]
    from synelia.deps.pagination import filtrer_trier_paginer

    return filtrer_trier_paginer(
        lignes, page, champs_recherche=("ressourceNom",), tri_defaut="ressourceNom"
    )
