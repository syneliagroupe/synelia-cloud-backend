from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.modules.web_hebergement.service import construire_site, depot, depot_sites
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/web/sites", tags=["Web Cloud — applications web"])


@router.get("", response_model=m.WebSitesGetResponse, response_model_exclude_none=True)
async def lister_sites_web(
    page: Page,
    hebergementId: str | None = None,
    type: str | None = None,
    majEnAttente: bool | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803, PLR0917
    return await depot_sites.lister(
        ctx,
        page,
        filtre=lambda s: (
            (not hebergementId or s.hebergementId == hebergementId)
            and (not type or s.type == type)
            and (majEnAttente is None or (s.majEnAttente or 0) > 0 == majEnAttente)
        ),
        tri_defaut="hote",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def installer_site_web(
    corps: m.WebSitesPostRequest, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:
    hebergement = await depot.obtenir(ctx, corps.hebergementId)
    pris = await depot_sites.tous(
        ctx, filtre=lambda s: s.hote == corps.site.hote and s.statut != "suspendu"
    )
    if pris:
        raise erreurs.conflit(f"« {corps.site.hote} » porte déjà une application.")
    site = construire_site(
        ctx, corps.hebergementId, corps.site, preprod=bool(corps.site.preproduction)
    )
    await depot_sites.creer(ctx, site)
    await journaliser(
        ctx,
        action="site.installation",
        cible_type="web_site",
        cible_id=site.id,
        cible=site.hote,
        org_id=ctx.org_id,
    )
    return await demarrer_travail(
        ctx,
        "site.installer",
        site.hote,
        cible_type="web_site",
        cible_id=site.id,
        entree=corps.model_dump(mode="json"),
        contexte={"hebergement": hebergement.domaineProvisoire},
        etapes=[
            {"nom": "Installer l'application", "dureeS": 25},
            {"nom": "Créer la base de données", "dureeS": 10},
            {"nom": "Configurer le domaine et SSL", "dureeS": 12},
            {"nom": "Vérifier le rendu", "dureeS": 5},
        ],
    )


@router.get("/{siteId}", response_model=m.SiteWeb, response_model_exclude_none=True)
async def obtenir_site_web(siteId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    return await depot_sites.obtenir(ctx, siteId)


@router.patch("/{siteId}", response_model=m.SiteWeb, response_model_exclude_none=True)
async def modifier_site_web(
    siteId: str,
    corps: m.WebSitesSiteIdPatchRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot_sites.obtenir(ctx, siteId)
    await depot_sites.modifier(ctx, siteId, corps)
    await journaliser(
        ctx,
        action="site.modification",
        cible_type="web_site",
        cible_id=siteId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await depot_sites.obtenir(ctx, siteId)


@router.delete(
    "/{siteId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def supprimer_site_web(
    siteId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    s = await depot_sites.obtenir(ctx, siteId)
    exiger_confirmation(s.hote, confirmation)
    await journaliser(
        ctx, action="site.suppression", cible_type="web_site", cible_id=siteId, cible=s.hote
    )
    return await demarrer_travail(
        ctx,
        "site.supprimer",
        s.hote,
        cible_type="web_site",
        cible_id=siteId,
        etapes=[
            {"nom": "Supprimer les fichiers", "dureeS": 8},
            {"nom": "Supprimer la base associée", "dureeS": 6},
            {"nom": "Annuler le certificat SSL", "dureeS": 5},
        ],
    )


@router.post(
    "/{siteId}/analyse-securite",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def analyser_securite_site_web(
    siteId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    s = await depot_sites.obtenir(ctx, siteId)
    await journaliser(
        ctx, action="site.analyse_securite", cible_type="web_site", cible_id=siteId, cible=s.hote
    )
    return await demarrer_travail(
        ctx,
        "site.analyse_securite",
        s.hote,
        cible_type="web_site",
        cible_id=siteId,
        etapes=[
            {"nom": "Analyser les vulnérabilités", "dureeS": 20},
            {"nom": "Scanner les malwares", "dureeS": 15},
            {"nom": "Générer le rapport", "dureeS": 5},
        ],
    )


@router.post(
    "/{siteId}/mise-a-jour",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def mettre_a_jour_site_web(
    siteId: str,
    corps: m.WebSitesSiteIdMiseAJourPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_sites.obtenir(ctx, siteId)
    await journaliser(
        ctx,
        action="site.mise_a_jour",
        cible_type="web_site",
        cible_id=siteId,
        cible=s.hote,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "site.mise_a_jour",
        s.hote,
        cible_type="web_site",
        cible_id=siteId,
        etapes=[
            {"nom": "Effectuer une sauvegarde", "dureeS": 10},
            {"nom": "Mettre à jour le cœur et les extensions", "dureeS": 30},
            {"nom": "Vérifier la compatibilité", "dureeS": 8},
        ],
    )


@router.post(
    "/{siteId}/preproduction",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def creer_preproduction_site_web(
    siteId: str,
    corps: m.WebSitesSiteIdPreproductionPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_sites.obtenir(ctx, siteId)
    await journaliser(
        ctx,
        action="site.preproduction",
        cible_type="web_site",
        cible_id=siteId,
        cible=s.hote,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "site.preproduction",
        s.hote,
        cible_type="web_site",
        cible_id=siteId,
        etapes=[
            {"nom": "Cloner l'application", "dureeS": 20},
            {"nom": "Copier la base", "dureeS": 10},
            {"nom": "Configurer l'accès de préproduction", "dureeS": 6},
        ],
    )


@router.post(
    "/{siteId}/mise-en-production",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def publier_preproduction_site_web(
    siteId: str,
    corps: m.WebSitesSiteIdMiseEnProductionPostRequest,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_sites.obtenir(ctx, siteId)
    if s.preproduction is None:
        raise erreurs.conflit(
            "Aucune préproduction active pour ce site : créez-en une d'abord.",
            code="aucune_preproduction",
        )
    exiger_confirmation(s.hote, confirmation)
    await journaliser(
        ctx,
        action="site.mise_en_production",
        cible_type="web_site",
        cible_id=siteId,
        cible=s.hote,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await demarrer_travail(
        ctx,
        "site.mise_en_production",
        s.hote,
        cible_type="web_site",
        cible_id=siteId,
        etapes=[
            {"nom": "Basculer le trafic", "dureeS": 8},
            {"nom": "Synchroniser les données", "dureeS": 15},
            {"nom": "Désactiver la préproduction", "dureeS": 4},
        ],
    )


@router.get(
    "/{siteId}/mises-a-jour", response_model=list[m.MiseAJourSite], response_model_exclude_none=True
)
async def lister_mises_a_jour_site_web(
    siteId: str, securiteSeulement: bool | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot_sites.obtenir(ctx, siteId)
    return []
