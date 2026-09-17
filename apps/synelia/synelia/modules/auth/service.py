"""Sessions : connexion, MFA, rafraîchissement rotatif, emprunt d'identité."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from synelia_contract import rbac
from synelia_db import rls
from synelia_db.modeles import Membership, Organisation, SessionAuth, Utilisateur
from synelia_kernel.config import reglages
from synelia_kernel.dates import dans, maintenant
from synelia_kernel.ids import jeton_opaque, nouvel_id

from synelia.securite import emettre_acces, hacher_jeton, politiques_securite


def utilisateur_contrat(u: Utilisateur) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "nom": u.nom,
        "mfaEnabled": bool(u.mfa_active),
        "idpSource": u.idp_source,
        "lastLoginAt": u.dernier_login_le,
        "orgId": u.org_active_id,
        "fonction": u.fonction,
        "statut": u.statut,
    }


async def appartenances(session, u: Utilisateur) -> list[dict[str, Any]]:
    """Liste **toutes** les organisations de l'utilisateur, pas seulement celle active dans la
    session courante — cf. `rls.sans_org` : sans lever temporairement la RLS de `memberships`
    ici, un utilisateur multi-organisation ne verrait jamais ses autres organisations dans le
    sélecteur (`/select-organisation`, la bascule de la barre supérieure)."""
    async with rls.sans_org(session):
        lignes = (
            await session.execute(
                select(Membership, Organisation)
                .join(Organisation, Organisation.id == Membership.org_id)
                .where(Membership.utilisateur_id == u.id, Membership.scope_type == "org")
            )
        ).all()
    return [
        {
            "orgId": o.id,
            "nom": o.nom,
            "secteur": o.secteur,
            "role": m.role,
            "logoUrl": o.logo_url,
            "defaut": o.id == u.org_active_id,
        }
        for m, o in lignes
    ]


def role_dans(apps: list[dict[str, Any]], org_id: str | None, u: Utilisateur) -> str:
    for a in apps:
        if a["orgId"] == org_id:
            return a["role"]
    if u.equipe and u.equipe.get("role") in rbac.ROLES_EQUIPE:
        return u.equipe["role"]
    return "read_only"


async def ouvrir_session(
    session,
    u: Utilisateur,
    *,
    ip: str | None,
    user_agent: str | None,
    org_id: str | None = None,
    mfa_validee: bool = True,
    emprunt: bool = False,
    duree_s: int | None = None,
    famille: str | None = None,
) -> dict[str, Any]:
    """Crée la ligne de session et renvoie la `Session` du contrat (ou le défi MFA)."""
    r = reglages()
    apps = await appartenances(session, u)
    org = org_id or u.org_active_id or (apps[0]["orgId"] if apps else None)
    role = role_dans(apps, org, u)
    politiques_session: dict[str, Any] = {}
    if org:
        o = await session.get(Organisation, org)
        if o:
            politiques_session = politiques_securite(o.politiques).get("session", {})
    duree_effective = duree_s
    if duree_effective is None and politiques_session.get("dureeMaxMin"):
        duree_effective = politiques_session["dureeMaxMin"] * 60
    brut = jeton_opaque()
    s = SessionAuth(
        id=nouvel_id(),
        utilisateur_id=u.id,
        org_id=org,
        role=role,
        famille=famille or nouvel_id(),
        rafraichissement_hash=hacher_jeton(brut),
        ip=ip,
        user_agent=(user_agent or "")[:400],
        expire_le=dans(duree_effective or r.rafraichissement_duree_s),
        derniere_activite_le=maintenant(),
        emprunt=emprunt,
        mfa_validee=mfa_validee,
        mfa_defi=None if mfa_validee else nouvel_id(),
    )
    session.add(s)
    if mfa_validee and org and politiques_session.get("sessionUniqueParUtilisateur"):
        # une seule session active par utilisateur (et par organisation) quand exigé
        for autre in (
            await session.execute(
                select(SessionAuth).where(
                    SessionAuth.utilisateur_id == u.id,
                    SessionAuth.org_id == org,
                    SessionAuth.revoquee_le.is_(None),
                    SessionAuth.id != s.id,
                )
            )
        ).scalars():
            autre.revoquee_le = maintenant()
    u.dernier_login_le = maintenant()
    if org and not u.org_active_id:
        u.org_active_id = org
    await session.flush()
    if not mfa_validee:
        return {
            "expiresIn": 300,
            "mfaRequis": True,
            "defiMfa": s.mfa_defi,
            "utilisateur": utilisateur_contrat(u),
        }
    acces = emettre_acces(
        {"sub": u.id, "org": org, "role": role, "sid": s.id, "emprunt": emprunt}, duree_s
    )
    return {
        "accessToken": acces,
        "refreshToken": brut,
        "expiresIn": duree_s or r.acces_duree_s,
        "utilisateur": utilisateur_contrat(u),
        "organisations": apps,
        "organisationActive": org,
        "roleActif": role,
        "mfaRequis": False,
    }


async def valider_defi(session, defi: str) -> SessionAuth | None:
    s = (
        await session.execute(
            select(SessionAuth).where(
                SessionAuth.mfa_defi == defi, SessionAuth.mfa_validee.is_(False)
            )
        )
    ).scalar_one_or_none()
    return s


async def mfa_exigee(session, u: Utilisateur, org_id: str | None) -> bool:
    if u.mfa_active:
        return True
    if org_id:
        o = await session.get(Organisation, org_id)
        if o and (o.politiques or {}).get("mfa", {}).get("obligatoire"):
            return True
    return False
