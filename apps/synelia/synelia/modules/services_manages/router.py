"""Routeurs « Services managés » : catalogue hébergé + cycle de vie des souscriptions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_contract import workflows
from synelia_db.modeles import Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation, pagine
from synelia.modules.services_manages import service
from synelia.modules.services_manages.service import (
    _VERSION_PAR_DEFAUT,
    _cout,
    _palier,
    configuration,
    depot_export,
    depot_service,
    depot_siege,
    fiche,
    fiches,
    vers_utilisateur,
    versions,
)
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/services", tags=["Services managés"])
router_catalogue = APIRouter(prefix="/catalogue", tags=["Services managés"])


# ── catalogue hébergé ────────────────────────────────────────────────────────
@router_catalogue.get(
    "/services", response_model=m.CatalogueServicesGetResponse, response_model_exclude_none=True
)
async def lister_catalogue_services(
    page: Page,
    categorie: str | None = None,
    mode: str | None = None,
    certifie: bool | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:
    items = fiches()
    if categorie:
        items = [f for f in items if f["categorie"] == categorie]
    if mode:
        items = [f for f in items if mode in f["modes"]]
    if certifie is not None:
        items = [f for f in items if f["certifie"] == certifie]
    return pagine(items, len(items), page)


@router_catalogue.get(
    "/services-partages", response_model=list[m.FicheCatalogue], response_model_exclude_none=True
)
async def lister_catalogue_partage(ctx: Contexte = Depends(exige(None))) -> Any:
    return fiches()


@router_catalogue.get(
    "/services/{slug}", response_model=m.FicheCatalogue, response_model_exclude_none=True
)
async def obtenir_fiche_catalogue(slug: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    f = fiche(slug)
    if f is None:
        raise erreurs.introuvable("Fiche catalogue", slug)
    return f


@router_catalogue.get(
    "/services/{slug}/configuration",
    response_model=m.ConfigurationService,
    response_model_exclude_none=True,
)
async def obtenir_schema_configuration(slug: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    c = configuration(slug)
    if c is None:
        raise erreurs.introuvable("Configuration", slug)
    return c


@router_catalogue.get(
    "/contrat-integration", response_model=m.ContratIntegration, response_model_exclude_none=True
)
async def obtenir_contrat_integration(ctx: Contexte = Depends(exige(None))) -> Any:
    return {"capacites": workflows.marketplace()["contratIntegration"]}


# ── souscriptions ────────────────────────────────────────────────────────────
@router.get("", response_model=m.ServicesGetResponse, response_model_exclude_none=True)
async def lister_services_manages(  # noqa: PLR0917
    page: Page,
    catalogSlug: str | None = None,
    mode: str | None = None,
    site: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803, PLR0917
    return await depot_service.lister(
        ctx,
        page,
        filtre=lambda s: (
            (not catalogSlug or s.catalogSlug == catalogSlug)
            and (not mode or s.mode == mode)
            and (not site or s.site == site)
            and (not statut or s.statut == statut)
        ),
        tri_defaut="nom",
    )


@router.post(
    "",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def souscrire_service(
    corps: m.SouscriptionService, ctx: Contexte = Depends(exige("marketplace.subscribe"))
) -> Any:
    f = fiche(corps.catalogSlug)
    if f is None:
        raise erreurs.introuvable("Fiche catalogue", corps.catalogSlug)
    pal = _palier(corps.catalogSlug, corps.palier)
    if pal is None:
        raise erreurs.validation("Palier inconnu pour ce service.", {"palier": corps.palier})
    if corps.mode not in f["modes"]:
        raise erreurs.validation("Mode indisponible pour ce service.", {"mode": corps.mode})
    ident = nouvel_id()
    id8 = ident[:8]
    sieges = corps.sieges or 0
    version = corps.version or _VERSION_PAR_DEFAUT.get(corps.catalogSlug, "1.0")
    domaine = corps.domaine or f"{corps.catalogSlug}-{id8}.apps.synelia.cloud"
    s = m.ServiceManage(
        id=ident,
        orgId=ctx.org_id,
        catalogSlug=corps.catalogSlug,
        nom=corps.nom or f["nom"],
        mode=corps.mode,
        site=corps.site,
        palier=corps.palier,
        version=version,
        versionDisponible=None,
        domaine=domaine,
        urlNative=f"https://{corps.catalogSlug}-{id8}.apps.synelia.cloud",
        statut="provisioning",
        siegesSouscrits=sieges,
        siegesUtilises=0,
        sso=m.Sso(actif=bool(corps.sso), clientId=id8, groupMappings=[]),
        backupPlanId=corps.backupPlanId,
        derniereSauvegarde=None,
        uptime30j=0.0,
        parametres=corps.parametres or {},
        coutMensuel=_cout(pal, sieges),
        createdAt=maintenant(),
        certificat=None,
    )
    await depot_service.creer(ctx, s)
    await journaliser(
        ctx,
        action="service_manage.souscription",
        cible_type="service_manage",
        cible_id=ident,
        cible=s.nom,
    )
    etapes = workflows.marketplace()["tachesProvisioning"]
    return await demarrer_travail(
        ctx,
        "service_manage.subscribe",
        s.nom,
        cible_type="service_manage",
        cible_id=ident,
        entree=corps.model_dump(mode="json"),
        etapes=[{"nom": n, "dureeS": 5} for n in etapes],
    )


@router.get("/{serviceManageId}", response_model=m.ServiceManage, response_model_exclude_none=True)
async def obtenir_service_manage(serviceManageId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    return await service.vers_service(ctx, s)


@router.patch(
    "/{serviceManageId}", response_model=m.ServiceManage, response_model_exclude_none=True
)
async def modifier_service_manage(
    serviceManageId: str,
    corps: m.ServicesServiceManageIdPatchRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    await depot_service.modifier(ctx, serviceManageId, corps)
    await journaliser(
        ctx,
        action="service_manage.modification",
        cible_type="service_manage",
        cible_id=serviceManageId,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await service.vers_service(ctx, await depot_service.obtenir(ctx, serviceManageId))


@router.delete(
    "/{serviceManageId}",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def resilier_service_manage(
    serviceManageId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("marketplace.subscribe")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    exiger_confirmation(s.nom, confirmation)
    await journaliser(
        ctx,
        action="service_manage.resiliation",
        cible_type="service_manage",
        cible_id=serviceManageId,
        cible=s.nom,
    )
    return await demarrer_travail(
        ctx, "service_manage.resilier", s.nom, cible_type="service_manage", cible_id=serviceManageId
    )


@router.get(
    "/{serviceManageId}/configuration",
    response_model=m.ConfigurationService,
    response_model_exclude_none=True,
)
async def obtenir_configuration_service(
    serviceManageId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    c = configuration(s.catalogSlug)
    if c is None:
        raise erreurs.introuvable("Configuration", s.catalogSlug)
    return c


@router.put(
    "/{serviceManageId}/configuration",
    response_model=m.ServicesServiceManageIdConfigurationPutResponse,
    response_model_exclude_none=True,
)
async def modifier_configuration_service(
    serviceManageId: str,
    corps: m.MiseAJourConfiguration,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    cles = service._cles_configuration(s.catalogSlug)
    inconnues = [k for k in (corps.valeurs or {}) if k not in cles]
    if inconnues:
        raise erreurs.validation(
            "Clés de configuration inconnues.", {k: "commentaire" for k in inconnues}
        )
    cfg = configuration(s.catalogSlug)
    par_cle = {
        ch.get("cle"): ch for sec in (cfg or {}).get("sections", []) for ch in sec.get("champs", [])
    }
    effets = [
        {"cle": k, "effet": par_cle[k].get("effet", "immediat")}
        for k in (corps.valeurs or {})
        if k in cles
    ]
    parametres = {**s.parametres, **{k: v for k, v in (corps.valeurs or {}).items() if k in cles}}
    await depot_service.modifier(ctx, serviceManageId, {"parametres": parametres})
    await journaliser(
        ctx,
        action="service_manage.configuration",
        cible_type="service_manage",
        cible_id=serviceManageId,
        details=list(corps.valeurs or {}),
    )
    return {"configuration": cfg, "effets": effets, "travailId": None}


@router.post(
    "/{serviceManageId}/export",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def exporter_service_manage(
    serviceManageId: str,
    corps: m.ServicesServiceManageIdExportPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    exp = m.ExportService(
        id=nouvel_id(),
        serviceId=serviceManageId,
        format=corps.format or "zip",
        perimetre=corps.perimetre,
        demandeLe=maintenant(),
        statut="en_cours",
    )
    await depot_export.creer(ctx, exp, parent_id=serviceManageId)
    await journaliser(
        ctx,
        action="service_manage.export",
        cible_type="service_manage",
        cible_id=serviceManageId,
        cible=s.nom,
    )
    return await demarrer_travail(
        ctx,
        "service_manage.export",
        f"Export {s.nom}",
        cible_type="service_manage",
        cible_id=serviceManageId,
    )


@router.get(
    "/{serviceManageId}/exports",
    response_model=list[m.ExportService],
    response_model_exclude_none=True,
)
async def lister_exports_service(
    serviceManageId: str, ctx: Contexte = Depends(exige("service.admin"))
) -> Any:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    return await depot_export.tous(ctx, parent_id=serviceManageId)


@router.get(
    "/{serviceManageId}/metriques",
    response_model=m.ServicesServiceManageIdMetriquesGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_metriques_service(
    serviceManageId: str, fenetre: str = "24h", ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    f = fenetre if fenetre in {"24h", "7j", "30j"} else "24h"
    series = [
        {"metrique": "cpu", "unite": "%", "fenetre": f, "points": []},
        {"metrique": "ram", "unite": "Go", "fenetre": f, "points": []},
        {"metrique": "disque", "unite": "Go", "fenetre": f, "points": []},
        {"metrique": "reseau_entrant", "unite": "Mbit/s", "fenetre": f, "points": []},
    ]
    return {"tuiles": None, "series": series, "liens": None}


@router.post(
    "/{serviceManageId}/mise-a-jour",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def mettre_a_jour_service_manage(
    serviceManageId: str,
    corps: m.ServicesServiceManageIdMiseAJourPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    versions_ = versions(s.catalogSlug, s.version)
    dispos = [v for v in versions_ if v["statut"] == "disponible"]
    if not dispos or dispos[0]["version"] == s.version:
        raise erreurs.conflit("Le service est déjà à jour.", code="service_deja_a_jour")
    cible = corps.version or dispos[0]["version"]
    await journaliser(
        ctx,
        action="service_manage.mise_a_jour",
        cible_type="service_manage",
        cible_id=serviceManageId,
        cible=s.nom,
    )
    return await demarrer_travail(
        ctx,
        "service_manage.mise_a_jour",
        s.nom,
        cible_type="service_manage",
        cible_id=serviceManageId,
        contexte={"nouvelleVersion": cible},
    )


@router.post(
    "/{serviceManageId}/ouverture",
    response_model=m.OuvertureService,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ouvrir_service_manage(
    serviceManageId: str, ctx: Contexte = Depends(exige("service.open"))
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    url = f"{s.urlNative}/open?jeton={nouvel_id()}"
    await journaliser(
        ctx,
        action="service_manage.ouverture",
        cible_type="service_manage",
        cible_id=serviceManageId,
    )
    return m.OuvertureService(url=url, expire=maintenant(), methode="redirection")


@router.get(
    "/{serviceManageId}/sieges",
    response_model=m.ServicesServiceManageIdSiegesGetResponse,
    response_model_exclude_none=True,
)
async def lister_sieges(
    serviceManageId: str,
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige(None)),
) -> Any:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    return await depot_siege.lister(
        ctx,
        page,
        parent_id=serviceManageId,
        filtre=(lambda x: not statut or x.statut == statut),
        tri_defaut="userId",
    )


@router.post(
    "/{serviceManageId}/sieges",
    response_model=m.Siege,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def attribuer_siege(
    serviceManageId: str, corps: m.SiegeAttribution, ctx: Contexte = Depends(exige("seat.assign"))
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    pal = _palier(s.catalogSlug, s.palier)
    limit = pal.get("siegesMax") if pal else None
    if limit is not None and s.siegesSouscrits >= limit:
        raise erreurs.quota_depasse(
            "La limite de sièges du palier est atteinte.",
            detail=f"palier={s.palier} limite={limit}",
        )
    if await depot_siege.compter(ctx, parent_id=serviceManageId) >= s.siegesSouscrits:
        raise erreurs.quota_depasse(
            "Nombre de sièges souscrits atteint.", detail=f"souscrits={s.siegesSouscrits}"
        )
    existants = await depot_siege.tous(ctx, parent_id=serviceManageId)
    members = {x.userId for x in existants}
    if corps.userId in members:
        raise erreurs.conflit(
            "Un siège existe déjà pour cet utilisateur.", code="siege_deja_present"
        )
    u = await ctx.session.get(Utilisateur, corps.userId)
    siege = m.Siege(
        id=nouvel_id(),
        managedServiceId=serviceManageId,
        userId=corps.userId,
        utilisateur=vers_utilisateur(u) if u else None,
        statut="actif",
        quotaTotal=corps.quotaTotal,
    )
    await depot_siege.creer(ctx, siege, parent_id=serviceManageId)
    await journaliser(
        ctx,
        action="service_manage.siege_attribue",
        cible_type="siege",
        cible_id=siege.id,
        cible=corps.userId,
    )
    return siege


@router.patch(
    "/{serviceManageId}/sieges/{siegeId}", response_model=m.Siege, response_model_exclude_none=True
)
async def modifier_siege(
    serviceManageId: str,
    siegeId: str,
    corps: m.ServicesServiceManageIdSiegesSiegeIdPatchRequest,
    ctx: Contexte = Depends(exige("seat.assign")),
) -> Any:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    siege = await depot_siege.modifier(ctx, siegeId, corps, org_id=ctx.org_id)
    await journaliser(
        ctx, action="service_manage.siege_modifie", cible_type="siege", cible_id=siegeId
    )
    return siege


@router.delete("/{serviceManageId}/sieges/{siegeId}", status_code=status.HTTP_204_NO_CONTENT)
async def retirer_siege(
    serviceManageId: str, siegeId: str, ctx: Contexte = Depends(exige("seat.assign"))
) -> Response:  # noqa: N803
    await depot_service.obtenir(ctx, serviceManageId)
    await depot_siege.supprimer(ctx, siegeId, logique=True, org_id=ctx.org_id)
    await journaliser(
        ctx, action="service_manage.siege_retire", cible_type="siege", cible_id=siegeId
    )
    return Response(status_code=204)


@router.put(
    "/{serviceManageId}/sso", response_model=m.ServiceManage, response_model_exclude_none=True
)
async def modifier_sso_service(
    serviceManageId: str,
    corps: m.ServicesServiceManageIdSsoPutRequest,
    ctx: Contexte = Depends(exige("sso.configure")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    sso = m.Sso(
        actif=corps.actif,
        clientId=s.sso.clientId,
        groupMappings=corps.groupMappings or s.sso.groupMappings,
    )
    await depot_service.modifier(ctx, serviceManageId, {"sso": sso.model_dump()})
    await journaliser(
        ctx,
        action="service_manage.sso",
        cible_type="service_manage",
        cible_id=serviceManageId,
        details={"actif": corps.actif},
    )
    return await service.vers_service(ctx, await depot_service.obtenir(ctx, serviceManageId))


@router.get(
    "/{serviceManageId}/versions",
    response_model=list[m.VersionService],
    response_model_exclude_none=True,
)
async def lister_versions_service(
    serviceManageId: str, ctx: Contexte = Depends(exige(None))
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    return versions(s.catalogSlug, s.version)


@router.post(
    "/{serviceManageId}/versions/rollback",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def revenir_version_service(
    serviceManageId: str,
    corps: m.ServicesServiceManageIdVersionsRollbackPostRequest,
    ctx: Contexte = Depends(exige("service.admin")),
) -> Any:  # noqa: N803
    s = await depot_service.obtenir(ctx, serviceManageId)
    exiger_confirmation(s.nom, corps.confirmation)
    versions_ = versions(s.catalogSlug, s.version)
    courante = next((v for v in versions_ if v["statut"] == "courante"), None)
    if not courante or not courante.get("rollbackPossible"):
        raise erreurs.conflit(
            "Aucune version précédente à restaurer.", code="aucune_version_restauration"
        )
    await journaliser(
        ctx,
        action="service_manage.rollback",
        cible_type="service_manage",
        cible_id=serviceManageId,
        cible=s.nom,
    )
    return await demarrer_travail(
        ctx, "service_manage.rollback", s.nom, cible_type="service_manage", cible_id=serviceManageId
    )
