"""Registrar : disponibilité, commande, transfert, renouvellement, code-auth."""

from __future__ import annotations

import os
from typing import Any

from synelia_kernel.ids import jeton_opaque, nouvel_id

ENV_URL = "SYNELIA_REGISTRAR_URL"

OCCUPES = {"google.com", "synelia.ci"}


class RegistrarSimule:
    def verifier(self, nom: str) -> bool:
        return nom.lower() not in OCCUPES

    def commander(self, nom: str, duree_annees: int) -> dict[str, Any]:
        return {
            "id": f"dom-{nouvel_id()[:8]}",
            "code_auth": jeton_opaque(12),
            "expiration": duree_annees,
        }

    def transferer(self, nom: str, code_auth: str) -> dict[str, Any]:
        return {"id": f"dom-{nouvel_id()[:8]}", "code_auth": jeton_opaque(12)}

    def renouveler(self, nom: str, duree_annees: int) -> None:
        return None

    def code_auth(self, nom: str) -> dict[str, Any]:
        return {"code": jeton_opaque(12), "expire_heures": 24}


class RegistrarOpenStack(RegistrarSimule):
    """Registrar partenaire via son API HTTP (variable d'environnement d'URL) —
    aucun partenaire n'est câblé pour l'instant : cette classe reste le simulé
    sous un autre nom, et `choisir_registrar()` ne la renvoie jamais tant que
    `SYNELIA_REGISTRAR_URL` n'est pas posée (jamais de faux « réel »)."""


def choisir_registrar() -> RegistrarSimule:
    """Le réel seulement quand un partenaire est configuré — partout ailleurs
    (lab y compris, aucune URL partenaire posée) le simulé, explicitement."""
    if os.environ.get(ENV_URL):
        return RegistrarOpenStack()
    return RegistrarSimule()
