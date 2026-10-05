from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_domaines import service
from synelia.modules.web_domaines.service import depot
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/domaines", tags=["Web Cloud — domaines & DNS"])


class ParametresEntreeWeb(BaseModel):
    """DNS publics à viser pour un domaine externe (alignés sur `SYNELIA_DOMAINE_DNS_*`)."""

    dnsEntreeA: str | None = Field(default=None, description="Apex `@` — entrée edge IPv4")
    dnsEntreeWildcardCname: str | None = Field(
        default=None, description="Wildcard `*` — CNAME vers le vhost edge"
    )


@router.get(
    "/parametres-entree",
    response_model=ParametresEntreeWeb,
    response_model_exclude_none=True,
)
async def parametres_entree_web(ctx: Contexte = Depends(exige(None))) -> Any:
    r = ctx.reglages
    return ParametresEntreeWeb(
        dnsEntreeA=r.domaine_dns_entree_a,
        dnsEntreeWildcardCname=r.domaine_dns_entree_wildcard_cname,
    )


@router.get(
    "/disponibilite", response_model=m.DisponibiliteDomaine, response_model_exclude_none=True
)
async def verifier_disponibilite_domaine(
    nom: str, extensions: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    base = nom.rsplit(".", 1)[0] if "." in nom else nom
    liste = [
        e if e.startswith(".") else f".{e}"
        for e in (extensions.split(",") if extensions else [service.tld_de(nom) or "com"])
    ]
    candidats = [f"{base}{e}" for e in liste]
    dispo = []
    for c in candidats:
        deja_a_nous = await Depot("web_domaine", m.Domaine).par_nom(ctx, c) is not None
        if deja_a_nous:
            await service.appliquer_dns_entree_domaine(ctx, c)
        # `verifier()` interroge le vrai registrar (OVH) une fois `RegistrarOvh` branché — sans
        # cet appel, "disponible" ne voulait dire que « pas déjà chez nous en base », jamais
        # « achetable » : un domaine réellement pris ailleurs s'affichait comme libre.
        # Un nom déjà dans le compte registrar mais rattaché à aucune organisation se
        # revendique sans achat (le exécuteur `domaine.commander` ne commande pas un nom
        # déjà géré) : sans cela, les domaines de démonstration n'étaient pas sélectionnables.
        du_compte = not deja_a_nous and await asyncio.to_thread(
            service.amont().domaine_du_compte, c
        )
        pris = deja_a_nous or (
            not du_compte and not await asyncio.to_thread(service.amont().verifier, c)
        )
        dispo.append(
            {
                "nom": c,
                "disponible": not pris,
                "prixAnnuel": None
                if pris
                else service.prix_tld("." + c.rsplit(".", 1)[-1].lower()),
                "prixRenouvellement": None
                if pris
                else service.prix_tld("." + c.rsplit(".", 1)[-1].lower()),
                "registre": None if pris else "Synelia Registrar",
                "whois": "Synelia Cloud" if pris else None,
                "suggestions": None
                if not pris
                else [m.Suggestion(nom=f"{base}-synelia.com", prixAnnuel=service.prix_tld(".com"))],
            }
        )
    return dispo[0]


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def commander_domaine(
    corps: m.CommandeDomaine, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    await service.exiger_nom_libre_global(ctx, corps.nom)
    extension = service.tld_de(corps.nom)
    domaine = m.Domaine(
        id=nouvel_id(),
        orgId=ctx.org_id,
        nom=corps.nom,
        extension=extension.lstrip("."),
        expiration=service.expiration_dans(corps.dureeAnnees),
        renouvellementAuto=bool(corps.renouvellementAuto),
        whoisProtege=bool(corps.whoisProtege),
        verrouTransfert=True,
        zoneId=None,
        hebergementId=corps.attacherHebergementId,
        provisionnement="manuel",
    )
    await depot.creer(ctx, domaine)
    await journaliser(
        ctx,
        action="domaine.commande",
        cible_type="web_domaine",
        cible_id=domaine.id,
        cible=corps.nom,
    )
    return await demarrer_travail(
        ctx,
        "domaine.commander",
        corps.nom,
        cible_type="web_domaine",
        cible_id=domaine.id,
        entree=corps.model_dump(mode="json"),
    )


@router.get("", response_model=m.WebDomainesGetResponse, response_model_exclude_none=True)
async def lister_domaines(
    page: Page,
    extension: str | None = None,
    expireAvant: str | None = None,
    hebergementId: str | None = None,
    renouvellementAuto: bool | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803, PLR0917
    return await depot.lister(
        ctx,
        page,
        filtre=lambda d: (
            (not extension or d.extension == extension.lstrip("."))
            and (not hebergementId or d.hebergementId == hebergementId)
            and (renouvellementAuto is None or d.renouvellementAuto == renouvellementAuto)
        ),
        tri_defaut="nom",
    )


@router.post(
    "/transferts",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def transferer_domaine(
    corps: m.TransfertDomaine, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    await service.exiger_nom_libre_global(ctx, corps.nom)
    extension = service.tld_de(corps.nom)
    domaine = m.Domaine(
        id=nouvel_id(),
        orgId=ctx.org_id,
        nom=corps.nom,
        extension=extension.lstrip("."),
        expiration=service.expiration_dans(1),
        renouvellementAuto=bool(corps.renouvellementAuto),
        whoisProtege=True,
        verrouTransfert=True,
        provisionnement="manuel",
    )
    await depot.creer(ctx, domaine)
    await journaliser(
        ctx,
        action="domaine.transfert",
        cible_type="web_domaine",
        cible_id=domaine.id,
        cible=corps.nom,
    )
    return await demarrer_travail(
        ctx,
        "domaine.transferer",
        corps.nom,
        cible_type="web_domaine",
        cible_id=domaine.id,
        entree=corps.model_dump(mode="json"),
    )


@router.get(
    "/{domaineId}",
    response_model=m.WebDomainesDomaineIdGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_domaine(domaineId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    d = await depot.obtenir(ctx, domaineId)
    return {"domaine": d, **await service.agregats(ctx, d.nom)}


@router.patch("/{domaineId}", response_model=m.Domaine, response_model_exclude_none=True)
async def modifier_domaine(
    domaineId: str,
    corps: m.WebDomainesDomaineIdPatchRequest,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    await depot.modifier(ctx, domaineId, corps)
    await journaliser(
        ctx,
        action="domaine.modification",
        cible_type="web_domaine",
        cible_id=domaineId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot.obtenir(ctx, domaineId)


@router.post(
    "/{domaineId}/code-auth",
    response_model=m.WebDomainesDomaineIdCodeAuthPostResponse,
    response_model_exclude_none=True,
)
async def obtenir_code_auth_domaine(
    domaineId: str, ctx: Contexte = Depends(exige("network.manage"))
) -> Any:  # noqa: N803
    d = await depot.obtenir(ctx, domaineId)
    code = service.amont().code_auth(d.nom)
    await depot.definir_secrets(ctx, domaineId, {"code_auth": code["code"]})
    await journaliser(
        ctx, action="domaine.code_auth", cible_type="web_domaine", cible_id=domaineId, cible=d.nom
    )
    return {"code": code["code"], "expire": maintenant()}


@router.delete(
    "/{domaineId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def resilier_domaine(
    domaineId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("network.manage")),
) -> Any:  # noqa: N803
    d = await depot.obtenir(ctx, domaineId)
    exiger_confirmation(d.nom, confirmation)
    if d.hebergementId:
        raise erreurs.conflit(
            "Un hébergement est encore attaché à ce domaine. Détachez-le ou supprimez-le d’abord."
        )
    await journaliser(
        ctx,
        action="domaine.resiliation",
        cible_type="web_domaine",
        cible_id=domaineId,
        cible=d.nom,
    )
    return await demarrer_travail(
        ctx,
        "domaine.resilier",
        d.nom,
        cible_type="web_domaine",
        cible_id=domaineId,
        etapes=service.ETAPES_RESILIATION,
    )


@router.post(
    "/{domaineId}/renouvellement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def renouveler_domaine(
    domaineId: str,
    corps: m.WebDomainesDomaineIdRenouvellementPostRequest,
    ctx: Contexte = Depends(exige("marketplace.subscribe")),
) -> Any:  # noqa: N803
    d = await depot.obtenir(ctx, domaineId)
    await journaliser(
        ctx,
        action="domaine.renouvellement",
        cible_type="web_domaine",
        cible_id=domaineId,
        cible=d.nom,
        details=corps.model_dump(mode="json"),
    )
    return await demarrer_travail(
        ctx,
        "domaine.renouveler",
        d.nom,
        cible_type="web_domaine",
        cible_id=domaineId,
        entree=corps.model_dump(mode="json"),
    )
