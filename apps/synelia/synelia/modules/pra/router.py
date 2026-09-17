from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.pra import service
from synelia.modules.pra.service import depot, exercices
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/pra", tags=["Sauvegarde & PRA"])


@router.get("", response_model=m.PraGetResponse, response_model_exclude_none=True)
async def lister_plans_pra(
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx, page, filtre=lambda p: not statut or p.statut == statut, tri_defaut="nom"
    )


@router.post(
    "",
    response_model=m.PlanPra,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_plan_pra(
    corps: m.PlanPraCreation, ctx: Contexte = Depends(exige("dr.failover.test"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.nom)
    plan = service.plan_vers_modele(corps, ctx)
    await depot.creer(ctx, plan)
    await journaliser(
        ctx, action="pra.plan.creation", cible_type="plan_pra", cible_id=plan.id, cible=plan.nom
    )
    return plan


@router.get("/{praId}", response_model=m.PlanPra, response_model_exclude_none=True)
async def obtenir_plan_pra(
    praId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, praId)
    return plan.model_copy(update={"exercices": await exercices.tous(ctx, parent_id=praId)})


@router.patch("/{praId}", response_model=m.PlanPra, response_model_exclude_none=True)
async def modifier_plan_pra(
    praId: str, corps: m.PlanPraCreation, ctx: Contexte = Depends(exige("dr.failover.test"))
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, praId)
    if corps.nom != plan.nom:
        await depot.exiger_nom_libre(ctx, corps.nom)
    service.controles_replication(corps.replication)
    # `GroupePra.dependances` is optional on the creation contract (clients — the mobile app's
    # PRA edit form included — routinely omit it) but the stored `Groupe` requires it: without
    # this default, `Depot.modifier`'s revalidation against `PlanPra` raised an unhandled
    # `ValidationError` (surfaced to the client as a bare 500), confirmed live 2026-09-08.
    # `plan_vers_modele` (used by `POST /pra`) already applies the same default on creation.
    corps = corps.model_copy(
        update={
            "groupes": [
                g.model_copy(update={"dependances": g.dependances or []}) for g in corps.groupes
            ]
        }
    )
    updated = await depot.modifier(ctx, praId, corps)
    await journaliser(
        ctx, action="pra.plan.modification", cible_type="plan_pra", cible_id=praId, cible=plan.nom
    )
    return updated


@router.delete("/{praId}", status_code=status.HTTP_204_NO_CONTENT)  # noqa: N803
async def supprimer_plan_pra(
    praId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("dr.failover.test"))
) -> Response:  # noqa: N803
    plan = await depot.obtenir(ctx, praId)
    exiger_confirmation(plan.nom, confirmation)
    await depot.supprimer(ctx, praId, logique=True)
    await journaliser(
        ctx, action="pra.plan.suppression", cible_type="plan_pra", cible_id=praId, cible=plan.nom
    )
    return Response(status_code=204)


@router.post(
    "/{praId}/bascule",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def basculer_pra(
    praId: str, corps: m.DemandeBascule, ctx: Contexte = Depends(exige("dr.failover.real"))
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, praId)
    if corps.type == "reel":
        exiger_confirmation(plan.nom, corps.confirmation)
    type_travail = "dr.failover.real" if corps.type == "reel" else "dr.failover.test"
    await journaliser(
        ctx,
        action="pra.bascule",
        cible_type="plan_pra",
        cible_id=praId,
        cible=plan.nom,
        details={"type": corps.type},
    )
    return await demarrer_travail(
        ctx,
        type_travail,
        plan.nom,
        cible_type="plan_pra",
        cible_id=plan.id,
        entree=corps.model_dump(mode="json", exclude_none=True),
    )


@router.get(
    "/{praId}/exercices", response_model=list[m.ExercicePra], response_model_exclude_none=True
)
async def lister_exercices_pra(praId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot.obtenir(ctx, praId)
    return await exercices.tous(ctx, parent_id=praId)


@router.post(
    "/{praId}/retour",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def revenir_site_source(
    praId: str,
    corps: m.PraPraIdRetourPostRequest,
    ctx: Contexte = Depends(exige("dr.failover.real")),
) -> Any:  # noqa: N803
    plan = await depot.obtenir(ctx, praId)
    exiger_confirmation(plan.nom, corps.confirmation)
    await journaliser(
        ctx, action="pra.retour", cible_type="plan_pra", cible_id=praId, cible=plan.nom
    )
    return await demarrer_travail(
        ctx,
        "dr.failover.retour",
        plan.nom,
        cible_type="plan_pra",
        cible_id=plan.id,
        entree=corps.model_dump(mode="json", exclude_none=True),
        etapes=[
            {"nom": "Figer les flux sur le site de repli", "dureeS": 30},
            {"nom": "Rejouer la réplication inverse", "dureeS": 240},
            {"nom": "Basculer les entrées DNS vers le site source", "dureeS": 40},
            {"nom": "Contrôler la cohérence finale", "dureeS": 35},
        ],
    )
