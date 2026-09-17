from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from synelia_contract import modeles as m
from synelia_contract.rbac import ROLES_ORDRE
from synelia_db.modeles import Invitation, Membership, Organisation, Utilisateur
from synelia_kernel import courriel, erreurs
from synelia_kernel.dates import dans
from synelia_kernel.ids import jeton_opaque, nouvel_id

from synelia.audit import journaliser
from synelia.deps import Contexte, Page, exige, exiger_confirmation
from synelia.deps.pagination import filtrer_trier_paginer
from synelia.modules.auth import service as auth
from synelia.securite import hacher_jeton

router = APIRouter(prefix="/membres", tags=["Membres & rôles"])
router_invitations = APIRouter(prefix="/invitations", tags=["Membres & rôles"])

DUREE_INVITATION_S = 7 * 24 * 3600


def membre_contrat(ctx: Contexte, mem: Membership, u: Utilisateur | None) -> dict[str, Any]:
    return {
        "id": mem.id,
        "userId": mem.utilisateur_id,
        "orgId": mem.org_id,
        "role": mem.role,
        "scopeType": mem.scope_type or "org",
        "scopeId": mem.scope_id,
        "scopeLabel": None,
        "utilisateur": auth.utilisateur_contrat(u) if u else None,
    }


async def obtenir_membership(ctx: Contexte, mem_id: str) -> tuple[Membership, Utilisateur | None]:
    mem = (
        await ctx.session.execute(
            select(Membership).where(Membership.id == mem_id, Membership.org_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if mem is None:
        raise erreurs.introuvable("Membre", mem_id)
    u = await ctx.session.get(Utilisateur, mem.utilisateur_id)
    return mem, u


async def _refuser_si_dernier_admin(ctx: Contexte, mem: Membership, message: str) -> None:
    """Bloque toute opération (retrait ou rétrogradation) qui laisserait l'organisation sans
    aucun `org_admin` — pas de chemin de récupération possible pour un compte encore
    authentifié qui se coupe (ou coupe le dernier autre admin) l'accès admin de l'org."""
    if mem.role != "org_admin" or mem.scope_type != "org":
        return
    nb = (
        await ctx.session.execute(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.org_id == ctx.org_id,
                Membership.role == "org_admin",
                Membership.scope_type == "org",
                Membership.id != mem.id,
            )
        )
    ).scalar_one()
    if nb == 0:
        raise erreurs.conflit(message, code="dernier_admin")


def invitation_contrat(ctx: Contexte, inv: Invitation, org_nom: str | None) -> dict[str, Any]:
    return {
        "id": inv.id,
        "email": inv.email,
        "orgId": inv.org_id,
        "orgNom": org_nom,
        "role": inv.role,
        "scopeType": inv.scope_type or "org",
        "scopeId": inv.scope_id,
        "invitePar": inv.invite_par,
        "expire": inv.expire_le,
        "statut": inv.statut or "en_attente",
    }


@router.get("", response_model=m.MembresGetResponse, response_model_exclude_none=True)
async def lister_membres(  # noqa: PLR0917
    page: Page,
    role: str | None = None,
    scopeType: str | None = None,  # noqa: N803
    scopeId: str | None = None,  # noqa: N803
    statut: str | None = None,
    ctx: Contexte = Depends(exige("member.invite", lecture=True)),
) -> Any:
    q = (
        select(Membership, Utilisateur)
        .join(Utilisateur, Utilisateur.id == Membership.utilisateur_id)
        .where(Membership.org_id == ctx.org_id)
    )
    if role:
        q = q.where(Membership.role == role)
    if scopeType:
        q = q.where(Membership.scope_type == scopeType)
    if scopeId:
        q = q.where(Membership.scope_id == scopeId)
    if statut:
        q = q.where(Membership.statut == statut)
    lignes = (await ctx.session.execute(q.order_by(Membership.cree_le))).all()
    resultats = [membre_contrat(ctx, mem, u) for mem, u in lignes]
    return filtrer_trier_paginer(
        resultats, page, champs_recherche=("nom", "email", "role"), tri_defaut="nom"
    )


@router.post(
    "",
    response_model=m.Membre,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def ajouter_appartenance(
    corps: m.MembresPostRequest, ctx: Contexte = Depends(exige("member.invite"))
) -> Any:
    u = await ctx.session.get(Utilisateur, corps.userId)
    if u is None:
        raise erreurs.introuvable("Utilisateur", corps.userId)
    existant = (
        await ctx.session.execute(
            select(Membership).where(
                Membership.org_id == ctx.org_id,
                Membership.utilisateur_id == corps.userId,
                Membership.scope_type == (corps.scopeType or "org"),
            )
        )
    ).scalar_one_or_none()
    if existant:
        raise erreurs.conflit(
            "Ce membre est déjà rattaché à l'organisation.", code="membre_existant"
        )
    mem = Membership(
        id=nouvel_id(),
        utilisateur_id=u.id,
        org_id=ctx.org_id,
        role=corps.role,
        scope_type=corps.scopeType or "org",
        scope_id=corps.scopeId,
        statut="actif",
    )
    ctx.session.add(mem)
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="membre.ajout",
        cible_type="membre",
        cible_id=mem.id,
        cible=u.email,
        details={"role": corps.role},
    )
    return membre_contrat(ctx, mem, u)


@router.get("/{membreId}", response_model=m.Membre, response_model_exclude_none=True)
async def obtenir_membre(membreId: str, ctx: Contexte = Depends(exige(None))) -> Any:  # noqa: N803
    mem, u = await obtenir_membership(ctx, membreId)
    return membre_contrat(ctx, mem, u)


@router.patch("/{membreId}", response_model=m.Membre, response_model_exclude_none=True)
async def modifier_membre(
    membreId: str,
    corps: m.MembresMembreIdPatchRequest,
    ctx: Contexte = Depends(exige("member.invite")),
) -> Any:  # noqa: N803
    mem, u = await obtenir_membership(ctx, membreId)
    if corps.role is not None and corps.role != mem.role:
        if corps.role not in ROLES_ORDRE:
            raise erreurs.validation("Rôle inconnu.", {"role": "invalide"})
        if corps.role != "org_admin":
            await _refuser_si_dernier_admin(
                ctx,
                mem,
                "Impossible de rétrograder le dernier administrateur de l'organisation.",
            )
        mem.role = corps.role
    if corps.scopeType is not None:
        mem.scope_type = corps.scopeType
    if corps.scopeId is not None:
        mem.scope_id = corps.scopeId
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="membre.modification",
        cible_type="membre",
        cible_id=mem.id,
        cible=(u.email if u else None),
        details=corps.model_dump(mode="json", exclude_none=True),
    )
    return membre_contrat(ctx, mem, u)


@router.delete("/{membreId}", status_code=status.HTTP_204_NO_CONTENT)
async def retirer_membre(
    membreId: str, confirmation: str | None = None, ctx: Contexte = Depends(exige("member.invite"))
) -> Response:  # noqa: N803
    mem, u = await obtenir_membership(ctx, membreId)
    email = u.email if u else f"membre:{membreId}"
    exiger_confirmation(email, confirmation)
    await _refuser_si_dernier_admin(
        ctx, mem, "Impossible de retirer le dernier administrateur de l'organisation."
    )
    await ctx.session.delete(mem)
    await ctx.session.flush()
    await journaliser(
        ctx, action="membre.retrait", cible_type="membre", cible_id=mem.id, cible=email
    )
    return Response(status_code=204)


@router_invitations.get(
    "", response_model=m.InvitationsGetResponse, response_model_exclude_none=True
)
async def lister_invitations(
    page: Page,
    statut: str | None = None,
    ctx: Contexte = Depends(exige("member.invite", lecture=True)),
) -> Any:
    q = (
        select(Invitation, Organisation)
        .join(Organisation, Organisation.id == Invitation.org_id)
        .where(Invitation.org_id == ctx.org_id)
    )
    if statut:
        q = q.where(Invitation.statut == statut)
    lignes = (await ctx.session.execute(q.order_by(Invitation.cree_le.desc()))).all()
    resultats = [
        invitation_contrat(ctx, inv, org.nom if isinstance(org, Organisation) else None)
        for inv, org in lignes
    ]
    return filtrer_trier_paginer(
        resultats, page, champs_recherche=("email", "role"), tri_defaut="email"
    )


@router_invitations.post(
    "",
    response_model=m.Invitation,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def inviter_membre(
    corps: m.InvitationCreation, ctx: Contexte = Depends(exige("member.invite"))
) -> Any:
    email = corps.email.lower()
    existant = (
        await ctx.session.execute(
            select(Invitation).where(
                Invitation.org_id == ctx.org_id,
                Invitation.email == email,
                Invitation.statut == "en_attente",
            )
        )
    ).scalar_one_or_none()
    if existant:
        raise erreurs.conflit(
            "Une invitation est déjà en attente pour cet email.", code="invitation_existante"
        )
    if corps.role not in ROLES_ORDRE:
        raise erreurs.validation("Rôle inconnu.", {"role": "invalide"})
    jeton_brut = jeton_opaque()
    inv = Invitation(
        id=nouvel_id(),
        org_id=ctx.org_id,
        email=email,
        role=corps.role,
        scope_type=corps.scopeType or "org",
        scope_id=corps.scopeId,
        jeton_hash=hacher_jeton(jeton_brut),
        invite_par=ctx.utilisateur_id,
        statut="en_attente",
        expire_le=dans(DUREE_INVITATION_S),
        message=corps.message,
    )
    ctx.session.add(inv)
    await ctx.session.flush()
    org = await ctx.session.get(Organisation, ctx.org_id)
    lien = f"{ctx.reglages.url_frontend}/invitation/{jeton_brut}"
    org_nom = org.nom if org else "une organisation"
    paragraphes = [f"Vous avez été invité·e à rejoindre {org_nom} en tant que {corps.role}."]
    if corps.message:
        paragraphes.append(corps.message)
    paragraphes.append("Ce lien est valable 7 jours.")
    await courriel.envoyer(
        email,
        f"Invitation à rejoindre {org_nom} sur Synelia Cloud",
        f"Rejoignez {org_nom}",
        paragraphes,
        bouton_texte="Accepter l'invitation",
        bouton_url=lien,
    )
    await journaliser(
        ctx,
        action="membre.invitation",
        cible_type="invitation",
        cible_id=inv.id,
        cible=email,
        details={"role": corps.role, "scopeType": corps.scopeType},
    )
    return invitation_contrat(ctx, inv, None)


async def _obtenir_invitation(ctx: Contexte, inv_id: str) -> Invitation:
    inv = (
        await ctx.session.execute(
            select(Invitation).where(Invitation.id == inv_id, Invitation.org_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if inv is None:
        raise erreurs.introuvable("Invitation", inv_id)
    return inv


@router_invitations.post(
    "/{invitationId}/relance", response_model=m.Invitation, response_model_exclude_none=True
)
async def relancer_invitation(
    invitationId: str, ctx: Contexte = Depends(exige("member.invite"))
) -> Any:  # noqa: N803
    inv = await _obtenir_invitation(ctx, invitationId)
    if inv.statut != "en_attente":
        raise erreurs.conflit(
            "Seule une invitation en attente peut être relancée.", code="invitation_non_relancable"
        )
    inv.expire_le = dans(DUREE_INVITATION_S)
    org = await ctx.session.get(Organisation, inv.org_id)
    await journaliser(
        ctx,
        action="membre.invitation_relance",
        cible_type="invitation",
        cible_id=inv.id,
        cible=inv.email,
    )
    return invitation_contrat(ctx, inv, org.nom if org else None)


@router_invitations.delete(
    "/{invitationId}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def revoquer_invitation(
    invitationId: str, ctx: Contexte = Depends(exige("member.invite"))
) -> Response:  # noqa: N803
    inv = await _obtenir_invitation(ctx, invitationId)
    inv.statut = "revoquee"
    await ctx.session.flush()
    await journaliser(
        ctx,
        action="membre.invitation_revocation",
        cible_type="invitation",
        cible_id=inv.id,
        cible=inv.email,
    )
    return Response(status_code=204)
