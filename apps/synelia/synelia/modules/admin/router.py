"""Routes /admin/** (hors catalogue, modeles, facturation) : pilotage Synelia."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, or_, select
from synelia_contract import modeles as m
from synelia_db.modeles import Audit, Membership, Organisation, Ressource, Travail, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.dates import depuis_iso, iso, maintenant
from synelia_kernel.ids import nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige_admin, exiger_confirmation
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.modules.admin import service
from synelia.modules.admin.service import (
    depot_backend,
    depot_campagne_maj,
    depot_campagne_migration,
    depot_fenetre,
    depot_incident,
    depot_lead,
    depot_placement,
    depot_statut_service,
)
from synelia.modules.espaces.service import depot as depot_espace
from synelia.modules.membres.router import membre_contrat
from synelia.modules.support.service import detenteur_tickets
from synelia.travaux import demarrer_travail, vers_contrat

router = APIRouter(prefix="/admin", tags=["Super admin — pilotage"])

# ── audit ────────────────────────────────────────────────────────────────────
_RESULTATS = {"succes": "ok", "ok": "ok", "refus": "refuse", "refuse": "refuse"}


def _resultat(r: str | None) -> str:
    return _RESULTATS.get(r or "", "erreur")


async def _vers_evenement(
    ctx: Contexte, a: Audit, org_nom: str | None, noms: dict[str, str]
) -> dict[str, Any]:
    det = a.details or {}
    acteur = a.acteur or "systeme"
    if acteur.startswith("cle:"):
        type_acteur = "api"
    elif a.acteur_id:
        type_acteur = "user"
    else:
        type_acteur = "systeme"
    role = (
        det.get("role")
        if det.get("role") in m.EvenementAudit.model_fields["role"].annotation.__args__
        else "org_admin"
    )
    detail = det.get("message") or det.get("motif")
    if detail is None and det:
        import json

        detail = json.dumps(det, ensure_ascii=False, default=str)[:1000]
    return {
        "id": a.id,
        "ts": a.date,
        "orgId": a.org_id,
        "orgNom": org_nom,
        "actor": {
            "id": a.acteur_id or acteur,
            "nom": noms.get(a.acteur_id or "", acteur),
            "email": acteur if "@" in acteur else None,
            "type": type_acteur,
        },
        "role": role,
        "scope": {"type": "org", "id": a.org_id, "label": org_nom or a.org_id or "plateforme"},
        "action": a.action,
        "target": a.cible or (f"{a.cible_type}:{a.cible_id}" if a.cible_type else a.action),
        "result": _resultat(a.resultat),
        "detail": detail,
        "ip": a.ip,
    }


@router.get("/audit", response_model=m.AdminAuditGetResponse, response_model_exclude_none=True)
async def lister_audit_plateforme(  # noqa: PLR0913,PLR0917
    page: Page,
    orgId: str | None = None,  # noqa: N803
    acteur: str | None = None,
    action: str | None = None,
    resultat: str | None = None,
    depuis: str | None = None,
    jusqua: str | None = None,
    ctx: Contexte = Depends(exige_admin("audit.view")),
) -> Any:
    q = select(Audit)
    if orgId:
        q = q.where(Audit.org_id == orgId)
    if depuis:
        q = q.where(Audit.date >= service.utc(depuis_iso(depuis)))
    if action:
        q = q.where(Audit.action.ilike(f"%{action}%"))
    if acteur:
        q = q.where(or_(Audit.acteur_id == acteur, Audit.acteur.ilike(f"%{acteur}%")))
    if resultat:
        q = q.where(
            Audit.resultat.in_(
                {"ok": ("succes", "ok"), "refuse": ("refus", "refuse")}.get(resultat, ("__none__",))
            )
        )
    if jusqua:
        q = q.where(Audit.date <= service.utc(depuis_iso(jusqua)))
    lignes = list((await ctx.session.execute(q.order_by(Audit.date.desc()))).scalars().all())
    ids = {a.acteur_id for a in lignes if a.acteur_id}
    noms: dict[str, str] = {}
    if ids:
        noms = {
            u.id: u.nom
            for u in (
                await ctx.session.execute(select(Utilisateur).where(Utilisateur.id.in_(ids)))
            ).scalars()
        }
    orgs = {o.id: o.nom for o in (await ctx.session.execute(select(Organisation))).scalars()}
    evenements = [await _vers_evenement(ctx, a, orgs.get(a.org_id), noms) for a in lignes]
    return filtrer_trier_paginer(
        evenements, page, champs_recherche=("action", "target", "detail", "acteur", "orgNom")
    )


# ── backends ─────────────────────────────────────────────────────────────────
def _backend_usage(b: m.Backend, usage: dict[str, float]) -> m.Backend:
    cap = b.capacite
    pct_v = min(100.0, round(100 * usage["vcpu"] / cap.vcpu, 1)) if cap.vcpu else 0.0
    pct_r = min(100.0, round(100 * usage["ramGo"] / cap.ramGo, 1)) if cap.ramGo else 0.0
    pct_s = (
        min(100.0, round(100 * usage["stockageTo"] / cap.stockageTo, 1)) if cap.stockageTo else 0.0
    )
    return b.model_copy(
        update={
            "usage": m.Usage(vcpuPct=pct_v, ramPct=pct_r, stockagePct=pct_s),
            "saturation": m.Saturation(j30=pct_v, j60=pct_v, j90=pct_v),
        }
    )


@router.get(
    "/backends", response_model=m.AdminBackendsGetResponse, response_model_exclude_none=True
)
async def lister_backends(  # noqa: PLR0917
    page: Page,
    type: str | None = None,
    site: str | None = None,
    statut: str | None = None,
    enSortie: bool | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),  # noqa: A002,N803
) -> Any:
    backends = await service.amacer_backends(ctx)
    usage = await service.usage_plateforme(ctx)
    items = [
        _backend_usage(b, usage)
        for b in backends
        if (not type or b.type == type)
        and (not site or b.site == site)
        and (not statut or b.statut == statut)
        and (enSortie is None or (b.enSortie is not None and b.enSortie.actif) == enSortie)
    ]
    return filtrer_trier_paginer(items, page, champs_recherche=("code",), tri_defaut="code")


@router.get("/backends/{backendId}", response_model=m.Backend, response_model_exclude_none=True)
async def obtenir_backend(
    backendId: str, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:  # noqa: N803
    b = await depot_backend.obtenir(ctx, backendId)
    return _backend_usage(b, await service.usage_plateforme(ctx))


@router.patch("/backends/{backendId}", response_model=m.Backend, response_model_exclude_none=True)
async def modifier_backend(
    backendId: str,
    corps: m.AdminBackendsBackendIdPatchRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:  # noqa: N803
    b = await depot_backend.modifier(ctx, backendId, corps)
    await journaliser(
        ctx,
        action="backend.modification",
        cible_type="backend",
        cible_id=backendId,
        cible=b.code,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return _backend_usage(b, await service.usage_plateforme(ctx))


# ── capacite ─────────────────────────────────────────────────────────────────
@router.get("/capacite", response_model=m.Capacite, response_model_exclude_none=True)
async def obtenir_capacite(
    site: str | None = None, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:
    backends = await service.amacer_backends(ctx)
    usage = await service.usage_plateforme(ctx)
    if site:
        backends = [b for b in backends if b.site == site]
    backends_enrichis = [_backend_usage(b, usage) for b in backends]
    placements = await depot_placement.tous(ctx)
    projection = [
        m.ProjectionItem(backendId=b.backendId, j30=b.percent, j60=b.percent, j90=b.percent)
        for b in placements
        if any(x.id == b.backendId for x in backends)
    ]
    par_site = []
    for c in ("ABJ", "GBM"):
        s_backends = [b for b in backends if b.site == c]
        if not s_backends:
            continue
        cap = m.Quota(
            vcpu=sum(x.capacite.vcpu for x in s_backends),
            ramGo=sum(x.capacite.ramGo for x in s_backends),
            stockageTo=sum(x.capacite.stockageTo for x in s_backends),
        )
        n = len(s_backends) or 1
        ut = m.Quota(
            vcpu=int(usage["vcpu"] / n),
            ramGo=int(usage["ramGo"] / n),
            stockageTo=round(usage["stockageTo"] / n, 2),
        )
        par_site.append(m.CapaciteParSiteItem(site=c, capacite=cap, utilise=ut))
    return {
        "backends": backends_enrichis,
        "placements": placements,
        "projection": projection,
        "capaciteParSite": par_site or None,
    }


@router.get("/espaces", response_model=list[m.EspaceCloud], response_model_exclude_none=True)
async def lister_espaces_plateforme(ctx: Contexte = Depends(exige_admin("capacity.manage"))) -> Any:
    """Espaces Cloud « plateforme » (`org_id` NULL) : réseau + load balancer partagés,
    jamais visibles depuis `/espaces` (portée client) — ex. la zone VPS partagée de
    `web_hebergement`, cf. `espaces.service.semer_zone_vps`. Seul point d'accès pour les
    voir/piloter depuis l'équipe Synelia."""
    lignes = await service.lignes_type(ctx, "espace")
    return [m.EspaceCloud.model_validate(r.donnees) for r in lignes if r.org_id is None]


