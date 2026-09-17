from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db import rls
from synelia_db.modeles import Membership, Organisation, Utilisateur
from synelia_kernel import erreurs

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.modules.auth import service as auth
from synelia.modules.organisations import service

router = APIRouter(prefix="/organisations", tags=["Organisations"])
router_utilisateurs = APIRouter(prefix="/utilisateurs", tags=["Compte & organisation active"])


@router.get("", response_model=m.OrganisationsGetResponse, response_model_exclude_none=True)
async def lister_organisations(
    page: Page,
    statut: str | None = None,
    secteur: str | None = None,
    site: str | None = None,
    ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True)),
) -> Any:
    p = ctx.principal
    assert p is not None
    q = select(Organisation)
    if not p.est_admin_plateforme:
        ids = set(p.roles_par_org) | ({p.org_id} if p.org_id else set())
        q = q.where(Organisation.id.in_(ids))
    if statut:
        q = q.where(Organisation.statut == statut)
    if secteur:
        q = q.where(Organisation.secteur == secteur)
    orgs = [
        await service.vers_contrat(ctx, o)
        for o in (await ctx.session.execute(q.order_by(Organisation.nom))).scalars()
    ]
    return filtrer_trier_paginer(
        orgs, page, champs_recherche=("nom", "secteur", "domaine"), tri_defaut="nom"
    )


