from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from synelia_kernel.dates import maintenant

from synelia_db.base import Base, DateTimeUTC, Horodate, Identifie


class Travail(Base, Identifie, Horodate):
    """Projection d'un travail de provisioning : `id` = identifiant du workflow."""

    __tablename__ = "travaux"
    __table_args__ = (Index("ix_travaux_org_statut", "org_id", "statut"),)

    org_id: Mapped[str | None] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(60), index=True)
    label: Mapped[str] = mapped_column(String(300))
    statut: Mapped[str] = mapped_column(String(20), default="queued")
    taches: Mapped[list[Any]] = mapped_column(default=list, nullable=False)
    erreur: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=maintenant)
    termine_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    duree_s: Mapped[int | None] = mapped_column(Integer)
    essai: Mapped[int] = mapped_column(Integer, default=0)
    cible_type: Mapped[str | None] = mapped_column(String(60))
    cible_id: Mapped[str | None] = mapped_column(String(36), index=True)
    entree: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    contexte: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    demande_par: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
