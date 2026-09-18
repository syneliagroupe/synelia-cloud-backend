from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from synelia_db.base import Base, DateTimeUTC, Horodate, Identifie


class Organisation(Base, Identifie, Horodate):
    __tablename__ = "organisations"

    nom: Mapped[str] = mapped_column(String(200), unique=True)
    pays: Mapped[str] = mapped_column(String(2), default="CI")
    secteur: Mapped[str | None] = mapped_column(String(100))
    tva: Mapped[str | None] = mapped_column(String(50))
    statut: Mapped[str] = mapped_column(String(20), default="active")
    logo_url: Mapped[str | None] = mapped_column(String(500))
    tenant_plan: Mapped[str | None] = mapped_column(String(50))
    domaine: Mapped[str | None] = mapped_column(String(253), index=True)
    keystone_domaine_id: Mapped[str | None] = mapped_column(String(64))
    politiques: Mapped[dict[str, Any]] = mapped_column(default=dict)
    sso: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    onboarding: Mapped[dict[str, Any] | None] = mapped_column(default=None)


class Utilisateur(Base, Identifie, Horodate):
    __tablename__ = "utilisateurs"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    nom: Mapped[str] = mapped_column(String(200))
    mot_de_passe_hash: Mapped[str | None] = mapped_column(String(300))
    mfa_secret_chiffre: Mapped[str | None] = mapped_column(String(300))
    mfa_active: Mapped[bool] = mapped_column(Boolean, default=False)
    idp_source: Mapped[str] = mapped_column(String(10), default="local")
    fonction: Mapped[str | None] = mapped_column(String(120))
    statut: Mapped[str] = mapped_column(String(20), default="actif")
    dernier_login_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    org_active_id: Mapped[str | None] = mapped_column(String(36))
    preferences: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reinit_jeton_hash: Mapped[str | None] = mapped_column(String(128))
    reinit_expire_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    equipe: Mapped[dict[str, Any] | None] = mapped_column(
        default=None
    )  # membre de l'équipe Synelia : rôle, élévation


class Membership(Base, Identifie, Horodate):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint(
            "utilisateur_id", "org_id", "scope_type", "scope_id", name="uq_membership"
        ),
        Index("ix_memberships_org", "org_id"),
    )

    utilisateur_id: Mapped[str] = mapped_column(ForeignKey("utilisateurs.id", ondelete="CASCADE"))
    org_id: Mapped[str] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(30))
    scope_type: Mapped[str] = mapped_column(String(20), default="org")
    scope_id: Mapped[str | None] = mapped_column(String(36), default=None)
    statut: Mapped[str] = mapped_column(String(20), default="actif")


class Invitation(Base, Identifie, Horodate):
    __tablename__ = "invitations"

    org_id: Mapped[str] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(30))
    scope_type: Mapped[str] = mapped_column(String(20), default="org")
    scope_id: Mapped[str | None] = mapped_column(String(36))
    jeton_hash: Mapped[str] = mapped_column(String(128), unique=True)
    invite_par: Mapped[str | None] = mapped_column(String(36))
    statut: Mapped[str] = mapped_column(String(20), default="en_attente")
    expire_le: Mapped[datetime] = mapped_column(DateTimeUTC)
    message: Mapped[str | None] = mapped_column(String(1000))


class VerificationEmail(Base, Identifie, Horodate):
    """Code à 6 chiffres de vérification d'email (inscription) : hash stocké, 15 min,
    5 essais max, un seul code actif par utilisateur (les précédents sont consommés)."""

    __tablename__ = "verifications_email"

    utilisateur_id: Mapped[str] = mapped_column(
        ForeignKey("utilisateurs.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(128))
    expire_le: Mapped[datetime] = mapped_column(DateTimeUTC)
    essais: Mapped[int] = mapped_column(Integer, default=0)
    consommee_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)


class SessionAuth(Base, Identifie, Horodate):
    """Rafraîchissement opaque rotatif ; l'accès reste sans état (JWT 15 min)."""

    __tablename__ = "sessions_auth"

    utilisateur_id: Mapped[str] = mapped_column(
        ForeignKey("utilisateurs.id", ondelete="CASCADE"), index=True
    )
    org_id: Mapped[str | None] = mapped_column(String(36), index=True)
    role: Mapped[str | None] = mapped_column(String(30))
    famille: Mapped[str] = mapped_column(String(36), index=True)
    rafraichissement_hash: Mapped[str] = mapped_column(String(128), unique=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    expire_le: Mapped[datetime] = mapped_column(DateTimeUTC)
    revoquee_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    derniere_activite_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    emprunt: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_defi: Mapped[str | None] = mapped_column(String(64))
    mfa_validee: Mapped[bool] = mapped_column(Boolean, default=True)


class CleApi(Base, Identifie, Horodate):
    """Clé d'API, deux portées selon `org_id` :

    - `org_id` renseigné : clé d'organisation, gérée par l'organisation elle-même
      (`/securite/cles-api`).
    - `org_id` NULL : jeton plateforme (super admin), géré uniquement via `/admin/jetons` et
      utilisable sans `X-Organisation-Id` — ses droits sont ceux de `role_emetteur`, bornés par
      `portee`.

    Postgres existant : `ALTER TABLE cles_api ALTER COLUMN org_id DROP NOT NULL;` (ADR 0003,
    à exécuter par le propriétaire de la table avant de déployer l'image qui porte ce modèle)."""

    __tablename__ = "cles_api"

    org_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    nom: Mapped[str] = mapped_column(String(120))
    prefixe: Mapped[str] = mapped_column(String(20), index=True)
    secret_hash: Mapped[str] = mapped_column(String(128), unique=True)
    portee: Mapped[list[Any]] = mapped_column(default=list)
    role_emetteur: Mapped[str] = mapped_column(String(30))
    cree_par: Mapped[str | None] = mapped_column(String(36))
    expire_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    derniere_utilisation_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    revoquee_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    ips_autorisees: Mapped[list[Any]] = mapped_column(default=list)
    utilisations: Mapped[int] = mapped_column(Integer, default=0)
