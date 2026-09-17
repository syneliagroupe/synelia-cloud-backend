"""Facturation : exécuteur du cycle mensuel, grand-livre (écritures), démo."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from synelia_contract import modeles as m
from synelia_db.modeles import Membership, Organisation, Ressource, Travail, Utilisateur
from synelia_kernel import argent, courriel
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id

from synelia.demo import peupleur
from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.facturation import metrologie
from synelia.travaux import Executeur, executeur


class Ecriture(BaseModel):
    """Une écriture du grand-livre (crédit positif)."""

    id: str
    orgId: str
    libelle: str
    type: str
    montant: int


class CycleFacturation(BaseModel):
    id: str
    periode: str
    lanceLe: datetime
    statut: str
    organisations: int
    facturesEmises: int | None = None
    montantTotal: int | None = None
    echecs: list[dict[str, str]] | None = None


depot_ecriture = Depot("ecriture", Ecriture, champ_nom="libelle", libelle="Écriture")

depot_cycle = Depot(
    "cycle_facturation",
    CycleFacturation,
    plateforme=True,
    champ_nom="periode",
    libelle="Cycle de facturation",
)

# Offre plateforme (catalogue admin) : même dépôt que `admin_catalogue.router.depot_offre`
# (import différé impossible côté admin_catalogue → facturation, on redéclare la vue).
depot_offre = Depot("offre", m.Offre, plateforme=True, champ_nom="code", libelle="Offre")


def mois_precedent(periode: str) -> str:
    annee, mois = (int(x) for x in periode.split("-")[:2])
    return (date(annee, mois, 1) - timedelta(days=1)).strftime("%Y-%m")


async def solde_credit(ctx: Contexte) -> int:
    ecritures = await depot_ecriture.tous(ctx, filtre=lambda e: e.type == "credit")
    return sum(e.montant for e in ecritures)


async def crediter(ctx: Contexte, org_id: str, libelle: str, montant: int) -> None:
    e = Ecriture(id=nouvel_id(), orgId=org_id, libelle=libelle, type="credit", montant=montant)
    r = Ressource(
        id=e.id, org_id=org_id, type="ecriture", nom=libelle, donnees=e.model_dump(mode="json")
    )
    ctx.session.add(r)
    await ctx.session.flush()


async def prochain_numero(ctx: Contexte, annee: int) -> str:
    """Numéro séquentiel `SYN-{année}-{n:06d}` par année, toutes organisations confondues."""
    q = (
        select(func.count())
        .select_from(Ressource)
        .where(Ressource.type == "facture", Ressource.supprime_le.is_(None))
    )
    total = int((await ctx.session.execute(q)).scalar_one())
    return f"SYN-{annee}-{total + 1:06d}"


# Composant -> types de travaux (`Travail.cible_type`) qui le concernent. Sert à calculer
# un taux de réussite réel des opérations de l'organisation sur 30 jours, faute d'une source
# de mesure de disponibilité par composant : pas de nombre inventé, un vrai ratio succès/échec
# (ou l'engagement contractuel lui-même quand aucune opération n'a encore eu lieu).
_CIBLES_SLA: dict[str, tuple[str, ...]] = {
    "compute": ("vm", "espace", "k8s_cluster", "service_manage", "application"),
    "stockage": ("volume",),
    "reseau": ("load_balancer", "ip_flottante", "groupe_securite", "reseau", "dns_zone"),
}

_DISPO_CIBLE: dict[str, float] = {"compute": 99.9, "stockage": 99.9, "reseau": 99.9}


async def sla_engagements(ctx: Contexte) -> dict[str, Any]:
    depuis = maintenant() - timedelta(days=30)
    engagements: list[dict[str, Any]] = []
    for composant, cibles in _CIBLES_SLA.items():
        q = select(Travail.statut).where(
            Travail.org_id == ctx.org_id,
            Travail.cible_type.in_(cibles),
            Travail.started_at >= depuis,
            Travail.statut.in_(["done", "failed"]),
        )
        statuts = list((await ctx.session.execute(q)).scalars())
        dispo = _DISPO_CIBLE[composant]
        if statuts:
            constate = round(100 * (statuts.count("done") / len(statuts)), 2)
        else:
            # Aucune opération mesurée sur la période : pas d'incident constaté, donc
            # conformité à l'engagement plutôt qu'un chiffre fabriqué.
            constate = dispo
        engagements.append(
            {
                "composant": composant,
                "dispo": dispo,
                "constate": constate,
                "reponseCritique": 15,
                "resolutionCritique": 60,
            }
        )
    return {"engagements": engagements, "credits": []}


async def offre_souscrite(ctx: Contexte, org_id: str) -> m.Offre | None:
    """L'offre du catalogue à laquelle l'organisation est abonnée (`Organisation.tenant_plan`
    porte le `code` de l'offre), sinon `None` — pas d'abonnement, facture 100% à l'usage."""
    org = (
        await ctx.session.execute(select(Organisation).where(Organisation.id == org_id))
    ).scalar_one_or_none()
    if org is None or not org.tenant_plan:
        return None
    return await depot_offre.par_nom(ctx, org.tenant_plan, org_id=None)


async def construire_facture(ctx: Contexte, org_id: str, periode: str) -> dict[str, Any]:
    # Vu depuis une autre organisation (cycle plateforme sur plusieurs org) : la consommation
    # doit être celle de `org_id`, pas celle de l'organisation active dans `ctx`.
    from synelia.modules.organisations.service import contexte_pour  # évite un cycle d'imports

    ctx_org = contexte_pour(ctx, org_id)
    cons = await metrologie.consommation(ctx_org, periode)
    conso_total = int(cons["total"])

    numero = await prochain_numero(ctx, int(periode.split("-", maxsplit=1)[0]))
    offre = await offre_souscrite(ctx, org_id)

    lignes = []
    if offre is not None:
        lignes.append(
            {
                "libelle": f"Abonnement {offre.nom} ({periode})",
                "ref": offre.code,
                "quantite": 1,
                "pu": offre.prix,
                "total": offre.prix,
            }
        )
    lignes.append(
        {
            "libelle": f"Consommation {periode}",
            "ref": periode,
            "quantite": 1,
            "pu": conso_total,
            "total": conso_total,
        }
    )
    sous_total = sum(ligne["total"] for ligne in lignes)
    facture_id = nouvel_id()
    facture = {
        "id": facture_id,
        "orgId": org_id,
        "numero": numero,
        "periode": periode,
        "lignes": lignes,
        "sousTotal": sous_total,
        "tvaPct": float(argent.TVA_CI_PCT),
        "total": argent.ttc(sous_total),
        "devise": "XOF",
        "statut": "emise",
        # Bug réel corrigé : un `nouvel_id()` frais ici pointait vers un id inexistant, le
        # téléchargement PDF de toute facture générée par un cycle rendait 404.
        "pdfUrl": f"/v1/facturation/factures/{facture_id}/pdf",
        "echeance": (
            date(int(periode.split("-", maxsplit=1)[0]), int(periode.split("-")[1]), 1)
            + timedelta(days=31)
        ).isoformat(),
    }
    r = Ressource(
        id=facture["id"], org_id=org_id, type="facture", nom=numero, statut="emise", donnees=facture
    )
    ctx.session.add(r)
    await ctx.session.flush()
    await _notifier_facture_emise(ctx, org_id, facture)
    return facture


async def _notifier_facture_emise(ctx: Contexte, org_id: str, facture: dict[str, Any]) -> None:
    """Best-effort : prévient les org_admin par courriel qu'une nouvelle facture est disponible.
    Une panne d'envoi ne doit jamais faire échouer le cycle de facturation."""
    destinataires = (
        await ctx.session.execute(
            select(Utilisateur)
            .join(Membership, Membership.utilisateur_id == Utilisateur.id)
            .where(Membership.org_id == org_id, Membership.role == "org_admin")
        )
    ).scalars()
    for u in destinataires:
        await courriel.envoyer(
            u.email,
            f"Nouvelle facture {facture['numero']} — Synelia Cloud",
            f"Bonjour {u.nom},",
            [
                f"Votre facture {facture['numero']} pour la période {facture['periode']} "
                f"est disponible, d'un montant de {facture['total']} {facture['devise']}.",
                f"Échéance : {facture['echeance']}.",
            ],
            bouton_texte="Voir la facture",
            bouton_url=f"{ctx.reglages.url_frontend}/app/facturation/factures/{facture['id']}",
        )


@executeur("facturation.cycle")
class ExecuteurCycleFacturation(Executeur):
    async def terminer(self, ctx: Contexte, travail) -> None:
        periode = str(travail.contexte.get("periode") or "")
        org_ids = travail.contexte.get("org_ids") or None
        if not periode:
            return
        q = select(Organisation).where(Organisation.statut == "active")
        if org_ids:
            q = q.where(Organisation.id.in_(org_ids))
        orgs = (await ctx.session.execute(q)).scalars().all()
        previous = mois_precedent(periode)
        emises = 0
        montant_total = 0
        echecs: list[dict[str, str]] = []
        for org in orgs:
            try:
                facture = await construire_facture(ctx, org.id, previous)
            except Exception as exc:  # noqa: BLE001 — une organisation en échec ne bloque pas le cycle
                echecs.append({"orgId": org.id, "erreur": str(exc)})
                continue
            emises += 1
            montant_total += int(facture["total"])
        if await depot_cycle.trouver(ctx, periode):
            await depot_cycle.modifier(
                ctx,
                periode,
                {
                    "statut": "termine",
                    "organisations": len(orgs),
                    "facturesEmises": emises,
                    "montantTotal": montant_total,
                    "echecs": echecs,
                },
            )


@peupleur
async def demo(session, org: Organisation, admin: Utilisateur) -> None:
    offres = [
        {
            "id": "offre-standard",
            "code": "standard",
            "nom": "Espace Standard",
            "categorie": "espace_cloud",
            "specs": "4 vCPU · 16 Go · 500 Go",
            "caracteristiques": ["IPv4 publique", "Sauvegarde quotidienne"],
            "prix": 45000,
            "populaire": True,
            "statut": "publiee",
            "souscriptionsActives": 3,
            "sla": "99.9",
            "surDevis": False,
        },
        {
            "id": "offre-performance",
            "code": "performance",
            "nom": "Espace Performance",
            "categorie": "espace_cloud",
            "specs": "8 vCPU · 32 Go · 1 To",
            "caracteristiques": ["IPv4 publique", "Sauvegarde horaire"],
            "prix": 90000,
            "statut": "publiee",
            "souscriptionsActives": 1,
            "sla": "99.95",
        },
        {
            "id": "offre-vm",
            "code": "vm-t2",
            "nom": "VM t2.micro",
            "categorie": "image_vm",
            "specs": "1 vCPU · 2 Go · 20 Go",
            "caracteristiques": [],
            "prix": 8000,
            "statut": "publiee",
            "souscriptionsActives": 0,
        },
    ]
    for o in offres:
        session.add(
            Ressource(
                id=o["id"], org_id=None, type="offre", nom=o["nom"], statut=o["statut"], donnees=o
            )
        )

    facture = {
        "id": "facture-demo",
        "orgId": org.id,
        "numero": "SYN-2026-000001",
        "periode": "2026-08",
        "lignes": [
            {
                "libelle": "Consommation Espace Standard",
                "ref": "2026-08",
                "quantite": 1,
                "pu": 45000,
                "total": 45000,
            }
        ],
        "sousTotal": 45000,
        "tvaPct": 18,
        "total": 53100,
        "devise": "XOF",
        "statut": "emise",
        "pdfUrl": "/v1/facturation/factures/facture-demo/pdf",
        "echeance": "2026-09-15",
    }
    session.add(
        Ressource(
            id=facture["id"],
            org_id=org.id,
            type="facture",
            nom=facture["numero"],
            statut=facture["statut"],
            donnees=facture,
        )
    )
