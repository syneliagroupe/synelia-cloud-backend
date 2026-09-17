"""`/moi/*` : compte et organisation active de la session."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_contract.rbac import permissions_effectives
from synelia_db.modeles import Organisation, SessionAuth, Utilisateur
from synelia_kernel import erreurs
from synelia_kernel.chiffrement import chiffrer
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import jeton_opaque

from synelia.audit import journaliser
from synelia.deps import Ctx
from synelia.modules.auth import service as auth
from synelia.securite import (
    hacher_mot_de_passe,
    nouveau_secret_totp,
    uri_totp,
    verifier_mot_de_passe,
)

router = APIRouter(prefix="/moi", tags=["Compte & organisation active"])


async def _moi(ctx) -> Utilisateur:
    p = ctx.principal
    if not p or not p.utilisateur_id:
        raise erreurs.interdit("Une clé d'API n'a pas de compte.", code="cle_api")
    u = await ctx.session.get(Utilisateur, p.utilisateur_id)
    assert u is not None
    return u


@router.get("", response_model=m.MoiGetResponse, response_model_exclude_none=True)
async def obtenir_mon_compte(ctx: Ctx) -> Any:
    u = await _moi(ctx)
    apps = await auth.appartenances(ctx.session, u)
    role = ctx.role
    p = ctx.principal
    if p and p.est_admin_plateforme:
        role = (
            p.role_equipe or role
        )  # l'équipe Synelia voit ses droits plateforme, pas ceux du rôle d'org
    perms = [a for a, perm in permissions_effectives(role).items() if perm != "none"]
    return {
        "utilisateur": auth.utilisateur_contrat(u),
        "organisations": apps,
        "organisationActive": ctx.org_id_ou_none,
        "roleActif": role,
        "permissions": perms,
    }


@router.patch("", response_model=m.Utilisateur, response_model_exclude_none=True)
async def modifier_mon_compte(ctx: Ctx, corps: m.MoiPatchRequest) -> Any:
    u = await _moi(ctx)
    if corps.nom:
        u.nom = corps.nom
    if corps.fonction is not None:
        u.fonction = corps.fonction
    if corps.telephone is not None:
        u.preferences = {**(u.preferences or {}), "telephone": corps.telephone}
    await journaliser(ctx, action="compte.modification", cible_type="utilisateur", cible_id=u.id)
    return auth.utilisateur_contrat(u)


@router.get(
    "/organisations",
    response_model=list[m.AppartenanceOrganisation],
    response_model_exclude_none=True,
)
async def lister_mes_organisations(ctx: Ctx) -> Any:
    return await auth.appartenances(ctx.session, await _moi(ctx))


@router.put("/organisation-active", response_model=m.Session, response_model_exclude_none=True)
async def choisir_organisation_active(ctx: Ctx, corps: m.MoiOrganisationActivePutRequest) -> Any:
    u = await _moi(ctx)
    p = ctx.principal
    assert p is not None
    if corps.orgId not in p.roles_par_org and not p.est_admin_plateforme:
        raise erreurs.validation(
            "Vous n'appartenez pas à cette organisation.", {"orgId": "inconnue"}
        )
    if p.est_admin_plateforme and await ctx.session.get(Organisation, corps.orgId) is None:
        # L'équipe Synelia n'est pas bornée à `roles_par_org` (elle peut ouvrir n'importe
        # quelle organisation cliente), mais un identifiant inexistant filait quand même
        # jusqu'à l'insertion de la session, où la RLS Postgres le refusait en 500 brut.
        raise erreurs.validation("Organisation introuvable.", {"orgId": "inconnue"})
    if corps.memoriser:
        u.org_active_id = corps.orgId
    rep = await auth.ouvrir_session(
        ctx.session, u, ip=ctx.ip, user_agent=ctx.entete("user-agent"), org_id=corps.orgId
    )
    await journaliser(
        ctx,
        action="compte.organisation_active_changee",
        cible_type="organisation",
        cible_id=corps.orgId,
        org_id=corps.orgId,
    )
    return rep


@router.get("/preferences", response_model=m.Preferences, response_model_exclude_none=True)
async def obtenir_mes_preferences(ctx: Ctx) -> Any:
    u = await _moi(ctx)
    return {
        "langue": "fr",
        "fuseau": "Africa/Abidjan",
        "deviseAffichee": "XOF",
        **(u.preferences or {}),
    }


@router.put("/preferences", response_model=m.Preferences, response_model_exclude_none=True)
async def modifier_mes_preferences(ctx: Ctx, corps: m.Preferences) -> Any:
    u = await _moi(ctx)
    u.preferences = {**(u.preferences or {}), **corps.model_dump(mode="json", exclude_none=True)}
    return u.preferences


@router.put("/mot-de-passe", response_model=m.MoiMotDePassePutResponse)
async def changer_mon_mot_de_passe(ctx: Ctx, corps: m.MoiMotDePassePutRequest) -> Any:
    u = await _moi(ctx)
    if not verifier_mot_de_passe(corps.actuel.get_secret_value(), u.mot_de_passe_hash):
        raise erreurs.validation("Mot de passe actuel incorrect.", {"actuel": "incorrect"})
    if len(corps.nouveau.get_secret_value()) < 8:
        raise erreurs.validation("Mot de passe trop court.", {"nouveau": "8 caractères minimum"})
    u.mot_de_passe_hash = hacher_mot_de_passe(corps.nouveau.get_secret_value())
    await journaliser(
        ctx, action="compte.mot_de_passe_change", cible_type="utilisateur", cible_id=u.id
    )
    return {"change": True}


@router.post(
    "/mfa",
    response_model=m.MoiMfaPostResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def activer_mon_mfa(ctx: Ctx, corps: m.MoiMfaPostRequest) -> Any:
    if corps.methode != "totp":
        raise erreurs.non_porte("Seule la méthode TOTP est disponible pour l'instant.")
    u = await _moi(ctx)
    secret = nouveau_secret_totp()
    u.mfa_secret_chiffre = chiffrer(secret)
    u.mfa_active = True
    codes = [jeton_opaque(6)[:10] for _ in range(8)]
    u.preferences = {
        **(u.preferences or {}),
        "codes_secours_hash": [hacher_mot_de_passe(c) for c in codes],
    }
    await journaliser(ctx, action="compte.mfa_activee", cible_type="utilisateur", cible_id=u.id)
    return {"secret": secret, "urlOtpauth": uri_totp(secret, u.email), "codesSecours": codes}


@router.delete("/mfa", status_code=status.HTTP_204_NO_CONTENT)
async def desactiver_mon_mfa(ctx: Ctx) -> Response:
    u = await _moi(ctx)
    if not u.mfa_active:
        raise erreurs.conflit("Le MFA n'est pas activé.", code="mfa_inactif")
    u.mfa_active = False
    u.mfa_secret_chiffre = None
    await journaliser(ctx, action="compte.mfa_desactivee", cible_type="utilisateur", cible_id=u.id)
    return Response(status_code=204)


def session_contrat(s: SessionAuth, u: Utilisateur | None, courante: str | None) -> dict[str, Any]:
    return {
        "id": s.id,
        "userId": s.utilisateur_id,
        "utilisateurNom": u.nom if u else None,
        "email": u.email if u else None,
        "ip": s.ip or "-",
        "agent": s.user_agent,
        "idpSource": u.idp_source if u else None,
        "debut": s.cree_le or maintenant(),
        "derniereActivite": s.derniere_activite_le or s.cree_le or maintenant(),
        "courante": s.id == courante,
    }


@router.get("/sessions", response_model=list[m.SessionActive], response_model_exclude_none=True)
async def lister_mes_sessions(ctx: Ctx) -> Any:
    u = await _moi(ctx)
    lignes = (
        await ctx.session.execute(
            select(SessionAuth)
            .where(
                SessionAuth.utilisateur_id == u.id,
                SessionAuth.revoquee_le.is_(None),
                SessionAuth.expire_le > maintenant(),
            )
            .order_by(SessionAuth.cree_le.desc())
        )
    ).scalars()
    return [
        session_contrat(s, u, ctx.principal.session_id if ctx.principal else None) for s in lignes
    ]


@router.get("/lanceur", response_model=m.MoiLanceurGetResponse)
async def obtenir_mon_lanceur(ctx: Ctx) -> Any:
    from synelia.depot import Depot

    services = await Depot("service_manage", m.ServiceManage).tous(ctx)
    return [
        {"service": s, "urlOuverture": f"{ctx.reglages.url_publique}/v1/services/{s.id}/ouverture"}
        for s in services
    ]