@router.post(
    "",
    response_model=m.Organisation,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def creer_organisation(
    corps: m.OrganisationCreation, ctx: Contexte = Depends(exige("org.manage"))
) -> Any:
    if (
        await ctx.session.execute(select(Organisation).where(Organisation.nom == corps.nom))
    ).scalar_one_or_none():
        raise erreurs.nom_deja_pris(corps.nom)
    org = Organisation(
        nom=corps.nom,
        pays=corps.pays,
        secteur=corps.secteur,
        tva=corps.tva,
        tenant_plan=corps.tenantPlan,
        statut="active",
    )
    ctx.session.add(org)
    await ctx.session.flush()
    details: dict[str, Any] = {}
    if corps.administrateur:
        email = corps.administrateur.email.lower()
        u = (
            await ctx.session.execute(select(Utilisateur).where(Utilisateur.email == email))
        ).scalar_one_or_none()
        if u is None:
            u = Utilisateur(
                email=email,
                nom=corps.administrateur.nom,
                idp_source="local",
                statut="invite",
                org_active_id=org.id,
            )
            ctx.session.add(u)
            await ctx.session.flush()
        # RLS de `memberships` vérifie `org_id = current_setting('app.org_id')` : celui de la
        # transaction reste l'organisation active de l'appelant (l'admin plateforme qui crée
        # cette organisation), jamais `org.id` qui vient tout juste d'être créé — sans lever le
        # filtre le temps de cet insert, Postgres refuse la ligne (« new row violates row-level
        # security policy », constaté en direct : toute création d'organisation avec un
        # administrateur échouait en 500). Même parade que `auth/service.py::appartenances`.
        async with rls.sans_org(ctx.session):
            ctx.session.add(
                Membership(utilisateur_id=u.id, org_id=org.id, role="org_admin", scope_type="org")
            )
            await ctx.session.flush()
        details["administrateur"] = email
    await journaliser(
        ctx,
        action="organisation.creation",
        cible_type="organisation",
        cible_id=org.id,
        cible=org.nom,
        details=details,
    )
    return await service.vers_contrat(ctx, org)


@router.get("/{orgId}", response_model=m.Organisation, response_model_exclude_none=True)
async def obtenir_organisation(
    orgId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    return await service.vers_contrat(ctx, await service.obtenir(ctx, orgId))


@router.patch("/{orgId}", response_model=m.Organisation, response_model_exclude_none=True)
async def modifier_organisation(
    orgId: str, corps: m.OrganisationModification, ctx: Contexte = Depends(exige("org.manage"))
) -> Any:  # noqa: N803
    o = await service.obtenir(ctx, orgId)
    if corps.nom and corps.nom != o.nom:
        if (
            await ctx.session.execute(select(Organisation).where(Organisation.nom == corps.nom))
        ).scalar_one_or_none():
            raise erreurs.nom_deja_pris(corps.nom)
        o.nom = corps.nom
    if corps.secteur is not None:
        o.secteur = corps.secteur
    if corps.tva is not None:
        o.tva = corps.tva
    if corps.logoUrl is not None:
        o.logo_url = corps.logoUrl
    if corps.tenantPlan is not None:
        o.tenant_plan = corps.tenantPlan
    if corps.statut is not None:
        if not (ctx.principal and ctx.principal.est_admin_plateforme):
            raise erreurs.interdit(
                "Le statut d'une organisation est géré par Synelia.", code="statut_reserve"
            )
        o.statut = corps.statut
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="organisation.modification",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        org_id=o.id,
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return await service.vers_contrat(ctx, o)


@router.post(
    "/{orgId}/emprunt-identite",
    response_model=m.Session,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def emprunter_identite_organisation(
    orgId: str,
    corps: m.OrganisationsOrgIdEmpruntIdentitePostRequest,
    ctx: Contexte = Depends(exige("org.manage")),
) -> Any:  # noqa: N803
    o = await service.obtenir(ctx, orgId)
    p = ctx.principal
    if p is None or not p.utilisateur_id:
        raise erreurs.interdit("Une clé d'API ne peut pas emprunter une identité.", code="cle_api")
    u = await ctx.session.get(Utilisateur, p.utilisateur_id)
    assert u is not None
    duree = (corps.dureeMin or 30) * 60
    rep = await auth.ouvrir_session(
        ctx.session,
        u,
        ip=ctx.ip,
        user_agent=ctx.entete("user-agent"),
        org_id=o.id,
        emprunt=True,
        duree_s=duree,
    )
    await journaliser(
        ctx,
        action="organisation.emprunt_identite",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        org_id=o.id,
        details={
            "motif": corps.motif,
            "ticketId": corps.ticketId,
            "ecriture": bool(corps.ecriture),
            "dureeS": duree,
        },
    )
    return rep


@router.post("/{orgId}/suspension", response_model=m.Organisation, response_model_exclude_none=True)
async def suspendre_organisation(
    orgId: str,
    corps: m.OrganisationsOrgIdSuspensionPostRequest,
    confirmation: str | None = None,
    ctx: Contexte = Depends(exige("org.manage")),
) -> Any:  # noqa: N803
    o = await service.obtenir(ctx, orgId)
    exiger_confirmation(o.nom, confirmation)
    if o.statut == "suspendue":
        raise erreurs.conflit("L'organisation est déjà suspendue.", code="deja_suspendue")
    o.statut = "suspendue"
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="organisation.suspension",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        org_id=o.id,
        details={"motif": corps.motif, "notifier": bool(corps.notifier)},
    )
    return await service.vers_contrat(ctx, o)


@router.delete(
    "/{orgId}/suspension", response_model=m.Organisation, response_model_exclude_none=True
)
async def reactiver_organisation(orgId: str, ctx: Contexte = Depends(exige("org.manage"))) -> Any:  # noqa: N803
    o = await service.obtenir(ctx, orgId)
    if o.statut != "suspendue":
        raise erreurs.conflit("L'organisation n'est pas suspendue.", code="non_suspendue")
    o.statut = "active"
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="organisation.reactivation",
        cible_type="organisation",
        cible_id=o.id,
        cible=o.nom,
        org_id=o.id,
    )
    return await service.vers_contrat(ctx, o)


@router.get(
    "/{orgId}/synthese",
    response_model=m.OrganisationsOrgIdSyntheseGetResponse,
    response_model_exclude_none=True,
)
async def obtenir_synthese_organisation(
    orgId: str, ctx: Contexte = Depends(exige("org.dashboard.view", lecture=True))
) -> Any:  # noqa: N803
    o = await service.obtenir(ctx, orgId)
    return {
        "organisation": await service.vers_contrat(ctx, o),
        "synthese": await service.synthese(ctx, o.id),
        "impayes": await service.impayes(ctx, o.id),
        "tickets": await service.tickets_organisation(ctx, o.id),
    }


@router_utilisateurs.get(
    "", response_model=m.UtilisateursGetResponse, response_model_exclude_none=True
)
async def lister_utilisateurs(
    page: Page, statut: str | None = None, ctx: Contexte = Depends(exige(None))
) -> Any:
    q = (
        select(Utilisateur)
        .join(Membership, Membership.utilisateur_id == Utilisateur.id)
        .where(Membership.org_id == ctx.org_id)
        .distinct()
    )
    if statut:
        q = q.where(Utilisateur.statut == statut)
    utilisateurs = [
        auth.utilisateur_contrat(u)
        for u in (await ctx.session.execute(q.order_by(Utilisateur.nom))).scalars()
    ]
    return filtrer_trier_paginer(
        utilisateurs, page, champs_recherche=("nom", "email", "fonction"), tri_defaut="nom"
    )
