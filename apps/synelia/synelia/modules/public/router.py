"""Fonctions de route de la vitrine publique (CtxPublic, sans authentification)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from synelia_contract import modeles as m
from synelia_kernel import erreurs
from synelia_kernel.argent import arrondi_fcfa, ttc
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id, slug_court

from synelia.audit import journaliser
from synelia.depot import Depot
from synelia.deps import CtxPublic, Page, pagine
from synelia.modules.admin.router import SITES_PHYSIQUES
from synelia.modules.admin.service import amacer_backends, statut_services_effectif
from synelia.modules.facturation.tarification import PRIX_UNITAIRES
from synelia.modules.public.service import (
    ETUDES_CAS,
    HYPOTHESES,
    PAGES_LEGALES,
    SLA_ENGAGEMENTS,
    catalogues,
    familles_tarifs,
    fiche_catalogue,
    souverainete,
)

router = APIRouter(prefix="/public", tags=["Vitrine publique"])

detenteur_contact = Depot("lead", m.Lead, plateforme=True)
detenteur_offres = Depot("offre", m.Offre, plateforme=True)
detenteur_incidents = Depot("incident", m.Incident, plateforme=True)

ACCUSES = {
    "contact": {
        "slug": "contact",
        "message": "Merci ! Un conseiller vous recontacte sous 24 h ouvrées.",
        "delaiReponseHeures": 24,
    },
    "devis": {
        "slug": "devis",
        "message": "Merci ! Votre devis est en cours de préparation.",
        "delaiReponseHeures": 48,
    },
}


async def _deposer_lead(
    ctx: Any,
    origine: str,
    contact: m.DemandeContact,
    configuration_simulee: m.EstimationCout | None = None,
) -> m.Lead:
    lead = m.Lead(
        id=nouvel_id(),
        recuLe=maintenant(),
        origine=origine,
        nom=contact.nom,
        email=contact.email,
        telephone=contact.telephone,
        organisation=contact.organisation,
        taille=contact.taille,
        secteur=contact.secteur,
        message=contact.message,
        configurationSimulee=configuration_simulee,
        statut="nouveau",
        notes=[],
    )
    await detenteur_contact.creer(ctx, lead)
    await journaliser(
        ctx,
        action=f"lead.{origine}",
        cible_type="lead",
        cible_id=lead.id,
        cible=contact.nom,
        org_id=None,
    )
    return lead


@router.get(
    "/catalogue/services",
    response_model=m.PublicCatalogueServicesGetResponse,
    response_model_exclude_none=True,
)
async def lister_catalogue_public(
    page: Page, ctx: CtxPublic, categorie: str | None = None, mode: str | None = None
) -> Any:
    data = [
        d
        for d in catalogues()
        if (not categorie or d["categorie"] == categorie) and (not mode or mode in d["modes"])
    ]
    return pagine(data, len(data), page)


@router.get(
    "/catalogue/services/{slug}", response_model=m.FicheCatalogue, response_model_exclude_none=True
)
async def obtenir_fiche_catalogue_publique(slug: str, ctx: CtxPublic) -> Any:
    f = fiche_catalogue(slug)
    if f is None:
        raise erreurs.introuvable("Service", slug)
    return f


@router.post(
    "/contact",
    response_model=m.AccuseReception,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def envoyer_demande_contact(corps: m.DemandeContact, ctx: CtxPublic) -> Any:
    lead = await _deposer_lead(ctx, "contact", corps)
    return {
        "reference": slug_court(lead.id),
        "message": ACCUSES["contact"]["message"],
        "delaiReponseHeures": 24,
    }


@router.get(
    "/couverture", response_model=m.PublicCouvertureGetResponse, response_model_exclude_none=True
)
async def obtenir_couverture(ctx: CtxPublic, ville: str | None = None) -> Any:
    # Aucune mesure de latence ni de fiabilité par ville n'est collectée : liste vide plutôt qu'inventée.
    return []


@router.get("/datacenters", response_model=list[m.Datacenter], response_model_exclude_none=True)
async def lister_datacenters(ctx: CtxPublic) -> Any:
    return SITES_PHYSIQUES


@router.get("/capacite")
async def capacite_par_site(ctx: CtxPublic) -> Any:
    """Capacité installée et charge moyenne par site, agrégées depuis les socles du back-office."""
    par_site: dict[str, list[Any]] = {}
    for b in await amacer_backends(ctx):
        par_site.setdefault(b.site, []).append(b)
    return [
        {
            "site": site,
            "vcpu": sum(b.capacite.vcpu for b in bs),
            "ramGo": sum(b.capacite.ramGo for b in bs),
            "stockageTo": round(sum(b.capacite.stockageTo for b in bs), 2),
            "chargePct": round(sum(b.usage.vcpuPct for b in bs) / len(bs)),
            "hotes": sum(b.hosts for b in bs),
            "socles": sorted({b.type for b in bs}),
        }
        for site, bs in par_site.items()
    ]


@router.post(
    "/devis",
    response_model=m.AccuseReception,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def envoyer_demande_devis(corps: m.DemandeDevis, ctx: CtxPublic) -> Any:
    lead = await _deposer_lead(ctx, "devis", corps.contact, configuration_simulee=corps.estimation)
    return {
        "reference": slug_court(lead.id),
        "message": ACCUSES["devis"]["message"],
        "delaiReponseHeures": 48,
    }


@router.get(
    "/disponibilite-domaine",
    response_model=m.DisponibiliteDomaine,
    response_model_exclude_none=True,
)
async def verifier_disponibilite_domaine_publique(nom: str, ctx: CtxPublic) -> Any:
    pris = nom.lower().strip() in {"google.com", "synelia.ci"}
    if not pris:
        from sqlalchemy import select
        from synelia_db.modeles import Ressource

        res = (
            await ctx.session.execute(
                select(Ressource).where(
                    Ressource.type == "web_domaine",
                    Ressource.supprime_le.is_(None),
                    Ressource.nom == nom,
                )
            )
        ).scalar_one_or_none()
        if res is not None:
            pris = True
    tld = nom.rsplit(".", 1)[-1] if "." in nom else "ci"
    prix = {"ci": 12000, "com": 9000, "net": 8000, "org": 8000, "fr": 10000}.get(tld, 9000)
    return {
        "nom": nom,
        "disponible": not pris,
        "prixAnnuel": None if pris else prix,
        "prixRenouvellement": None if pris else prix,
        "premium": tld in {"fr", "com"} and not pris,
        "registre": "ARDCI" if tld == "ci" else None,
        "whois": "Titulaire non communiqué" if pris else None,
        "suggestions": None
        if pris
        else [
            {"nom": f"{nom}-cloud", "prixAnnuel": prix},
            {"nom": f"{nom}-afrique", "prixAnnuel": prix},
        ],
    }


@router.get(
    "/etudes-cas", response_model=m.PublicEtudesCasGetResponse, response_model_exclude_none=True
)
async def lister_etudes_cas(page: Page, ctx: CtxPublic, secteur: str | None = None) -> Any:
    data = [e for e in ETUDES_CAS if (not secteur or e["secteur"] == secteur)]
    return pagine(data, len(data), page)


@router.get("/offres", response_model=m.PublicOffresGetResponse, response_model_exclude_none=True)
async def lister_offres_publiques(
    page: Page, ctx: CtxPublic, categorie: str | None = None, populaire: bool | None = None
) -> Any:
    dossiers = await detenteur_offres.tous(ctx, statut="publiee")
    data = [o.model_dump(mode="json") for o in dossiers]
    data = [
        o
        for o in data
        if (not categorie or o.get("categorie") == categorie)
        and (populaire is None or bool(o.get("populaire")) == populaire)
    ]
    return pagine(data, len(data), page)


@router.get("/offres/{slug}", response_model=m.FicheProduit, response_model_exclude_none=True)
async def obtenir_fiche_produit(slug: str, ctx: CtxPublic) -> Any:
    offres = await detenteur_offres.tous(ctx, statut="publiee")
    found = next((x for x in offres if slug in (x.code, x.id)), None)
    if found is None:
        raise erreurs.introuvable("Offre", slug)
    d = found.model_dump(mode="json")
    return {
        "slug": slug,
        "nom": d["nom"],
        "accroche": d["specs"],
        "description": d["specs"],
        "categorie": d["categorie"],
        "aPartirDe": d["prix"],
        "caracteristiques": list(d.get("caracteristiques") or []),
        "paliers": None,
        "sla": d.get("sla"),
        "faq": None,
    }


@router.get(
    "/pages-legales",
    response_model=m.PublicPagesLegalesGetResponse,
    response_model_exclude_none=True,
)
async def lister_pages_legales(ctx: CtxPublic) -> Any:
    return list(PAGES_LEGALES.values())


@router.get("/pages-legales/{slug}", response_model=m.PageLegale, response_model_exclude_none=True)
async def obtenir_page_legale(slug: str, ctx: CtxPublic) -> Any:
    page = PAGES_LEGALES.get(slug)
    if page is None:
        raise erreurs.introuvable("Page légale", slug)
    return page


async def _estimer(corps: m.PublicSimulateurPostRequest) -> m.EstimationCout:
    lignes: list[dict[str, Any]] = []
    vcpu = corps.vcpu or 0
    ram = corps.ramGo or 0
    stock = corps.stockageGo or 0
    pu = PRIX_UNITAIRES
    for libelle, qte, unite, prix, total in (
        (f"{vcpu} vCPU", vcpu, "mois", pu["vcpu_mois"], vcpu * pu["vcpu_mois"]),
        (f"{ram} Go RAM", ram, "mois", pu["ram_go_mois"], ram * pu["ram_go_mois"]),
        (
            f"{stock} Go stockage",
            stock / 100,
            "100 Go · mois",
            round(pu["stockage_go_mois"] * 100),
            stock * pu["stockage_go_mois"],
        ),
    ):
        if qte:
            lignes.append(
                {
                    "libelle": libelle,
                    "quantite": qte,
                    "unite": unite,
                    "prixUnitaire": prix,
                    "total": arrondi_fcfa(total),
                }
            )
    total_ht = sum(ligne["total"] for ligne in lignes)
    return m.EstimationCout(
        lignes=lignes,
        totalMensuel=ttc(total_ht),
        totalHoraire=round(total_ht / 730, 2),
        devise="XOF",
        engagement="aucun",
        avertissements=["Hors trafic sortant (egress) et licences tierces.", "TVA 18 % incluse."],
    )


@router.post("/simulateur", response_model=m.EstimationCout, response_model_exclude_none=True)
async def simuler_cout(corps: m.PublicSimulateurPostRequest, ctx: CtxPublic) -> Any:
    return await _estimer(corps)


@router.get("/sla", response_model=list[m.EngagementSla], response_model_exclude_none=True)
async def obtenir_sla_public(ctx: CtxPublic) -> Any:
    return SLA_ENGAGEMENTS


@router.get("/souverainete", response_model=m.Souverainete, response_model_exclude_none=True)
async def obtenir_souverainete(ctx: CtxPublic) -> Any:
    return souverainete(SITES_PHYSIQUES, await amacer_backends(ctx))


@router.get("/statut", response_model=m.PublicStatutGetResponse, response_model_exclude_none=True)
async def obtenir_statut_public(ctx: CtxPublic) -> Any:
    services = await statut_services_effectif(ctx)
    incidents = [i.model_dump(mode="json") for i in await detenteur_incidents.tous(ctx)]
    return {
        "services": services,
        "incidents": incidents,
        "disponibiliteGlobale90j": round(sum(s["uptime90j"] for s in services) / len(services), 2),
        "derniereMaj": maintenant().isoformat(),
    }


@router.get(
    "/statut/incidents/{incidentId}", response_model=m.Incident, response_model_exclude_none=True
)
async def obtenir_incident_public(incidentId: str, ctx: CtxPublic) -> Any:  # noqa: N803
    incident = await detenteur_incidents.obtenir(ctx, incidentId)
    return incident.model_dump(mode="json")


@router.get("/tarifs", response_model=m.PublicTarifsGetResponse, response_model_exclude_none=True)
async def obtenir_tarifs(ctx: CtxPublic) -> Any:
    par_categorie: dict[str, dict[str, Any]] = {}
    for o in sorted(await detenteur_offres.tous(ctx, statut="publiee"), key=lambda o: o.prix):
        fam = par_categorie.setdefault(
            o.categorie,
            {"code": o.categorie, "nom": o.categorie, "description": None, "offres": []},
        )
        fam["offres"].append(o.model_dump(mode="json"))
    return {
        "familles": [*par_categorie.values(), *familles_tarifs()],
        "tarifsUnitaires": {**PRIX_UNITAIRES, "tvaPct": 18},
        "hypotheses": HYPOTHESES,
    }
