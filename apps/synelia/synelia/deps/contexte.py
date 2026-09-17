"""Le contexte d'une requête : session base, principal, organisation active, corrélation.

`Ctx` = authentifié (jeton ou clé d'API) ; `CtxPublic` = vitrine sans authentification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from synelia_contract.rbac import ROLES_EQUIPE
from synelia_db import rls
from synelia_db.modeles import CleApi, Membership, Organisation, SessionAuth, Utilisateur
from synelia_db.session import fabrique
from synelia_kernel import erreurs
from synelia_kernel.config import Reglages, reglages
from synelia_kernel.dates import maintenant
from synelia_kernel.journal import org_id_courant, utilisateur_id_courant

from synelia.deps import limitation
from synelia.securite import (
    hacher_jeton,
    ip_autorisee,
    lire_acces,
    politiques_securite,
    role_effectif_equipe,
)


@dataclass
class Principal:
    utilisateur_id: str | None
    email: str
    nom: str
    org_id: str | None
    role: str
    session_id: str | None = None
    cle_api_id: str | None = None
    portee: list[str] = field(default_factory=list)
    emprunt: bool = False
    equipe: bool = False  # membre de l'équipe Synelia (super admin / opérateur)
    role_equipe: str | None = None
    roles_par_org: dict[str, str] = field(default_factory=dict)

    @property
    def est_admin_plateforme(self) -> bool:
        return self.equipe and (self.role_equipe in ROLES_EQUIPE)


@dataclass
class Contexte:
    request: Request
    session: AsyncSession
    reglages: Reglages
    correlation_id: str
    principal: Principal | None = None
    langue: str = "fr"

    @property
    def org_id(self) -> str:
        if self.principal is None or not self.principal.org_id:
            raise erreurs.AppError(
                "organisation_requise", 400, "Aucune organisation active pour cette session."
            )
        return self.principal.org_id

    @property
    def org_id_ou_none(self) -> str | None:
        return self.principal.org_id if self.principal else None

    @property
    def role(self) -> str:
        return self.principal.role if self.principal else "anonyme"

    @property
    def utilisateur_id(self) -> str | None:
        return self.principal.utilisateur_id if self.principal else None

    @property
    def ip(self) -> str | None:
        return self.request.client.host if self.request.client else None

    def entete(self, nom: str) -> str | None:
        return self.request.headers.get(nom)


async def _session() -> AsyncIterator[AsyncSession]:
    async with fabrique()() as s:
        try:
            yield s
            if s.in_transaction():
                await s.commit()
        except Exception:
            if s.in_transaction():
                await s.rollback()
            raise


async def contexte_public(
    request: Request,
    session: Annotated[AsyncSession, Depends(_session)],
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Contexte:
    limitation.verifier(request)
    return Contexte(
        request=request,
        session=session,
        reglages=reglages(),
        correlation_id=getattr(request.state, "correlation_id", "-"),
        langue=(accept_language or "fr")[:2],
    )


async def _politiques_org(session: AsyncSession, org_id: str | None) -> dict[str, Any]:
    if not org_id:
        return {}
    o = await session.get(Organisation, org_id)
    return politiques_securite(o.politiques) if o else {}


async def _verifier_organisation_active(
    session: AsyncSession, org_id: str | None, *, admin_plateforme: bool
) -> None:
    """Une organisation suspendue coupe l'accès de ses membres à *chaque* requête (pas
    seulement à la connexion) — une session déjà ouverte avant la suspension ne doit pas
    survivre jusqu'à son expiration naturelle. L'équipe Synelia garde l'accès (elle doit
    pouvoir consulter/réactiver l'organisation qu'elle vient de suspendre)."""
    if not org_id or admin_plateforme:
        return
    o = await session.get(Organisation, org_id)
    if o is not None and o.statut == "suspendue":
        raise erreurs.interdit("Organisation suspendue.", code="organisation_suspendue")


async def _principal_depuis_jeton(session: AsyncSession, jeton: str) -> Principal:
    claims = lire_acces(jeton)
    sid = claims.get("sid")
    if sid:
        s = await session.get(SessionAuth, sid)
        if s is None or s.revoquee_le is not None or s.expire_le < maintenant():
            raise erreurs.non_authentifie("Session révoquée ou expirée.")
        if not s.mfa_validee:
            raise erreurs.non_authentifie("Second facteur requis.")
        inactivite_min = (
            (await _politiques_org(session, s.org_id)).get("session", {}).get("inactiviteMin")
        )
        reference = s.derniere_activite_le or s.cree_le
        if (
            inactivite_min
            and reference
            and maintenant() - reference > timedelta(minutes=inactivite_min)
        ):
            s.revoquee_le = maintenant()
            raise erreurs.non_authentifie("Session expirée pour inactivité.")
        s.derniere_activite_le = maintenant()
    u = await session.get(Utilisateur, claims["sub"])
    if u is None or u.statut == "suspendu":
        raise erreurs.non_authentifie("Compte inconnu ou suspendu.")
    membres = (
        (await session.execute(select(Membership).where(Membership.utilisateur_id == u.id)))
        .scalars()
        .all()
    )
    roles = {m.org_id: m.role for m in membres if m.scope_type == "org"}
    equipe = u.equipe or {}
    role_equipe = role_effectif_equipe(equipe)
    org_id = claims.get("org")
    admin_plateforme = bool(equipe) and role_equipe in ROLES_EQUIPE
    await _verifier_organisation_active(session, org_id, admin_plateforme=admin_plateforme)
    if admin_plateforme:
        role = role_equipe or claims.get("role") or "read_only"
    elif org_id:
        # Rôle réel de l'organisation à *cette* requête, jamais celui figé dans le jeton à
        # la connexion (`claims["role"]`) : sinon un changement de rôle via
        # `PATCH /v1/membres/{id}` ne prend effet qu'à l'expiration/rafraîchissement du
        # jeton déjà émis (jusqu'à 15 min), ce qui vide de son sens toute rétrogradation
        # de sécurité (compte compromis, offboarding). `roles` vient d'une lecture Membership
        # fraîche faite plus haut pour *chaque* requête authentifiée (déjà nécessaire pour
        # `roles_par_org`) : pas de requête supplémentaire, donc pas de coût additionnel.
        # Absent de `roles` (membre retiré entre-temps) : on ne retombe jamais sur le rôle du
        # jeton, seulement sur `read_only` — même logique que l'organisation suspendue.
        role = roles.get(org_id) or "read_only"
    else:
        role = claims.get("role") or "read_only"
    return Principal(
        utilisateur_id=u.id,
        email=u.email,
        nom=u.nom,
        org_id=org_id,
        role=role,
        session_id=sid,
        emprunt=bool(claims.get("emprunt")),
        equipe=bool(equipe),
        role_equipe=role_equipe,
        roles_par_org=roles,
    )


async def _principal_depuis_cle(session: AsyncSession, cle: str) -> Principal:
    ligne = (
        await session.execute(
            select(CleApi).where(
                CleApi.secret_hash == hacher_jeton(cle), CleApi.revoquee_le.is_(None)
            )
        )
    ).scalar_one_or_none()
    if ligne is None or (ligne.expire_le and ligne.expire_le < maintenant()):
        raise erreurs.non_authentifie("Clé d'API inconnue, révoquée ou expirée.")
    ligne.derniere_utilisation_le = maintenant()
    ligne.utilisations = (ligne.utilisations or 0) + 1
    return Principal(
        utilisateur_id=None,
        email=f"cle:{ligne.prefixe}",
        nom=ligne.nom,
        org_id=ligne.org_id,
        role=ligne.role_emetteur,
        cle_api_id=ligne.id,
        portee=list(ligne.portee or []),
    )


async def _verifier_restriction_ip(
    session: AsyncSession, request: Request, principal: Principal
) -> None:
    """`restrictionIp` réelle : coupe l'accès si l'IP de la requête n'est dans aucune plage
    autorisée couvrant la portée (portail/API) de ce principal."""
    politiques = await _politiques_org(session, principal.org_id)
    restriction = politiques.get("restrictionIp", {})
    if not restriction.get("actif"):
        return
    if restriction.get("appliqueAuxAdmins") is False and principal.est_admin_plateforme:
        return
    portee_requise = "api" if principal.cle_api_id else "portail"
    ip = request.client.host if request.client else None
    if not ip_autorisee(ip, restriction.get("plages", []), portee_requise):
        raise erreurs.interdit(
            "Adresse IP non autorisée par la politique de sécurité de l'organisation.",
            code="ip_non_autorisee",
        )


async def contexte(
    request: Request,
    session: Annotated[AsyncSession, Depends(_session)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    x_organisation_id: Annotated[str | None, Header(alias="X-Organisation-Id")] = None,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> Contexte:
    limitation.verifier(request)
    if authorization and authorization.lower().startswith("bearer "):
        principal = await _principal_depuis_jeton(session, authorization[7:].strip())
    elif x_api_key:
        principal = await _principal_depuis_cle(session, x_api_key)
    else:
        raise erreurs.non_authentifie()

    if x_organisation_id and x_organisation_id != principal.org_id:
        if principal.est_admin_plateforme:
            principal.org_id = x_organisation_id
            principal.role = principal.role_equipe or principal.role
        elif x_organisation_id in principal.roles_par_org:
            await _verifier_organisation_active(session, x_organisation_id, admin_plateforme=False)
            principal.org_id = x_organisation_id
            principal.role = principal.roles_par_org[x_organisation_id]
        else:
            raise erreurs.interdit(
                "Vous n'appartenez pas à cette organisation.", code="organisation_interdite"
            )

    rls.org_id_transaction.set(principal.org_id)
    org_id_courant.set(principal.org_id)
    utilisateur_id_courant.set(principal.utilisateur_id)
    # La résolution du principal ci-dessus (`_principal_depuis_jeton`/`_principal_depuis_cle`)
    # a déjà exécuté des requêtes (lecture de `sessions_auth`/`utilisateurs`/`memberships`/
    # `cles_api`) avant que `org_id` soit connu — la transaction Postgres est donc déjà ouverte,
    # avec `app.org_id` posé à `''` par l'écouteur `begin` (RLS no-op le temps de cette
    # résolution, nécessaire : on ne sait pas encore à quelle organisation restreindre). Poser
    # `org_id_transaction.set(...)` seul ne suffit plus à corriger `app.org_id` sur cette
    # transaction déjà commencée (l'écouteur ne se redéclenche pas) : `rls.poser()` l'applique
    # directement, pour que toutes les requêtes métier qui suivent dans cette même transaction
    # soient bien filtrées par la RLS Postgres, pas seulement par les filtres applicatifs.
    await rls.poser(session, principal.org_id)
    if principal.org_id:
        await _verifier_restriction_ip(session, request, principal)
    request.state.principal = principal
    return Contexte(
        request=request,
        session=session,
        reglages=reglages(),
        correlation_id=getattr(request.state, "correlation_id", "-"),
        principal=principal,
        langue=(accept_language or "fr")[:2],
    )


Ctx = Annotated[Contexte, Depends(contexte)]
CtxPublic = Annotated[Contexte, Depends(contexte_public)]
