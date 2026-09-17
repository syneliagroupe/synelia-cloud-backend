"""RBAC, référentiels, onboarding, recherche."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import or_, select
from synelia_contract import modeles as m
from synelia_contract import rbac
from synelia_db.modeles import Organisation, Ressource

from synelia.audit import journaliser
from synelia.deps import Ctx, CtxPublic

router = APIRouter(tags=["Compte & organisation active"])

SITES = [
    {"code": "ABJ", "libelle": "Abidjan", "ville": "Abidjan"},
    {"code": "GBM", "libelle": "Grand-Bassam", "ville": "Grand-Bassam"},
]
SECTEURS = [
    "Banque & assurance",
    "Télécoms",
    "Administration",
    "Santé",
    "Éducation",
    "Commerce",
    "Industrie",
    "Technologie",
    "Médias",
    "Autre",
]
PAYS = [
    {"code": "CI", "nom": "Côte d'Ivoire", "indicatif": "+225"},
    {"code": "SN", "nom": "Sénégal", "indicatif": "+221"},
    {"code": "BF", "nom": "Burkina Faso", "indicatif": "+226"},
    {"code": "ML", "nom": "Mali", "indicatif": "+223"},
    {"code": "TG", "nom": "Togo", "indicatif": "+228"},
    {"code": "BJ", "nom": "Bénin", "indicatif": "+229"},
    {"code": "GN", "nom": "Guinée", "indicatif": "+224"},
    {"code": "CM", "nom": "Cameroun", "indicatif": "+237"},
    {"code": "FR", "nom": "France", "indicatif": "+33"},
]


@router.get("/rbac/matrice", response_model=list[m.ActionRbac])
async def obtenir_matrice_rbac(ctx: Ctx) -> Any:
    return rbac.matrice()


@router.get("/referentiels", response_model=m.Referentiels, response_model_exclude_none=True)
async def obtenir_referentiels(ctx: CtxPublic) -> Any:
    return {
        "pays": PAYS,
        "secteurs": SECTEURS,
        "taillesOrganisation": ["1-10", "11-50", "51-200", "201-1000", "1000+"],
        "sites": SITES,
        "devises": ["XOF", "EUR", "USD"],
        "roles": [{"code": r, "libelle": rbac.ROLE_LABEL[r]} for r in rbac.ROLES_CLIENT],
        "moyensPaiement": [
            {"code": "cinetpay", "libelle": "Mobile money & cartes (CinetPay)"},
            {"code": "stripe", "libelle": "Carte bancaire (EUR/USD)"},
            {"code": "virement", "libelle": "Virement bancaire"},
            {"code": "prepaye", "libelle": "Compte prépayé"},
        ],
    }


ETAPES_ONBOARDING = [
    ("organisation", "Compléter la fiche organisation", "/app/organisation", None, True),
    ("membres", "Inviter un premier membre", "/app/membres", "member.invite", False),
    ("espace", "Créer un Espace Cloud", "/app/espaces", "espace.create", True),
    ("vm", "Lancer une première machine", "/app/vms", "vm.create_delete", False),
    ("paiement", "Ajouter un moyen de paiement", "/app/facturation", "payment.update", True),
    ("mfa", "Activer le second facteur", "/app/compte", None, False),
]


async def _compter(ctx, type_: str) -> int:
    from synelia.depot import Depot

    return await Depot(type_, m.Vm).compter(ctx)


@router.get("/onboarding", response_model=m.Onboarding, response_model_exclude_none=True)
async def obtenir_onboarding(ctx: Ctx) -> Any:
    o = await ctx.session.get(Organisation, ctx.org_id)
    etat = (o.onboarding or {}) if o else {}
    faites = set(etat.get("faites", []))
    faites.add("organisation")
    if await _compter(ctx, "espace"):
        faites.add("espace")
    if await _compter(ctx, "vm"):
        faites.add("vm")
    if await _compter(ctx, "moyen_paiement"):
        faites.add("paiement")
    etapes = [
        {
            "cle": c,
            "libelle": lib,
            "faite": c in faites,
            "href": href,
            "actionRbac": act,
            "obligatoire": ob,
        }
        for c, lib, href, act, ob in ETAPES_ONBOARDING
    ]
    pct = round(100 * len([e for e in etapes if e["faite"]]) / len(etapes), 1)
    return {
        "termine": pct >= 100,
        "masque": bool(etat.get("masque")),
        "etapes": etapes,
        "pctComplete": pct,
    }


@router.patch("/onboarding", response_model=m.Onboarding, response_model_exclude_none=True)
async def modifier_onboarding(ctx: Ctx, corps: m.OnboardingPatchRequest) -> Any:
    o = await ctx.session.get(Organisation, ctx.org_id)
    assert o is not None
    etat = dict(o.onboarding or {})
    if corps.masque is not None:
        etat["masque"] = corps.masque
    if corps.etape:
        faites = set(etat.get("faites", []))
        (faites.add if corps.faite is not False else faites.discard)(corps.etape)
        etat["faites"] = sorted(faites)
    o.onboarding = etat
    await journaliser(
        ctx, action="onboarding.modification", cible_type="organisation", cible_id=ctx.org_id
    )
    return await obtenir_onboarding(ctx)


# Route réelle de chaque type de ressource côté frontend — la pluralisation
# naïve (`/app/{type}s/{id}`) ne tenait que par coïncidence pour `vm` et
# `espace` : la quasi-totalité des autres types renvoyaient un lien mort
# (ex. `k8s_cluster` → `/app/k8s_clusters/` au lieu de `/app/kubernetes/`).
# Un type sans fiche propre pointe vers la section qui le liste plutôt que de
# deviner une URL qui n'existe pas.
_HREF_PAR_TYPE: dict[str, str] = {
    "vm": "/app/vms/{id}",
    "espace": "/app/espaces/{id}",
    "k8s_cluster": "/app/kubernetes/{id}",
    "load_balancer": "/app/reseau/lb/{id}",
    "bucket": "/app/objet/{id}",
    "volume": "/app/stockage",
    "reseau": "/app/reseau",
    "groupe_securite": "/app/reseau",
    "base_managee": "/app/bases",
    "projet": "/app/applications/projets/{id}",
    "web_domaine": "/app/web/domaines/{id}",
    "web_site": "/app/web/applications/{id}",
    "web_hebergement": "/app/web/hebergement/{id}",
    "web_certificat": "/app/web/ssl/{id}",
    "web_drive": "/app/web/drive/{id}",
    "web_drive_siege": "/app/web/drive",
    "web_sauvegarde": "/app/web/backup/{id}",
    "dns_zone": "/app/web/domaines",
    "smtp_relais": "/app/smtp",
    "ticket": "/app/support/{id}",
    "facture": "/app/facturation",
    "ecriture": "/app/facturation",
    "agent_ia": "/app/ia/agents/{id}",
    "connaissance_ia": "/app/ia/connaissances/{id}",
    "flux_ia": "/app/ia/orchestration/{id}",
    "cle_ia": "/app/ia/parametres/passerelle",
    "environnement": "/app/applications/variables",
    "composant": "/app/applications/parametres",
    "application": "/app/applications/projets",
    "deploiement": "/app/applications/deploiements",
    "docs_progression": "/app/docs",
}


def _href_resultat(r: Ressource) -> str:
    """Lien réel de la fiche, ou de sa section à défaut de fiche propre."""
    if r.type == "projet_service" and r.parent_id:
        return f"/app/applications/projets/{r.parent_id}/{r.id}"
    if r.type == "vm_instantane" and r.parent_id:
        return f"/app/vms/{r.parent_id}"
    return _HREF_PAR_TYPE.get(r.type, f"/app/{r.type}s/{r.id}").format(id=r.id)


@router.get("/recherche", response_model=m.RechercheGetResponse, response_model_exclude_none=True)
async def rechercher(ctx: Ctx, q: str, types: str | None = None, limite: int = 20) -> Any:
    motif = f"%{q.lower()}%"
    req = select(Ressource).where(
        Ressource.org_id == ctx.org_id,
        Ressource.supprime_le.is_(None),
        or_(Ressource.nom.ilike(motif), Ressource.id == q),
    )
    if types:
        req = req.where(Ressource.type.in_(types.split(",")))
    lignes = list((await ctx.session.execute(req.limit(limite + 1))).scalars())
    champs = set(m.ResultatRecherche.model_fields)
    resultats = []
    for r in lignes[:limite]:
        d = {
            "id": r.id,
            "type": r.type,
            "libelle": r.nom or r.id,
            "href": _href_resultat(r),
            "statut": r.statut,
        }
        resultats.append({k: v for k, v in d.items() if k in champs})
    return {"resultats": resultats, "tronque": len(lignes) > limite}
