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
from synelia.modules.public.service import (
    COUVERTURE,
    DATACENTERS,
    ETUDES_CAS,
    HYPOTHESES,
    PAGES_LEGALES,
    PRIX_UNITAIRES,
    SLA_ENGAGEMENTS,
    SOUVERAINETE,
    catalogues,
    familles_tarifs,
    fiche_catalogue,
)

router = APIRouter(prefix="/public", tags=["Vitrine publique"])

detenteur_contact = Depot("lead", m.Lead, plateforme=True)
detenteur_offres = Depot("offre", m.Offre, plateforme=True)
detenteur_statut = Depot("statut_service", m.StatutService, plateforme=True)
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
    return [c for c in COUVERTURE if (not ville or c["ville"] == ville)]


@router.get("/datacenters", response_model=list[m.Datacenter], response_model_exclude_none=True)
async def lister_datacenters(ctx: CtxPublic) -> Any:
    return DATACENTERS


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


_OFFRES_FALLBACK = [
    {
        "id": "offre-espace-standard",
        "code": "espace-standard",
        "nom": "Espace Cloud Standard",
        "categorie": "espace_cloud",
        "specs": "16 vCPU, 64 Go RAM, 2 To",
        "caracteristiques": ["16 vCPU", "64 Go RAM", "2 To stockage", "Réseau privé"],
        "prix": 150000,
        "populaire": True,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99,95 %",
    },
    {
        "id": "offre-k8s",
        "code": "k8s-standard",
        "nom": "Kubernetes managé",
        "categorie": "k8s",
        "specs": "3 nœuds, 6 vCPU, 12 Go",
        "caracteristiques": ["3 nœuds", "Autoscaling", "Ingress managé"],
        "prix": 95000,
        "populaire": False,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99,9 %",
    },
    {
        "id": "offre-web",
        "code": "web-standard",
        "nom": "Hébergement web managé",
        "categorie": "web",
        "specs": "1 site, 10 Go",
        "caracteristiques": ["1 site", "10 Go", "SSL inclus"],
        "prix": 12000,
        "populaire": False,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99,9 %",
    },
    {
        "id": "offre-stack",
        "code": "stack-dev",
        "nom": "Dev Stack",
        "categorie": "stack",
        "specs": "Git + CI/CD",
        "caracteristiques": ["Git", "CI/CD", "Registre"],
        "prix": 40000,
        "populaire": False,
        "statut": "publiee",
        "souscriptionsActives": 0,
        "sla": "99,9 %",
    },
    {
        "id": "offre-image",
        "code": "image-ubuntu",
        "nom": "Image Ubuntu 24.04",
        "categorie": "image_vm",
        "specs": "Image système",
        "caracteristiques": ["Linux", "Sécurisée"],
        "prix": 0,
        "populaire": False,
        "statut": "publiee",
        "souscriptionsActives": 0,
    },
]


@router.get("/offres", response_model=m.PublicOffresGetResponse, response_model_exclude_none=True)
async def lister_offres_publiques(
    page: Page, ctx: CtxPublic, categorie: str | None = None, populaire: bool | None = None
) -> Any:
    dossiers = await detenteur_offres.tous(ctx, statut="publiee")
    data = [o.model_dump(mode="json") for o in dossiers] or _OFFRES_FALLBACK
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
        found = next((x for x in _OFFRES_FALLBACK if x["code"] == slug), None)
    if found is None:
        raise erreurs.introuvable("Offre", slug)
    d = found if isinstance(found, dict) else found.model_dump(mode="json")
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
    if vcpu:
        ht = vcpu * PRIX_UNITAIRES["vcpu_heure"]
        lignes.append(
            {
                "libelle": f"{vcpu} vCPU",
                "quantite": vcpu,
                "unite": "heure",
                "prixUnitaire": PRIX_UNITAIRES["vcpu_heure"],
                "total": ht,
            }
        )
    if ram:
        ht = ram * PRIX_UNITAIRES["ram_go_heure"]
        lignes.append(
            {
                "libelle": f"{ram} Go RAM",
                "quantite": ram,
                "unite": "heure",
                "prixUnitaire": PRIX_UNITAIRES["ram_go_heure"],
                "total": ht,
            }
        )
    if stock:
        ht = arrondi_fcfa(stock / 1024 * PRIX_UNITAIRES["stockage_to_jour"])
        lignes.append(
            {
                "libelle": f"{stock} Go stockage",
                "quantite": round(stock / 1024, 2),
                "unite": "to/jour",
                "prixUnitaire": PRIX_UNITAIRES["stockage_to_jour"],
                "total": ht,
            }
        )
    vcpu_mois = vcpu * 24 * 30 * PRIX_UNITAIRES["vcpu_heure"]
    ram_mois = ram * 24 * 30 * PRIX_UNITAIRES["ram_go_heure"]
    stock_mois = arrondi_fcfa(stock / 1024 * PRIX_UNITAIRES["stockage_to_jour"] * 30)
    total_ht = vcpu_mois + ram_mois + stock_mois
    total_ttc = ttc(total_ht)
    return m.EstimationCout(
        lignes=lignes,
        totalMensuel=total_ttc,
        totalHoraire=vcpu * PRIX_UNITAIRES["vcpu_heure"] + ram * PRIX_UNITAIRES["ram_go_heure"],
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
    return SOUVERAINETE


@router.get("/statut", response_model=m.PublicStatutGetResponse, response_model_exclude_none=True)
async def obtenir_statut_public(ctx: CtxPublic) -> Any:
    services_ress = await detenteur_statut.tous(ctx)
    if services_ress:
        services = [s.model_dump(mode="json") for s in services_ress]
    else:
        services = [
            {
                "nom": "Compute (VMs)",
                "categorie": "compute",
                "etats": {"ABJ": "operationnel", "GBM": "operationnel"},
                "uptime90j": 99.98,
            },
            {
                "nom": "Stockage",
                "categorie": "storage",
                "etats": {"ABJ": "operationnel", "GBM": "operationnel"},
                "uptime90j": 99.99,
            },
            {
                "nom": "Réseau",
                "categorie": "network",
                "etats": {"ABJ": "operationnel", "GBM": "operationnel"},
                "uptime90j": 99.95,
            },
            {
                "nom": "Services managés",
                "categorie": "manages",
                "etats": {"ABJ": "operationnel", "GBM": "operationnel"},
                "uptime90j": 99.92,
            },
        ]
    incidents = [i.model_dump(mode="json") for i in await detenteur_incidents.tous(ctx)]
    return {
        "services": services,
        "incidents": incidents,
        "disponibiliteGlobale90j": 99.96,
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
    return {
        "familles": familles_tarifs(),
        "tarifsUnitaires": {**PRIX_UNITAIRES, "tvaPct": 18},
        "hypotheses": HYPOTHESES,
    }
