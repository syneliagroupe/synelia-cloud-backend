from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column
from synelia_kernel.dates import maintenant

from synelia_db.base import Base, DateTimeUTC, Identifie


class Audit(Base, Identifie):
    """Append-only imposé par Postgres : le rôle applicatif (`synelia_app`) n'a que
    `SELECT, INSERT` sur cette table (propriétaire : `synelia`, hors-ligne) — voir §3 de
    `docs/PLAN-ARCHITECTURE-SUITE.md`. Hash chaîné par organisation sur `hash_precedent`.
    Aucune route ne modifie ni ne supprime une ligne : le code le respecte, et depuis ce plan
    les droits Postgres l'imposent même en cas de code applicatif compromis."""

    __tablename__ = "audit"
    __table_args__ = (Index("ix_audit_org_date", "org_id", "date"),)

    org_id: Mapped[str | None] = mapped_column(String(36))
    date: Mapped[datetime] = mapped_column(DateTimeUTC, default=maintenant)
    acteur_id: Mapped[str | None] = mapped_column(String(36))
    acteur: Mapped[str] = mapped_column(String(320), default="systeme")
    action: Mapped[str] = mapped_column(String(120), index=True)
    cible_type: Mapped[str | None] = mapped_column(String(60))
    cible_id: Mapped[str | None] = mapped_column(String(120))
    cible: Mapped[str | None] = mapped_column(String(300))
    resultat: Mapped[str] = mapped_column(String(20), default="succes")
    ip: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    hash_precedent: Mapped[str | None] = mapped_column(String(128))
    hash: Mapped[str | None] = mapped_column(String(128))
