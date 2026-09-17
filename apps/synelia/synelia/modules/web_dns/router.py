from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_dns import service
from synelia.modules.web_dns.service import depot

router = APIRouter(prefix="/web/dns", tags=["Web Cloud — domaines & DNS"])


@router.get("", response_model=m.WebDnsGetResponse, response_model_exclude_none=True)
async def lister_zones_dns(
    page: Page, dnssec: bool | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    return await depot.lister(
        ctx, page, filtre=lambda z: dnssec is None or z.dnssec == dnssec, tri_defaut="domaine"
    )


@router.post(
    "",
    response_model=m.ZoneDns,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_zone_dns(
    corps: m.WebDnsPostRequest, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:
    await depot.exiger_nom_libre(ctx, corps.domaine)
    zone = await service.creer_zone(ctx, corps.domaine)
    await journaliser(
        ctx,
        action="dns.zone_creation",
        cible_type="dns_zone",
        cible_id=zone.id,
        cible=corps.domaine,
    )
    return zone


@router.get("/modeles", response_model=list[m.ModeleDns], response_model_exclude_none=True)
async def lister_modeles_dns(ctx: Contexte = Depends(exige(None))) -> Any:
    return service.MODELES_DNS


@router.get("/{zoneId}", response_model=m.ZoneDns, response_model_exclude_none=True)
async def obtenir_zone_dns(zoneId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot.obtenir(ctx, zoneId)


@router.delete("/{zoneId}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def supprimer_zone_dns(
    zoneId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    z = await depot.obtenir(ctx, zoneId)
    exiger_confirmation(z.domaine, confirmation)
    zid = await service.zone_id_amont(ctx, z)
    await asyncio.to_thread(service.amont().supprimer_zone, zid)
    await depot.supprimer(ctx, zoneId, logique=True)
    await journaliser(
        ctx, action="dns.zone_suppression", cible_type="dns_zone", cible_id=zoneId, cible=z.domaine
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{zoneId}/dnssec", response_model=m.ZoneDns, response_model_exclude_none=True)
async def modifier_dnssec(
    zoneId: str,
    corps: m.WebDnsZoneIdDnssecPutRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    z = await depot.obtenir(ctx, zoneId)
    if z.dnssec == corps.actif:
        raise erreurs.conflit("DNSSEC est déjà dans cet état.", code="dnssec_etat_identique")
    zid = await service.zone_id_amont(ctx, z)
    await asyncio.to_thread(service.amont().activer_dnssec, zid, corps.actif)
    await depot.remplacer(ctx, zoneId, z.model_copy(update={"dnssec": corps.actif}))
    await journaliser(
        ctx,
        action="dns.dnssec",
        cible_type="dns_zone",
        cible_id=zoneId,
        details={"actif": corps.actif},
    )
    return await depot.obtenir(ctx, zoneId)


@router.post(
    "/{zoneId}/enregistrements",
    response_model=m.ZoneDns,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_enregistrement_dns(
    zoneId: str,
    corps: m.EnregistrementDnsCreation,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    z = await depot.obtenir(ctx, zoneId)
    service.verifier_non_duplique(z, corps)
    zone = await service.appliquer_enregistrements(ctx, zoneId, [corps])
    await journaliser(
        ctx, action="dns.enregistrement_creation", cible_type="dns_zone", cible_id=zoneId
    )
    return zone


@router.put("/{zoneId}/enregistrements", response_model=m.ZoneDns, response_model_exclude_none=True)
async def remplacer_enregistrements_dns(
    zoneId: str,
    corps: m.WebDnsZoneIdEnregistrementsPutRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    zone = await service.appliquer_enregistrements(
        ctx, zoneId, corps.enregistrements, remplacer=True
    )
    await journaliser(
        ctx, action="dns.enregistrements_remplacement", cible_type="dns_zone", cible_id=zoneId
    )
    return zone


@router.patch(
    "/{zoneId}/enregistrements/{enregistrementId}",
    response_model=m.ZoneDns,
    response_model_exclude_none=True,
)
async def modifier_enregistrement_dns(
    zoneId: str,
    enregistrementId: str,
    corps: m.EnregistrementDnsCreation,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    z = await depot.obtenir(ctx, zoneId)
    await service.modifier_enregistrement_amont(ctx, z, enregistrementId, corps)
    modifie = [
        service.enregistrement_vers(z, corps, enregistrementId) if r.id == enregistrementId else r
        for r in z.enregistrements
    ]
    await depot.remplacer(ctx, zoneId, z.model_copy(update={"enregistrements": modifie}))
    await journaliser(
        ctx, action="dns.enregistrement_modification", cible_type="dns_zone", cible_id=zoneId
    )
    return await depot.obtenir(ctx, zoneId)


@router.delete(
    "/{zoneId}/enregistrements/{enregistrementId}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def supprimer_enregistrement_dns(
    zoneId: str, enregistrementId: str, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    z = await depot.obtenir(ctx, zoneId)
    await service.supprimer_enregistrement_amont(ctx, z, enregistrementId)
    restants = [r for r in z.enregistrements if r.id != enregistrementId]
    await depot.remplacer(ctx, zoneId, z.model_copy(update={"enregistrements": restants}))
    await journaliser(
        ctx, action="dns.enregistrement_suppression", cible_type="dns_zone", cible_id=zoneId
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{zoneId}/modeles/{modeleId}", response_model=m.ZoneDns, response_model_exclude_none=True
)
async def appliquer_modele_dns(
    zoneId: str,
    modeleId: str,
    corps: m.WebDnsZoneIdModelesModeleIdPostRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    modele = next((mo for mo in service.MODELES_DNS if mo.id == modeleId), None)
    if modele is None:
        raise erreurs.introuvable("Modèle DNS", modeleId)
    remplacer = (
        corps.remplacerExistants
        if corps.remplacerExistants is not None
        else bool(modele.remplaceExistants)
    )
    zone = await service.appliquer_enregistrements(
        ctx, zoneId, modele.enregistrements, remplacer=remplacer
    )
    await journaliser(
        ctx, action="dns.modele_applique", cible_type="dns_zone", cible_id=zoneId, cible=modele.nom
    )
    return zone
