from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.espaces import service
from synelia.modules.espaces.service import depot
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/espaces", tags=["Espaces Cloud"])


@router.get("", response_model=m.EspacesGetResponse, response_model_exclude_none=True)
async def lister_espaces(
    page: Page,
    site: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    return await depot.lister(
        ctx,
        page,
        filtre=lambda e: (not site or e.site == site) and (not statut or e.statut == statut),
        tri_defaut="code",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_espace(
    corps: m.EspaceCloudCreation, ctx: Contexte = Depends(exige("espace.create"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.code)
    offres = Depot("offre", m.Offre, plateforme=True)
    offre = await offres.trouver(ctx, corps.offerId)
    espace = m.EspaceCloud(
        id=nouvel_id(),
        orgId=ctx.org_id,
        code=corps.code,
        offerId=corps.offerId,
        offreNom=offre.nom if offre else None,
        site=corps.site,
        cidr=corps.cidr,
        quota=corps.quota,
        usage=m.Quota(vcpu=0, ramGo=0, stockageTo=0),
        projets=0,
        statut="provisioning",
        createdAt=maintenant(),
        dnsInterne=corps.dnsInterne,
    )
    await depot.creer(ctx, espace)
    await journaliser(
        ctx, action="espace.creation", cible_type="espace", cible_id=espace.id, cible=espace.code
    )
    return await demarrer_travail(
        ctx,
        "espace.create",
        espace.code,
        cible_type="espace",
        cible_id=espace.id,
        entree=corps.model_dump(mode="json"),
    )


async def _espace(ctx: Contexte, espace_id: str) -> m.EspaceCloud:
    e = await depot.obtenir(ctx, espace_id)
    return e.model_copy(update={"usage": m.Quota(**await service.usage(ctx, espace_id))})


@router.get("/{espaceId}", response_model=m.EspaceCloud, response_model_exclude_none=True)
async def obtenir_espace(
    espaceId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await _espace(ctx, espaceId)


@router.patch("/{espaceId}", response_model=m.EspaceCloud, response_model_exclude_none=True)
async def modifier_espace(
    espaceId: str, corps: m.EspaceCloudModification, ctx: Contexte = Depends(exige("espace.create"))
) -> Any:  # noqa: N803
    e = await depot.obtenir(ctx, espaceId)
    if corps.code and corps.code != e.code:
        await depot.exiger_nom_libre(ctx, corps.code)
    await depot.modifier(ctx, espaceId, corps)
    await journaliser(
        ctx,
        action="espace.modification",
        cible_type="espace",
        cible_id=espaceId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await _espace(ctx, espaceId)


@router.delete(
    "/{espaceId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_espace(
    espaceId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("espace.create"))
) -> Any:  # noqa: N803
    e = await depot.obtenir(ctx, espaceId)
    exiger_confirmation(e.code, confirmation)
    if await Depot("vm", m.Vm).compter(ctx, parent_id=espaceId) or any(
        v.espaceId == espaceId for v in await Depot("vm", m.Vm).tous(ctx)
    ):
        raise erreurs.conflit("L'espace contient encore des machines.", code="espace_non_vide")
    await journaliser(
        ctx, action="espace.suppression", cible_type="espace", cible_id=espaceId, cible=e.code
    )
    return await demarrer_travail(
        ctx,
        "espace.delete",
        e.code,
        cible_type="espace",
        cible_id=espaceId,
        etapes=[
            {"nom": "Vérifier que l'espace est vide", "dureeS": 3},
            {"nom": "Libérer le réseau et le projet", "dureeS": 25},
            {"nom": "Clore la facturation", "dureeS": 4},
        ],
    )


@router.put("/{espaceId}/quota", response_model=m.EspaceCloud, response_model_exclude_none=True)
async def modifier_quota_espace(
    espaceId: str, corps: m.Quota, ctx: Contexte = Depends(exige("espace.quota.update"))
) -> Any:  # noqa: N803
    u = await service.usage(ctx, espaceId)
    if u["vcpu"] > corps.vcpu or u["ramGo"] > corps.ramGo or u["stockageTo"] > corps.stockageTo:
        raise erreurs.quota_depasse("L'usage actuel dépasse le nouveau quota.", detail=str(u))
    secrets = await depot.secrets(ctx, espaceId)
    projet_id = secrets.get("projet_id")
    if projet_id:
        # Sans cet appel, le quota n'est modifié que côté application : Nova/Cinder gardent
        # l'ancienne limite (constaté en direct — `openstack quota show` inchangé après un
        # PUT réussi), ce qui laisse le client croire à une augmentation qui n'existe pas
        # réellement côté amont.
        await asyncio.to_thread(
            service.amont().poser_quotas, projet_id, corps.vcpu, corps.ramGo, corps.stockageTo
        )
    await depot.modifier(ctx, espaceId, {"quota": corps.model_dump()})
    await journaliser(
        ctx,
        action="espace.quota",
        cible_type="espace",
        cible_id=espaceId,
        details=corps.model_dump(),
    )
    return await _espace(ctx, espaceId)


@router.get(
    "/{espaceId}/consommation", response_model=m.Consommation, response_model_exclude_none=True
)
async def obtenir_consommation_espace(
    espaceId: str, periode: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot.obtenir(ctx, espaceId)
    from synelia.modules.facturation import metrologie  # module optionnel

    return await metrologie.consommation(
        ctx, periode or maintenant().strftime("%Y-%m"), espace_id=espaceId
    )


@router.get("/{espaceId}/placements", response_model=list[m.Placement])
async def lister_placements_espace(espaceId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    await depot.obtenir(ctx, espaceId)
    return await Depot("placement", m.Placement).tous(ctx, filtre=lambda p: p.espaceId == espaceId)
