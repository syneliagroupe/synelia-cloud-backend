"""Organisations : visibilité (admin plateforme vs. sa propre organisation), projection contrat, synthèse."""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

from sqlalchemy import func, select
from synelia_contract import modeles as m
from synelia_db.modeles import Membership, Organisation, Ressource
from synelia_kernel import erreurs
from synelia_kernel.dates import maintenant

from synelia.depot import Depot
from synelia.deps.contexte import Contexte

TICKETS_OUVERTS = ("ouvert", "en_cours", "attente_client")

TYPES_COMPTES = {
    "espaces": "espace",
    "vms": "vm",
    "clusters": "k8s_cluster",
    "servicesManages": "service_manage",
    "applications": "application",
}


def visible(ctx: Contexte, org_id: str) -> bool:
    p = ctx.principal
    return bool(p) and (p.est_admin_plateforme or org_id == ctx.org_id_ou_none)


async def obtenir(ctx: Contexte, org_id: str) -> Organisation:
    """404 pour toute organisation qui n'est pas la sienne (sauf équipe Synelia)."""
    o = await ctx.session.get(Organisation, org_id) if visible(ctx, org_id) else None
    if o is None:
        raise erreurs.introuvable("Organisation", org_id)
    return o


async def nb_utilisateurs(ctx: Contexte, org_id: str) -> int:
    q = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.org_id == org_id, Membership.scope_type == "org")
    )
    return int((await ctx.session.execute(q)).scalar_one())


async def vers_contrat(ctx: Contexte, o: Organisation) -> dict[str, Any]:
    return {
        "id": o.id,
        "nom": o.nom,
        "pays": o.pays or "CI",
        "secteur": o.secteur,
        "tva": o.tva,
        "statut": o.statut or "active",
        "logoUrl": o.logo_url,
        "createdAt": o.cree_le or maintenant(),
        "espaces": await Depot("espace", m.EspaceCloud).compter(ctx, org_id=o.id),
        "utilisateurs": await nb_utilisateurs(ctx, o.id),
        "tenantPlan": o.tenant_plan,
        "domaine": o.domaine,
    }


def contexte_pour(ctx: Contexte, org_id: str) -> Contexte:
    """Contexte vu depuis une autre organisation (équipe Synelia) pour les calculs scellés par `ctx.org_id`."""
    if ctx.org_id_ou_none == org_id or ctx.principal is None:
        return ctx
    return dataclasses.replace(ctx, principal=dataclasses.replace(ctx.principal, org_id=org_id))


async def impayes(ctx: Contexte, org_id: str) -> list[m.Impaye]:
    """Factures émises/impayées en retard d'échéance pour cette organisation — même règle
    que `/admin/facturation/impayes`, restreinte à `org_id` (les factures sont stockées à
    portée plateforme : le filtre par organisation se fait sur le champ `orgId`, pas la
    colonne `Ressource.org_id`)."""
    depot_f = Depot("facture", m.Facture, plateforme=True)
    aujourdhui = date.today()
    out = []
    for f in await depot_f.tous(ctx):
        if f.orgId != org_id or f.statut not in ("emise", "impayee") or not f.echeance:
            continue
        retard = (aujourdhui - f.echeance).days
        if retard < 0:
            continue
        out.append(
            m.Impaye(
                org=f.orgId,
                orgId=f.orgId,
                facture=f.numero,
                montant=f.total,
                echeance=f.echeance,
                relances=0,
                retardJours=retard,
                prochaineAction="mise_en_demeure" if retard >= 15 else "rappel",
            )
        )
    return out


async def tickets_organisation(ctx: Contexte, org_id: str) -> list[m.Ticket]:
    q = select(Ressource).where(Ressource.type == "ticket", Ressource.org_id == org_id)
    return [
        m.Ticket.model_validate(r.donnees)
        for r in (await ctx.session.execute(q)).scalars().all()
    ]


async def synthese(ctx: Contexte, org_id: str) -> dict[str, Any]:
    compteurs = {
        cle: await Depot(t, m.EspaceCloud).compter(ctx, org_id=org_id)
        for cle, t in TYPES_COMPTES.items()
    }
    espaces = await Depot("espace", m.EspaceCloud).tous(ctx, org_id=org_id)
    quota = {"vcpu": 0, "ramGo": 0, "stockageTo": 0.0}
    usage = {"vcpu": 0, "ramGo": 0, "stockageTo": 0.0}
    for e in espaces:
        for k in quota:
            quota[k] += getattr(e.quota, k, 0) or 0
            usage[k] += (getattr(e.usage, k, 0) or 0) if e.usage else 0
    from synelia.modules.facturation import metrologie  # module optionnel

    conso = await metrologie.consommation(
        contexte_pour(ctx, org_id), maintenant().strftime("%Y-%m")
    )
    return {
        **compteurs,
        "siegesUtilises": await nb_utilisateurs(ctx, org_id),
        "quota": quota,
        "usage": {**usage, "stockageTo": round(usage["stockageTo"], 2)},
        "depenseMois": int(conso.get("total", 0)),
        "previsionMois": int(conso.get("prevision", 0)),
        "depenseMoisPrecedent": int(conso.get("totalMoisPrecedent", 0)),
        "facturesEnAttente": len(await impayes(ctx, org_id)),
        "ticketsOuverts": sum(
            1 for t in await tickets_organisation(ctx, org_id) if t.statut in TICKETS_OUVERTS
        ),
    }
