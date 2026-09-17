"""Admin catalogue : offres, familles, fiches de service, modèles applicatifs + facturation plateforme."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import Page, exige_admin
from synelia.deps.contexte import Contexte
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.modules.facturation.service import CycleFacturation
from synelia.travaux import demarrer_travail

router = APIRouter(prefix="/admin", tags=["Super admin — produit"])

depot_fiche = Depot(
    "fiche_catalogue",
    m.FicheCatalogue,
    plateforme=True,
    champ_nom="slug",
    libelle="Fiche de catalogue",
)
depot_version = Depot(
    "version_service", m.VersionService, plateforme=True, libelle="Version de service"
)
depot_modele = Depot(
    "modele_applicatif",
    m.ModeleApplicatif,
    plateforme=True,
    champ_nom="slug",
    libelle="Modèle applicatif",
)
depot_offre = Depot("offre", m.Offre, plateforme=True, champ_nom="code", libelle="Offre")
depot_cycle = Depot(
    "cycle_facturation",
    CycleFacturation,
    plateforme=True,
    champ_nom="periode",
    libelle="Cycle de facturation",
)


@router.get(
    "/catalogue/familles",
    response_model=m.AdminCatalogueFamillesGetResponse,
    response_model_exclude_none=True,
)
async def lister_familles_catalogue(ctx: Contexte = Depends(exige_admin("catalog.edit"))) -> Any:
    offres = await depot_offre.tous(ctx)
    familles: dict[str, dict[str, Any]] = {}
    libelles = {
        "espace_cloud": "Espace Cloud",
        "image_vm": "Image & VM",
        "k8s": "Kubernetes",
        "stack": "Stack applicative",
        "web": "Web & hébergement",
    }
    for o in offres:
        f = familles.setdefault(
            o.categorie, {"offres": 0, "publiees": 0, "souscriptionsActives": 0}
        )
        f["offres"] += 1
        if o.statut == "publiee":
            f["publiees"] += 1
        f["souscriptionsActives"] += o.souscriptionsActives
    return [
        m.AdminCatalogueFamillesGetResponseItem(
            code=c,
            libelle=libelles.get(c, c),
            offres=f["offres"],
            publiees=f["publiees"],
            souscriptionsActives=f["souscriptionsActives"],
        )
        for c, f in familles.items()
    ]


@router.get(
    "/catalogue/offres",
    response_model=m.AdminCatalogueOffresGetResponse,
    response_model_exclude_none=True,
)
async def lister_offres(
    page: Page,
    categorie: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige_admin("catalog.edit")),
) -> Any:
    return await depot_offre.lister(
        ctx,
        page,
        filtre=lambda o: (
            (not categorie or o.categorie == categorie) and (not statut or o.statut == statut)
        ),
        tri_defaut="prix",
    )


@router.post(
    "/catalogue/offres",
    response_model=m.Offre,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_offre(
    corps: m.OffreCreation, ctx: Contexte = Depends(exige_admin("catalog.edit"))
) -> Any:
    await depot_offre.exiger_nom_libre(ctx, corps.code, org_id=None)
    offre = m.Offre(
        id=nouvel_id(),
        code=corps.code,
        nom=corps.nom,
        categorie=corps.categorie,
        specs=corps.specs,
        caracteristiques=corps.caracteristiques or [],
        prix=corps.prix,
        statut=corps.statut or "brouillon",
        souscriptionsActives=0,
        sla=corps.sla,
        surDevis=corps.surDevis,
        populaire=corps.populaire,
    )
    await depot_offre.creer(ctx, offre, org_id=None)
    await journaliser(
        ctx, action="offre.creation", cible_type="offre", cible_id=offre.id, cible=offre.code
    )
    return offre


@router.get("/catalogue/offres/{offreId}", response_model=m.Offre, response_model_exclude_none=True)
async def obtenir_offre(offreId: str, ctx: Contexte = Depends(exige_admin("catalog.edit"))) -> Any:  # noqa: N803
    return await depot_offre.obtenir(ctx, offreId, org_id=None)


@router.patch(
    "/catalogue/offres/{offreId}", response_model=m.Offre, response_model_exclude_none=True
)
async def modifier_offre(
    offreId: str, corps: m.OffreCreation, ctx: Contexte = Depends(exige_admin("catalog.edit"))
) -> Any:  # noqa: N803
    current = await depot_offre.obtenir(ctx, offreId, org_id=None)
    patch = corps.model_dump(mode="json", exclude_unset=True)
    if patch.get("code") and patch.get("code") != current.code:
        await depot_offre.exiger_nom_libre(ctx, patch["code"], org_id=None)
    await depot_offre.modifier(ctx, offreId, patch, org_id=None)
    await journaliser(ctx, action="offre.modification", cible_type="offre", cible_id=offreId)
    return await depot_offre.obtenir(ctx, offreId, org_id=None)


@router.delete("/catalogue/offres/{offreId}", status_code=status.HTTP_204_NO_CONTENT)
async def supprimer_offre(
    offreId: str, ctx: Contexte = Depends(exige_admin("catalog.edit"))
) -> Response:  # noqa: N803
    offre = await depot_offre.obtenir(ctx, offreId, org_id=None)
    if offre.statut != "brouillon":
        raise erreurs.conflit(
            "Une offre publiée ne se supprime pas, elle se déprécie — elle a été vendue. "
            "Seul un brouillon jamais souscrit se supprime.",
            code="offre_publiee",
        )
    if offre.souscriptionsActives > 0:
        raise erreurs.conflit(
            "Cette offre a des souscriptions actives, elle ne peut être supprimée.",
            code="offre_souscrite",
        )
    await depot_offre.supprimer(ctx, offreId, org_id=None, logique=True)
    await journaliser(
        ctx, action="offre.suppression", cible_type="offre", cible_id=offreId, cible=offre.code
    )
    return Response(status_code=204)


@router.post(
    "/catalogue/offres/{offreId}/publication",
    response_model=m.Offre,
    response_model_exclude_none=True,
)
async def publier_offre(
    offreId: str,
    corps: m.AdminCatalogueOffresOffreIdPublicationPostRequest,
    ctx: Contexte = Depends(exige_admin("catalog.edit")),
) -> Any:  # noqa: N803
    offre = await depot_offre.obtenir(ctx, offreId, org_id=None)
    if offre.souscriptionsActives > 0 and corps.statut == "depreciee":
        raise erreurs.conflit("Cette offre a des souscriptions actives.", code="offre_souscrite")
    await depot_offre.definir_statut(ctx, offreId, corps.statut, org_id=None)
    await journaliser(
        ctx,
        action="offre.publication",
        cible_type="offre",
        cible_id=offreId,
        details={"statut": corps.statut},
    )
    return await depot_offre.obtenir(ctx, offreId, org_id=None)


@router.put(
    "/catalogue/services/{slug}", response_model=m.FicheCatalogue, response_model_exclude_none=True
)
async def modifier_fiche_catalogue_service(
    slug: str, corps: m.FicheCatalogue, ctx: Contexte = Depends(exige_admin("catalog.edit"))
) -> Any:
    fichier = corps.model_copy(update={"slug": slug})
    if await depot_fiche.trouver(ctx, slug, org_id=None):
        await depot_fiche.remplacer(ctx, slug, fichier, org_id=None)
    else:
        await depot_fiche.creer(ctx, fichier, org_id=None, id_=slug)
    await journaliser(
        ctx, action="fiche_catalogue.maj", cible_type="fiche_catalogue", cible_id=slug
    )
    return fichier


@router.post(
    "/catalogue/services/{slug}/versions",
    response_model=m.VersionService,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def qualifier_version_service(
    slug: str,
    corps: m.AdminCatalogueServicesSlugVersionsPostRequest,
    ctx: Contexte = Depends(exige_admin("catalog.edit")),
) -> Any:
    fiche = await depot_fiche.trouver(ctx, slug, org_id=None)
    if fiche is None:
        raise erreurs.introuvable("Fiche de catalogue", slug)
    version = m.VersionService(
        version=corps.version,
        publieeLe=date.today(),
        statut=corps.statut or "disponible",
        notesUrl=corps.notesUrl,
        notes=corps.notes,
        rupture=corps.rupture,
        dureeIndisponibiliteMin=corps.dureeIndisponibiliteMin,
        rollbackPossible=corps.rollbackPossible,
    )
    await depot_version.creer(ctx, version, org_id=None)
    await journaliser(
        ctx,
        action="service.version",
        cible_type="fiche_catalogue",
        cible_id=slug,
        details={"version": corps.version},
    )
    return version


@router.put("/modeles/{slug}", response_model=m.ModeleApplicatif, response_model_exclude_none=True)
async def modifier_modele_applicatif(
    slug: str, corps: m.ModeleApplicatif, ctx: Contexte = Depends(exige_admin("catalog.edit"))
) -> Any:
    modele = corps.model_copy(update={"slug": slug})
    if await depot_modele.trouver(ctx, slug, org_id=None):
        await depot_modele.remplacer(ctx, slug, modele, org_id=None)
    else:
        await depot_modele.creer(ctx, modele, org_id=None, id_=slug)
    await journaliser(
        ctx, action="modele_applicatif.maj", cible_type="modele_applicatif", cible_id=slug
    )
    return modele


@router.post(
    "/facturation/cycle",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_cycle_facturation(
    corps: m.AdminFacturationCyclePostRequest, ctx: Contexte = Depends(exige_admin("invoice.view"))
) -> Any:
    if await depot_cycle.trouver(ctx, corps.periode, org_id=None):
        raise erreurs.conflit(
            "Un cycle de facturation pour cette période est déjà lancé.", code="cycle_deja_lance"
        )
    cycle = CycleFacturation(
        id=corps.periode,
        periode=corps.periode,
        lanceLe=maintenant(),
        statut="en_cours",
        organisations=0,
    )
    await depot_cycle.creer(ctx, cycle, org_id=None, id_=corps.periode)
    await journaliser(
        ctx,
        action="facturation.cycle.lancement",
        cible_type="cycle_facturation",
        cible_id=corps.periode,
    )
    return await demarrer_travail(
        ctx,
        "facturation.cycle",
        f"Cycle de facturation {corps.periode}",
        entree=corps.model_dump(mode="json"),
        contexte={"periode": corps.periode, "org_ids": corps.orgIds},
    )


@router.get(
    "/facturation/cycles",
    response_model=m.AdminFacturationCyclesGetResponse,
    response_model_exclude_none=True,
)
async def lister_cycles_facturation(
    page: Page,
    periode: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige_admin("invoice.view")),
) -> Any:
    return await depot_cycle.lister(
        ctx,
        page,
        filtre=lambda c: (
            (not periode or c.periode == periode) and (not statut or c.statut == statut)
        ),
        tri_defaut="periode",
    )


@router.get(
    "/facturation/factures",
    response_model=m.AdminFacturationFacturesGetResponse,
    response_model_exclude_none=True,
)
async def lister_factures_plateforme(
    page: Page,
    orgId: str | None = None,
    statut: str | None = None,
    periode: str | None = None,
    ctx: Contexte = Depends(exige_admin("invoice.view")),
) -> Any:  # noqa: N803
    depot_f = Depot("facture", m.Facture, plateforme=True)
    return await depot_f.lister(
        ctx,
        page,
        filtre=lambda f: (
            (not orgId or f.orgId == orgId)
            and (not statut or f.statut == statut)
            and (not periode or f.periode == periode)
        ),
        tri_defaut="numero",
    )


@router.get(
    "/facturation/impayes",
    response_model=m.AdminFacturationImpayesGetResponse,
    response_model_exclude_none=True,
)
async def lister_impayes(
    page: Page,
    retardMinJours: int | None = None,
    orgId: str | None = None,
    ctx: Contexte = Depends(exige_admin("invoice.view")),
) -> Any:  # noqa: N803
    depot_f = Depot("facture", m.Facture, plateforme=True)
    aujourdhui = date.today()
    impayes = []
    for f in await depot_f.tous(ctx):
        if orgId and f.orgId != orgId:
            continue
        if f.statut not in ("emise", "impayee") or not f.echeance:
            continue
        retard = (aujourdhui - f.echeance).days
        if retard < 0 or (retardMinJours is not None and retard < retardMinJours):
            continue
        impayes.append(
            m.Impaye(
                org=f.orgId,
                orgId=f.orgId,
                facture=f.numero,
                montant=f.total,
                echeance=f.echeance,
                retardJours=retard,
                relances=0,
                prochaineAction="mise_en_demeure" if retard >= 15 else "rappel",
            )
        )
    return filtrer_trier_paginer(impayes, page, tri_defaut="retardJours")


@router.post(
    "/facturation/impayes/relances",
    response_model=m.AdminFacturationImpayesRelancesPostResponse,
    response_model_exclude_none=True,
)
async def lancer_relances(
    corps: m.AdminFacturationImpayesRelancesPostRequest,
    ctx: Contexte = Depends(exige_admin("invoice.view")),
) -> Any:
    depot_f = Depot("facture", m.Facture, plateforme=True)
    envoyees = 0
    for facture_id in corps.factures:
        try:
            facture = await depot_f.obtenir(ctx, facture_id)
            # Increment relances counter
            await depot_f.modifier(ctx, facture_id, {"relances": facture.relances + 1})
            envoyees += 1
        except Exception:  # noqa: BLE001, S112
            # Silently skip factures that fail to update
            continue
    await journaliser(
        ctx,
        action="facturation.relances",
        cible_type="facturation",
        cible_id="impayes",
        details=corps.model_dump(mode="json"),
    )
    return {"envoyees": envoyees, "echecs": len(corps.factures) - envoyees}


@router.get("/facturation/marges", response_model=list[m.MargeBackend])
async def lister_marges_backends(ctx: Contexte = Depends(exige_admin("catalog.edit"))) -> Any:
    backends = [
        ("OpenStack Compute", "vm"),
        ("Ceph", "stockage"),
        ("Postgres", "base"),
        ("Kubernetes", "k8s"),
        ("Réseau", "reseau"),
    ]
    return [
        m.MargeBackend(backend=b, type=t, coutInfra=0, revenu=0, marge=0.0) for b, t in backends
    ]
