from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from synelia_db.base import Base, DateTimeUTC, Horodate, Identifie


class Ressource(Base, Identifie, Horodate):
    """Document typé par une classe du contrat.

    Chaque module possède un `Depot[T]` (T = modèle Pydantic généré) qui lit et écrit ici,
    scellé par `org_id`. Les ressources plateforme (catalogue, backends, offres) ont `org_id` NULL.
    Le module reste propriétaire de sa logique : ce n'est pas un CRUD générique exposé,
    c'est une persistance générique derrière des routes écrites une par une."""

    __tablename__ = "ressources"
    __table_args__ = (
        Index("ix_ressources_type_org", "type", "org_id"),
        Index("ix_ressources_parent", "type", "parent_id"),
        Index("ix_ressources_nom", "type", "org_id", "nom"),
        Index("ix_ressources_cree_par", "cree_par_id"),
    )

    org_id: Mapped[str | None] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(60))
    nom: Mapped[str | None] = mapped_column(String(300))
    parent_id: Mapped[str | None] = mapped_column(String(120))
    statut: Mapped[str | None] = mapped_column(String(40))
    cree_par_id: Mapped[str | None] = mapped_column(
        String(36)
    )  # utilisateurs.id du principal qui a fait le POST — NULL pour l'amorçage plateforme
    donnees: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secrets: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # chiffrés, jamais sérialisés dans `donnees`
    supprime_le: Mapped[datetime | None] = mapped_column(DateTimeUTC)