# ── conformité ───────────────────────────────────────────────────────────────
REFERENTIELS = [
    {"nom": "ISO 27001", "statut": "partiel", "ecarts": 2},
    {"nom": "SOC 2 Type II", "statut": "conforme", "ecarts": 0},
    {
        "nom": "Réglement général sur la protection des données (RGPD)",
        "statut": "partiel",
        "ecarts": 1,
    },
    {"nom": "HDS", "statut": "non_conforme", "ecarts": 3},
    {"nom": "SWIFT CSP", "statut": "conforme", "ecarts": 0},
    {"nom": "PCI DSS", "statut": "partiel", "ecarts": 4},
]


@router.get(
    "/conformite", response_model=m.AdminConformiteGetResponse, response_model_exclude_none=True
)
async def obtenir_conformite_plateforme(
    ctx: Contexte = Depends(exige_admin("compliance.export")),
) -> Any:
    return {"referentiels": REFERENTIELS}


@router.get(
    "/conformite/fenetres-patching",
    response_model=m.AdminConformiteFenetresPatchingGetResponse,
    response_model_exclude_none=True,
)
async def lister_fenetres_patching(
    page: Page,
    statut: str | None = None,
    depuis: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    items = await depot_fenetre.tous(ctx, filtre=lambda f: not statut or f.statut == statut)
    return filtrer_trier_paginer(
        items, page, champs_recherche=("libelle", "perimetre"), tri_defaut="debut"
    )


@router.post(
    "/conformite/fenetres-patching",
    response_model=m.FenetrePatching,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def planifier_fenetre_patching(
    corps: m.AdminConformiteFenetresPatchingPostRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    fenetre = m.FenetrePatching(
        id=nouvel_id(),
        libelle=corps.libelle,
        perimetre=corps.perimetre,
        debut=corps.debut,
        dureeMin=corps.dureeMin,
        recurrence=corps.recurrence,
        impactClient=corps.impactClient,
        organisationsNotifiees=0,
        statut="planifiee",
    )
    await depot_fenetre.creer(ctx, fenetre)
    await journaliser(
        ctx,
        action="fenetre_patching.creation",
        cible_type="fenetre_patching",
        cible_id=fenetre.id,
        cible=fenetre.libelle,
    )
    return fenetre


@router.post(
    "/conformite/tests-restauration",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_campagne_tests_restauration(
    corps: m.AdminConformiteTestsRestaurationPostRequest,
    ctx: Contexte = Depends(exige_admin("compliance.export")),
) -> Any:
    libelle = "Test de restauration " + (corps.valeur or corps.perimetre)
    travail = await demarrer_travail(
        ctx,
        "admin.tests_restauration",
        libelle,
        cible_type="conformite",
        entree=corps.model_dump(mode="json"),
        etapes=[
            {"nom": "Sélectionner les ressources sauvegardées", "dureeS": 18},
            {"nom": "Restaurer un échantillon", "dureeS": 480},
            {"nom": "Contrôler l'intégrité des données", "dureeS": 90},
            {"nom": "Consigner le rapport PRA", "dureeS": 12},
        ],
    )
    await journaliser(
        ctx,
        action="conformite.test_restauration",
        cible_type="conformite",
        cible_id=travail["id"],
        cible=libelle,
        details={"perimetre": corps.perimetre},
    )
    return travail


# ── équipe ───────────────────────────────────────────────────────────────────
def _membre_contrat(u: Utilisateur, eq: dict[str, Any]) -> dict[str, Any]:
    elev = None
    for e in eq.get("elevations") or []:
        expire = e.get("expire")
        actif = bool(e.get("actif", True))
        if expire:
            actif = actif and depuis_iso(expire) > maintenant()
        if actif:
            elev = {"active": True, "jusqua": e.get("expire"), "justification": e.get("motif")}
            break
    return {
        "id": u.id,
        "nom": u.nom,
        "email": u.email,
        "role": eq.get("role"),
        "equipe": eq.get("equipe") or "",
        "dernierAcces": u.dernier_login_le or u.cree_le or maintenant(),
        "privilegie": bool(eq.get("privilegie", False)),
        "elevation": elev,
        "revuLe": eq.get("revuLe"),
    }


@router.get("/equipe", response_model=m.AdminEquipeGetResponse, response_model_exclude_none=True)
async def lister_equipe(
    page: Page,
    equipe: str | None = None,
    privilegie: bool | None = None,
    revueDue: bool | None = None,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: PLR0913
    membres = [
        _membre_contrat(u, u.equipe)
        for u in await service.membres_equipe(ctx)
        if (not equipe or (u.equipe.get("equipe") or "") == equipe)
        and (privilegie is None or u.equipe.get("privilegie") is privilegie)
        and (not revueDue or not u.equipe.get("revuLe"))
    ]
    return filtrer_trier_paginer(membres, page, champs_recherche=("nom", "email"), tri_defaut="nom")


@router.post(
    "/equipe",
    response_model=m.MembreEquipe,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ajouter_membre_equipe(
    corps: m.AdminEquipePostRequest, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Any:
    email = corps.email.lower()
    u = (
        await ctx.session.execute(select(Utilisateur).where(Utilisateur.email == email))
    ).scalar_one_or_none()
    if u is None:
        u = Utilisateur(email=email, nom=corps.nom, idp_source="local", statut="actif")
        ctx.session.add(u)
        await ctx.session.flush()
    u.nom = corps.nom
    u.equipe = {
        "role": corps.role,
        "equipe": corps.equipe,
        "privilegie": bool(corps.privilegie),
        "depuis": maintenant().isoformat(),
        "elevations": [],
    }
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="equipe.ajout",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        details={"role": corps.role},
    )
    return _membre_contrat(u, u.equipe)


@router.patch("/equipe/{membreId}", response_model=m.MembreEquipe, response_model_exclude_none=True)
async def modifier_membre_equipe(
    membreId: str,
    corps: m.AdminEquipeMembreIdPatchRequest,
    ctx: Contexte = Depends(exige_admin("org.manage")),
) -> Any:  # noqa: N803
    u = await service.membre_equipe(ctx, membreId)
    eq = dict(u.equipe)
    if corps.role is not None:
        eq["role"] = corps.role
    if corps.equipe is not None:
        eq["equipe"] = corps.equipe
    if corps.privilegie is not None:
        eq["privilegie"] = corps.privilegie
    if corps.revuLe is not None:
        eq["revuLe"] = iso(corps.revuLe)
    u.equipe = eq
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="equipe.modification",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return _membre_contrat(u, u.equipe)


@router.delete("/equipe/{membreId}", status_code=status.HTTP_204_NO_CONTENT)
async def retirer_membre_equipe(
    membreId: str, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Response:  # noqa: N803
    u = await service.membre_equipe(ctx, membreId)
    super_admins = [
        x
        for x in await service.membres_equipe(ctx)
        if (x.equipe or {}).get("role") == "super_admin"
    ]
    if u.equipe.get("role") == "super_admin" and len(super_admins) <= 1:
        raise erreurs.conflit(
            "Impossible de retirer le dernier super administrateur.", code="dernier_super_admin"
        )
    email = u.email
    u.equipe = None
    await ctx.session.flush()
    await journaliser(
        ctx, action="equipe.retrait", cible_type="utilisateur", cible_id=u.id, cible=email
    )
    return Response(status_code=204)


@router.get(
    "/equipe/{membreId}/elevation",
    response_model=list[m.Elevation],
    response_model_exclude_none=True,
)
async def lister_elevations(
    membreId: str, ctx: Contexte = Depends(exige_admin("audit.view"))
) -> Any:  # noqa: N803
    u = await service.membre_equipe(ctx, membreId)
    return [service.elevation_contrat(e, membre=u.id) for e in (u.equipe.get("elevations") or [])]


@router.post(
    "/equipe/{membreId}/elevation",
    response_model=m.Elevation,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def elever_privileges(
    membreId: str,
    corps: m.AdminEquipeMembreIdElevationPostRequest,
    ctx: Contexte = Depends(exige_admin("org.manage")),
) -> Any:  # noqa: N803
    u = await service.membre_equipe(ctx, membreId)
    eq = dict(u.equipe)
    elevations = list(eq.get("elevations") or [])
    exp = maintenant() + timedelta(minutes=corps.dureeMin)
    heures, minutes = divmod(corps.dureeMin, 60)
    duree_txt = (
        f"{heures} h"
        if heures and not minutes
        else (f"{heures} h {minutes} min" if heures else f"{minutes} min")
    )
    elev = {
        "id": nouvel_id(),
        "qui": ctx.principal.nom if ctx.principal else "systeme",
        "quand": iso(maintenant()),
        "duree": duree_txt,
        "motif": corps.motif,
        "actif": True,
        "membreId": u.id,
        "role": corps.role,
        "ticketId": corps.ticketId,
        "expire": iso(exp),
        "accordePar": ctx.principal.email if ctx.principal else None,
    }
    # Le rôle de base (`eq["role"]`) n'est jamais muté ici : le rôle effectivement
    # autorisé (RBAC comme affichage) est recalculé à chaque lecture par
    # `role_effectif_equipe()`, qui privilégie une élévation active tant qu'elle n'a
    # pas expiré ou été révoquée — sans ça, une élévation « temporaire » restait en
    # fait permanente (jamais réellement révoquée par expiration ou par DELETE).
    elevations.append(elev)
    eq["elevations"] = elevations
    u.equipe = eq
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="equipe.elevation",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
        details={"role": corps.role, "motif": corps.motif},
    )
    return service.elevation_contrat(elev, membre=u.id)


@router.delete("/equipe/{membreId}/elevation", status_code=status.HTTP_204_NO_CONTENT)
async def revoquer_elevation(
    membreId: str, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Response:  # noqa: N803
    u = await service.membre_equipe(ctx, membreId)
    eq = dict(u.equipe)
    # On désactive les élévations actives (`actif=False`) sans effacer l'historique —
    # la trace (qui, quand, motif) reste consultable via GET .../elevation, seul l'effet
    # RBAC cesse (voir `role_effectif_equipe`, qui ignore une élévation `actif=False`).
    elevations = [dict(e) for e in (eq.get("elevations") or [])]
    for e in elevations:
        if e.get("actif", True):
            e["actif"] = False
    eq["elevations"] = elevations
    eq.pop("elevation", None)
    u.equipe = eq
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="equipe.elevation_revocation",
        cible_type="utilisateur",
        cible_id=u.id,
        cible=u.email,
    )
    return Response(status_code=204)


# ── leads ────────────────────────────────────────────────────────────────────
@router.get("/leads", response_model=m.AdminLeadsGetResponse, response_model_exclude_none=True)
async def lister_leads(
    page: Page,
    statut: str | None = None,
    origine: str | None = None,
    assigneA: str | None = None,
    depuis: str | None = None,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: PLR0917
    items = await depot_lead.tous(
        ctx,
        filtre=lambda lead: (
            (not statut or lead.statut == statut)
            and (not origine or lead.origine == origine)
            and (not assigneA or lead.assigneA == assigneA)
        ),
    )
    return filtrer_trier_paginer(
        items, page, champs_recherche=("nom", "email", "organisation"), tri_defaut="recuLe"
    )


@router.patch("/leads/{leadId}", response_model=m.Lead, response_model_exclude_none=True)
async def modifier_lead(
    leadId: str, corps: m.AdminLeadsLeadIdPatchRequest, ctx: Contexte = Depends(exige_admin(None))
) -> Any:  # noqa: N803
    lead = await depot_lead.obtenir(ctx, leadId)
    changements: dict[str, Any] = {}
    if corps.statut is not None:
        changements["statut"] = corps.statut
    if corps.assigneA is not None:
        changements["assigneA"] = corps.assigneA
    if corps.note:
        notes = list(lead.notes or [])
        notes.append(
            m.Note(
                date=maintenant(),
                auteur=ctx.principal.email if ctx.principal else "synelia",
                texte=corps.note,
            )
        )
        changements["notes"] = notes
    if changements:
        lead = await depot_lead.modifier(ctx, leadId, changements)
        await journaliser(
            ctx,
            action="lead.modification",
            cible_type="lead",
            cible_id=leadId,
            cible=lead.nom,
            details=corps.model_dump(mode="json", exclude_none=True),
        )
    return lead


# ── marketplace : campagnes de mise à jour ───────────────────────────────────
@router.get(
    "/marketplace/campagnes",
    response_model=m.AdminMarketplaceCampagnesGetResponse,
    response_model_exclude_none=True,
)
async def lister_campagnes_maj(
    page: Page,
    catalogSlug: str | None = None,
    statut: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    items = await depot_campagne_maj.tous(
        ctx,
        filtre=lambda c: (
            (not catalogSlug or c.catalogSlug == catalogSlug) and (not statut or c.statut == statut)
        ),
    )
    return filtrer_trier_paginer(items, page, champs_recherche=("nom",), tri_defaut="nom")


@router.post(
    "/marketplace/campagnes",
    response_model=m.CampagneMaj,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_campagne_maj(
    corps: m.AdminMarketplaceCampagnesPostRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    if corps.instances:
        instances = len(corps.instances)
    else:
        instances = len(await service.lignes_type(ctx, "service_manage"))
    campagne = m.CampagneMaj(
        id=nouvel_id(),
        nom=corps.nom,
        catalogSlug=corps.catalogSlug,
        versionCible=corps.versionCible,
        fenetre=corps.fenetre,
        instances=instances,
        faites=0,
        statut="planifiee",
        strategie=corps.strategie,
        notesVersion=corps.notesVersion,
        rollbackPossible=True,
    )
    await depot_campagne_maj.creer(ctx, campagne)
    await journaliser(
        ctx,
        action="campagne_maj.creation",
        cible_type="campagne_maj",
        cible_id=campagne.id,
        cible=campagne.nom,
    )
    return campagne


async def _campagne_maj(ctx: Contexte, campagneId: str) -> m.CampagneMaj:  # noqa: N803
    return await depot_campagne_maj.obtenir(ctx, campagneId)


@router.post(
    "/marketplace/campagnes/{campagneId}/lancement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_campagne_maj(
    campagneId: str, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:  # noqa: N803
    c = await _campagne_maj(ctx, campagneId)
    if c.statut not in ("planifiee", "suspendue"):
        raise erreurs.conflit(
            "La campagne ne peut pas être lancée dans son état actuel.", code="etat_invalide"
        )
    await depot_campagne_maj.definir_statut(ctx, c.id, "en_cours")
    await journaliser(
        ctx, action="campagne_maj.lancement", cible_type="campagne_maj", cible_id=c.id, cible=c.nom
    )
    return await demarrer_travail(
        ctx, "admin.maj.lancement", c.nom, cible_type="campagne_maj", cible_id=c.id, entree={}
    )


@router.post(
    "/marketplace/campagnes/{campagneId}/suspension",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def suspendre_campagne_maj(
    campagneId: str, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:  # noqa: N803
    c = await _campagne_maj(ctx, campagneId)
    if c.statut not in ("planifiee", "en_cours"):
        raise erreurs.conflit(
            "La campagne ne peut pas être suspendue dans son état actuel.", code="etat_invalide"
        )
    await depot_campagne_maj.definir_statut(ctx, c.id, "suspendue")
    await journaliser(
        ctx, action="campagne_maj.suspension", cible_type="campagne_maj", cible_id=c.id, cible=c.nom
    )
    return await demarrer_travail(
        ctx, "admin.suspension", c.nom, cible_type="campagne_maj", cible_id=c.id, entree={}
    )


async def _instance_parc(ctx: Contexte, r: Ressource, orgs: dict[str, str]) -> dict[str, Any]:
    d = r.donnees or {}
    return {
        "id": r.id,
        "orgId": r.org_id or "",
        "orgNom": orgs.get(r.org_id) if r.org_id else None,
        "catalogSlug": d.get("catalogSlug"),
        "serviceNom": d.get("serviceNom"),
        "mode": d.get("mode", "mutualise"),
        "site": d.get("site", "ABJ"),
        "version": d.get("version", "1.0.0"),
        "sieges": d.get("sieges") or "0/0",
        "sante": d.get("sante", "ok"),
        "derniereSauvegarde": d.get("derniereSauvegarde"),
        "derniereMaj": d.get("derniereMaj"),
    }


@router.get(
    "/marketplace/instances",
    response_model=m.AdminMarketplaceInstancesGetResponse,
    response_model_exclude_none=True,
)
async def lister_parc_instances(
    page: Page,
    catalogSlug: str | None = None,
    orgId: str | None = None,
    sante: str | None = None,
    version: str | None = None,
    site: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:  # noqa: N803,PLR0913,PLR0917
    orgs = {o.id: o.nom for o in (await ctx.session.execute(select(Organisation))).scalars()}
    items = []
    for r in await service.lignes_type(ctx, "service_manage", org_id=orgId):
        inst = await _instance_parc(ctx, r, orgs)
        if catalogSlug and inst["catalogSlug"] != catalogSlug:
            continue
        if sante and inst["sante"] != sante:
            continue
        if version and inst["version"] != version:
            continue
        if site and inst["site"] != site:
            continue
        items.append(inst)
    return filtrer_trier_paginer(
        items, page, champs_recherche=("serviceNom", "catalogSlug"), tri_defaut="serviceNom"
    )


# ── migration : campagnes ────────────────────────────────────────────────────
@router.get(
    "/migration/campagnes",
    response_model=m.AdminMigrationCampagnesGetResponse,
    response_model_exclude_none=True,
)
async def lister_campagnes_migration(
    page: Page,
    statut: str | None = None,
    backendSource: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    items = await depot_campagne_migration.tous(
        ctx,
        filtre=lambda c: (
            (not statut or c.statut == statut)
            and (not backendSource or c.backendSource == backendSource)
        ),
    )
    return filtrer_trier_paginer(items, page, champs_recherche=("nom",), tri_defaut="nom")


@router.post(
    "/migration/campagnes",
    response_model=m.CampagneMigration,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_campagne_migration(
    corps: m.AdminMigrationCampagnesPostRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    ressources = len(corps.ressources) if corps.ressources else 0
    campagne = m.CampagneMigration(
        id=nouvel_id(),
        nom=corps.nom,
        backendSource=corps.backendSource,
        backendCible=corps.backendCible,
        ressources=ressources,
        migrees=0,
        fenetre=corps.fenetre,
        statut="planifiee",
        rollbackPossible=True,
        impactClient="Coupure annoncée" if corps.notifierClients else None,
    )
    await depot_campagne_migration.creer(ctx, campagne)
    await journaliser(
        ctx,
        action="campagne_migration.creation",
        cible_type="campagne_migration",
        cible_id=campagne.id,
        cible=campagne.nom,
    )
    return campagne


async def _campagne_migration(ctx: Contexte, campagneId: str) -> m.CampagneMigration:  # noqa: N803
    return await depot_campagne_migration.obtenir(ctx, campagneId)


@router.post(
    "/migration/campagnes/{campagneId}/lancement",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def lancer_campagne_migration(
    campagneId: str, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:  # noqa: N803
    c = await _campagne_migration(ctx, campagneId)
    if c.statut not in ("planifiee", "suspendue"):
        raise erreurs.conflit(
            "La campagne ne peut pas être lancée dans son état actuel.", code="etat_invalide"
        )
    await depot_campagne_migration.definir_statut(ctx, c.id, "en_cours")
    await journaliser(
        ctx,
        action="campagne_migration.lancement",
        cible_type="campagne_migration",
        cible_id=c.id,
        cible=c.nom,
    )
    return await demarrer_travail(
        ctx,
        "admin.migration.lancement",
        c.nom,
        cible_type="campagne_migration",
        cible_id=c.id,
        entree={},
    )


@router.post(
    "/migration/campagnes/{campagneId}/rollback",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def annuler_campagne_migration(
    campagneId: str,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:  # noqa: N803
    c = await _campagne_migration(ctx, campagneId)
    exiger_confirmation(c.nom, confirmation)
    await journaliser(
        ctx,
        action="campagne_migration.rollback",
        cible_type="campagne_migration",
        cible_id=c.id,
        cible=c.nom,
    )
    return await demarrer_travail(
        ctx,
        "admin.migration.rollback",
        c.nom,
        cible_type="campagne_migration",
        cible_id=c.id,
        entree={},
    )


@router.post(
    "/migration/campagnes/{campagneId}/suspension",
    response_model=m.TravailProvisioning,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def suspendre_campagne_migration(
    campagneId: str, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:  # noqa: N803
    c = await _campagne_migration(ctx, campagneId)
    if c.statut not in ("planifiee", "en_cours"):
        raise erreurs.conflit(
            "La campagne ne peut pas être suspendue dans son état actuel.", code="etat_invalide"
        )
    await depot_campagne_migration.definir_statut(ctx, c.id, "suspendue")
    await journaliser(
        ctx,
        action="campagne_migration.suspension",
        cible_type="campagne_migration",
        cible_id=c.id,
        cible=c.nom,
    )
    return await demarrer_travail(
        ctx, "admin.suspension", c.nom, cible_type="campagne_migration", cible_id=c.id, entree={}
    )


# ── organisations : notification ─────────────────────────────────────────────
@router.post(
    "/organisations/{orgId}/notification",
    response_model=m.AdminOrganisationsOrgIdNotificationPostResponse,
    response_model_exclude_none=True,
)
async def notifier_organisation(
    orgId: str,
    corps: m.AdminOrganisationsOrgIdNotificationPostRequest,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: N803
    org = await ctx.session.get(Organisation, orgId)
    if org is None:
        raise erreurs.introuvable("Organisation", orgId)
    q = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.org_id == orgId, Membership.scope_type == "org")
    )
    if corps.roles:
        q = q.where(Membership.role.in_(corps.roles))
    destinataires = int((await ctx.session.execute(q)).scalar_one())
    ctx.session.add(
        Ressource(
            id=nouvel_id(),
            org_id=orgId,
            type="notification",
            nom=corps.sujet,
            donnees=corps.model_dump(mode="json", exclude_none=True),
        )
    )
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="organisation.notification",
        cible_type="organisation",
        cible_id=orgId,
        cible=org.nom,
        org_id=orgId,
        details={"destinataires": destinataires, "sujet": corps.sujet},
    )
    return {"destinataires": destinataires}


# ── organisations : lecture cross-tenant (fiche organisation, espace fournisseur) ────────────
# Les routes `/espaces`, `/membres`, `/support/tickets` sont scellées par organisation (RLS +
# filtre applicatif sur `ctx.org_id`, cf. `Depot._org`) : un admin plateforme qui consulte la
# fiche d'*une* organisation cliente ne peut pas s'en servir pour lire celles d'une autre. Ces
# trois routes existent pour ce seul usage — passer `org_id=orgId` explicitement au dépôt/à la
# requête, comme le fait déjà `notifier_organisation` ci-dessus et `service.lignes_type` pour les
# agrégations plateforme. Aucune n'accepte l'id d'appel du client : l'appelant doit être
# `exige_admin`, jamais une route `/v1/**` ordinaire.
@router.get(
    "/organisations/{orgId}/espaces",
    response_model=list[m.EspaceCloud],
    response_model_exclude_none=True,
)
async def lister_espaces_organisation(
    orgId: str, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Any:  # noqa: N803
    org = await ctx.session.get(Organisation, orgId)
    if org is None:
        raise erreurs.introuvable("Organisation", orgId)
    return await depot_espace.tous(ctx, org_id=orgId)


@router.get(
    "/organisations/{orgId}/membres",
    response_model=list[m.Membre],
    response_model_exclude_none=True,
)
async def lister_membres_organisation(
    orgId: str, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Any:  # noqa: N803
    org = await ctx.session.get(Organisation, orgId)
    if org is None:
        raise erreurs.introuvable("Organisation", orgId)
    q = (
        select(Membership, Utilisateur)
        .join(Utilisateur, Utilisateur.id == Membership.utilisateur_id)
        .where(Membership.org_id == orgId)
        .order_by(Membership.cree_le)
    )
    lignes = (await ctx.session.execute(q)).all()
    return [membre_contrat(ctx, mem, u) for mem, u in lignes]


@router.get(
    "/organisations/{orgId}/tickets",
    response_model=list[m.Ticket],
    response_model_exclude_none=True,
)
async def lister_tickets_organisation(
    orgId: str, ctx: Contexte = Depends(exige_admin("org.manage"))
) -> Any:  # noqa: N803
    org = await ctx.session.get(Organisation, orgId)
    if org is None:
        raise erreurs.introuvable("Organisation", orgId)
    items = await detenteur_tickets.tous(ctx, org_id=orgId)
    return [t.model_dump(mode="json") for t in items]


# ── placements ───────────────────────────────────────────────────────────────
@router.put("/placements", response_model=list[m.Placement], response_model_exclude_none=True)
async def modifier_placements(
    corps: m.AdminPlacementsPutRequest, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:
    par_espace: dict[str, float] = {}
    for p in corps.placements:
        par_espace[p.espaceId] = round(par_espace.get(p.espaceId, 0.0) + p.percent, 2)
    for espace_id, total in par_espace.items():
        if total != 100.0:
            raise erreurs.validation(
                f"Les pourcentages de l'espace {espace_id} doivent totaliser 100 (actuel : {total}).",
                champs={"placements": "total 100 requis par espace"},
            )
    for existant in await depot_placement.tous(ctx):
        await depot_placement.supprimer(ctx, existant.id)
    nouveaux = []
    for p in corps.placements:
        nouveau = m.Placement(
            id=nouvel_id(), espaceId=p.espaceId, backendId=p.backendId, percent=p.percent
        )
        await depot_placement.creer(ctx, nouveau)
        nouveaux.append(nouveau)
    await journaliser(
        ctx,
        action="placement.reconfiguration",
        cible_type="placement",
        cible=", ".join(sorted(par_espace)),
        details={"espaces": par_espace},
    )
    return nouveaux


# ── santé, sites, statut ─────────────────────────────────────────────────────
@router.get("/sante", response_model=m.SanteePlateforme, response_model_exclude_none=True)
async def obtenir_sante_plateforme(ctx: Contexte = Depends(exige_admin("capacity.manage"))) -> Any:
    backends = await service.amacer_backends(ctx)
    usage = await service.usage_plateforme(ctx)
    en_attente = en_cours = en_echec = 0
    for t in (await ctx.session.execute(select(Travail))).scalars():
        if t.statut == "queued":
            en_attente += 1
        elif t.statut == "running":
            en_cours += 1
        elif t.statut in ("failed", "rolled_back"):
            en_echec += 1
    return {
        "backends": [_backend_usage(b, usage) for b in backends],
        "filesProvisioning": {"enAttente": en_attente, "enCours": en_cours, "enEchec24h": en_echec},
        "integrations": await service.sante_integrations(ctx),
        "alertes": [],
        "accesRefuses24h": await service.acces_refuses_24h(ctx),
        "ticketsSlaRisque": await service.tickets_sla_risque(ctx),
    }


# Seul le site ABJ est réellement adossé au lab OpenStack. Un second site "Grand-Bassam"
# (GBM) figurait ici avec des specs inventées (Tier III, 800 kW, ISO 27001, latence 4ms) —
# retiré le 2026-09-09, même règle que `backend-gbm` dans service.py : un site fantôme avec
# des chiffres précis est plus trompeur qu'un site absent.
SITES_PHYSIQUES = [
    {
        "code": "ABJ",
        "nom": "Datacenter Abidjan",
        "ville": "Abidjan",
        "site": "ABJ",
        "operateur": "Synelia Cloud",
        "certifications": ["ISO 27001", "Tier III"],
        "energie": "Double alimentation",
        "redondance": "2N",
        "capacite": "1,2 MW",
        "photoUrl": "/images/sites/abj.jpg",
    },
]


@router.get("/sites", response_model=list[m.Datacenter], response_model_exclude_none=True)
async def lister_sites_physiques(ctx: Contexte = Depends(exige_admin("capacity.manage"))) -> Any:
    return SITES_PHYSIQUES


@router.get(
    "/statut/incidents",
    response_model=m.AdminStatutIncidentsGetResponse,
    response_model_exclude_none=True,
)
async def lister_incidents_plateforme(
    page: Page, statut: str | None = None, ctx: Contexte = Depends(exige_admin(None))
) -> Any:
    items = await depot_incident.tous(ctx, filtre=lambda i: not statut or i.statut == statut)
    return filtrer_trier_paginer(items, page, champs_recherche=("titre",), tri_defaut="debut")


@router.post(
    "/statut/incidents",
    response_model=m.Incident,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ouvrir_incident(
    corps: m.AdminStatutIncidentsPostRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    incident = m.Incident(
        id=nouvel_id(),
        titre=corps.titre,
        gravite=corps.gravite,
        statut="en_cours",
        debut=maintenant(),
        services=corps.services,
        sites=corps.sites,
        mises_a_jour=[{"ts": maintenant(), "texte": corps.message}],
    )
    await depot_incident.creer(ctx, incident)
    await journaliser(
        ctx,
        action="incident.ouverture",
        cible_type="incident",
        cible_id=incident.id,
        cible=incident.titre,
        details={"gravite": corps.gravite},
    )
    return incident


@router.post(
    "/statut/incidents/{incidentId}",
    response_model=m.Incident,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def mettre_a_jour_incident(
    incidentId: str,
    corps: m.AdminStatutIncidentsIncidentIdPostRequest,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:  # noqa: N803
    inc = await depot_incident.obtenir(ctx, incidentId)
    maj = list(inc.mises_a_jour)
    maj.append({"ts": maintenant(), "texte": corps.texte})
    changements: dict[str, Any] = {"mises_a_jour": maj}
    if corps.statut is not None:
        changements["statut"] = corps.statut
        if corps.statut == "resolu":
            changements["fin"] = maintenant()
    inc = await depot_incident.modifier(ctx, incidentId, changements)
    await journaliser(
        ctx,
        action="incident.mise_a_jour",
        cible_type="incident",
        cible_id=incidentId,
        cible=inc.titre,
        details={"statut": corps.statut},
    )
    return inc


@router.put(
    "/statut/services", response_model=list[m.StatutService], response_model_exclude_none=True
)
async def modifier_statut_services(
    corps: m.AdminStatutServicesPutRequest, ctx: Contexte = Depends(exige_admin("capacity.manage"))
) -> Any:
    for existant in await depot_statut_service.lignes(ctx):
        await depot_statut_service.supprimer(ctx, existant.id)
    services = []
    for s in corps.services:
        service_c = s.model_copy(update={"id": nouvel_id()}) if hasattr(s, "id") else s
        await depot_statut_service.creer(ctx, service_c)
        services.append(service_c)
    await journaliser(
        ctx,
        action="statut_services.modification",
        cible_type="statut_service",
        details={"services": [s.nom for s in services]},
    )
    return services


# ── tableau de bord ──────────────────────────────────────────────────────────
@router.get(
    "/tableau-de-bord", response_model=m.SynthesePlateforme, response_model_exclude_none=True
)
async def obtenir_tableau_de_bord_plateforme(
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:
    backends = await service.amacer_backends(ctx)
    en_ligne = sum(1 for b in backends if b.statut == "en_ligne")
    vcpu_total = sum(b.capacite.vcpu for b in backends)
    ram_total = sum(b.capacite.ramGo for b in backends)
    stockage_total = sum(b.capacite.stockageTo for b in backends)
    usage = await service.usage_plateforme(ctx)
    tenants_actifs = int(
        (
            await ctx.session.execute(
                select(func.count())
                .select_from(Organisation)
                .where(Organisation.statut == "active")
            )
        ).scalar_one()
    )
    espaces = len(await service.lignes_type(ctx, "espace"))
    projets = len(await service.lignes_type(ctx, "projet"))
    jobs_echec = int(
        (
            await ctx.session.execute(
                select(func.count())
                .select_from(Travail)
                .where(Travail.statut.in_(("failed", "rolled_back")))
            )
        ).scalar_one()
    )
    return {
        "vcpuTotal": vcpu_total,
        "vcpuUtilise": int(usage["vcpu"]),
        "ramTotalGo": ram_total,
        "stockageTotalTo": round(stockage_total, 2),
        "tenantsActifs": tenants_actifs,
        "espacesTotal": espaces,
        "projetsTotal": projets,
        "backendsEnLigne": en_ligne,
        "backendsTotal": len(backends),
        "accesRefuses24h": await service.acces_refuses_24h(ctx),
        "jobsEnEchec": jobs_echec,
        "ticketsSlaRisque": await service.tickets_sla_risque(ctx),
        # `caMensuel` reste à 0 : la facturation (module `facturation`) n'agrège pas encore
        # de revenu récurrent plateforme calculé — hors périmètre de ce module.
        "caMensuel": 0,
    }


# ── tickets ──────────────────────────────────────────────────────────────────
def _ticket(r: Ressource) -> m.Ticket:
    return m.Ticket.model_validate(r.donnees)


async def _charger_ticket(ctx: Contexte, ticketId: str) -> tuple[Ressource, m.Ticket]:  # noqa: N803
    r = (
        await ctx.session.execute(
            select(Ressource).where(Ressource.type == "ticket", Ressource.id == ticketId)
        )
    ).scalar_one_or_none()
    if r is None:
        raise erreurs.introuvable("Ticket", ticketId)
    return r, _ticket(r)


@router.get("/tickets", response_model=m.AdminTicketsGetResponse, response_model_exclude_none=True)
async def lister_tickets_plateforme(
    page: Page,
    orgId: str | None = None,
    statut: str | None = None,
    gravite: str | None = None,
    slaRisque: bool | None = None,
    assigneA: str | None = None,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: N803,PLR0913,PLR0917
    items = []
    for r in await service.lignes_type(ctx, "ticket", org_id=orgId):
        t = _ticket(r)
        if statut and t.statut != statut:
            continue
        if gravite and t.gravite != gravite:
            continue
        if assigneA and t.assigneA != assigneA:
            continue
        if slaRisque is not None and (t.slaRestantMin is None or t.slaRestantMin > 30) == slaRisque:
            continue
        items.append(t)
    return filtrer_trier_paginer(
        items, page, champs_recherche=("sujet", "numero"), tri_defaut="createdAt"
    )


@router.patch("/tickets/{ticketId}", response_model=m.Ticket, response_model_exclude_none=True)
async def traiter_ticket(
    ticketId: str,
    corps: m.AdminTicketsTicketIdPatchRequest,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: N803
    r, t = await _charger_ticket(ctx, ticketId)
    donnees = dict(r.donnees)
    if corps.assigneA is not None:
        donnees["assigneA"] = corps.assigneA
    if corps.statut is not None:
        donnees["statut"] = corps.statut
    if corps.gravite is not None:
        donnees["gravite"] = corps.gravite
    r.donnees = donnees
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="ticket.traitement",
        cible_type="ticket",
        cible_id=ticketId,
        cible=t.sujet,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return _ticket(r)


@router.post(
    "/tickets/{ticketId}/messages",
    response_model=m.Ticket,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def repondre_ticket_plateforme(
    ticketId: str,
    corps: m.AdminTicketsTicketIdMessagesPostRequest,
    ctx: Contexte = Depends(exige_admin(None)),
) -> Any:  # noqa: N803
    r, t = await _charger_ticket(ctx, ticketId)
    donnees = dict(r.donnees)
    messages = list(donnees.get("messages") or [])
    messages.append(
        {
            "auteur": ctx.principal.email if ctx.principal else "synelia",
            "role": "synelia",
            "date": iso(maintenant()),
            "contenu": corps.contenu,
            "pieces": corps.pieces,
        }
    )
    donnees["messages"] = messages
    r.donnees = donnees
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="ticket.reponse",
        cible_type="ticket",
        cible_id=ticketId,
        cible=t.sujet,
        details={"interne": corps.interne},
    )
    return _ticket(r)


# ── travaux ──────────────────────────────────────────────────────────────────
@router.get("/travaux", response_model=m.AdminTravauxGetResponse, response_model_exclude_none=True)
async def lister_travaux_plateforme(
    page: Page,
    statut: str | None = None,
    orgId: str | None = None,
    type: str | None = None,
    ctx: Contexte = Depends(exige_admin("capacity.manage")),
) -> Any:  # noqa: A002
    q = select(Travail)
    if statut:
        q = q.where(Travail.statut == statut)
    if orgId:
        q = q.where(Travail.org_id == orgId)
    if type:
        q = q.where(Travail.type == type)
    items = [
        vers_contrat(t)
        for t in (await ctx.session.execute(q.order_by(Travail.started_at.desc()))).scalars()
    ]
    return filtrer_trier_paginer(items, page, champs_recherche=("label", "type"))
