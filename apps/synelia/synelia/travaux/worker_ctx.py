"""Exécution d'un travail hors requête HTTP (worker Temporal, worker Local — `travaux/local.py`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from synelia_db.modeles import Travail
from synelia_db.session import fabrique
from synelia_kernel.config import reglages

from synelia.deps.contexte import Contexte, Principal
from synelia.travaux import moteur


def contexte_travail(session: AsyncSession, travail: Travail) -> Contexte:
    """Construit le `Contexte` d'un travail rejoué hors requête HTTP : une fausse `Request`
    (aucun en-tête — un exécuteur qui en dépendrait devrait être corrigé, pas ce helper) et un
    `Principal` repris de `travail.contexte["principal"]` (posé par `moteur.demarrer_travail` en
    mode worker, cf. étape 1.3 — mêmes lignes d'audit qu'un `create_task`, portant l'e-mail réel
    de l'utilisateur) s'il existe, sinon un principal synthétique `platform_operator` (travaux
    créés avant ce champ, ou par un code interne comme `espaces.service._provisionner_zone_vps`)."""
    faux_request: Any = SimpleNamespace(
        headers={},
        client=None,
        state=SimpleNamespace(correlation_id=travail.correlation_id or "-"),
    )
    p = (travail.contexte or {}).get("principal")
    principal = (
        Principal(
            utilisateur_id=p.get("utilisateur_id"),
            email=p.get("email", "worker"),
            nom=p.get("nom", "worker"),
            org_id=p.get("org_id"),
            role=p.get("role", "platform_operator"),
            equipe=bool(p.get("equipe", True)),
            role_equipe=p.get("role_equipe", "platform_operator"),
        )
        if p
        else Principal(
            utilisateur_id=travail.demande_par,
            email="worker",
            nom="worker",
            org_id=travail.org_id,
            role="platform_operator",
            equipe=True,
            role_equipe="platform_operator",
        )
    )
    return Contexte(
        request=faux_request,
        session=session,
        reglages=reglages(),
        correlation_id=travail.correlation_id or "-",
        principal=principal,
    )


async def executer_depuis_worker(travail_id: str, depuis: int | None) -> str:
    async with fabrique()() as session:
        travail = await session.get(Travail, travail_id)
        if travail is None:
            return "introuvable"
        ctx = contexte_travail(session, travail)
        if depuis is None:
            taches = travail.taches or []
            depuis = next((i for i, t in enumerate(taches) if t["statut"] == "failed"), 0)
            for t in taches[depuis:]:
                t["statut"] = "pending"
            travail.taches = list(taches)
        await moteur._executer(ctx, travail, depuis=depuis)
        await session.commit()
        return travail.statut
