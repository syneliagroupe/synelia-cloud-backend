"""Hooks Schemathesis pour la suite « contrat » (voir tools/contrat_schemathesis.sh).

- Authentification : un jeton est obtenu par `POST /v1/auth/connexion` (compte admin amorcé) et posé en
  `Authorization: Bearer …` sur chaque requête ; il est ré-obtenu automatiquement sur 401/403.
- `X-Organisation-Id` porte l'organisation active de la session (celle du jeton), pour que les appels
  ciblent l'organisation de démo et non un identifiant aléatoire généré par le fuzzer.

Variables : SYNELIA_SCHEMATHESIS_URL (défaut http://127.0.0.1:4020/v1), SYNELIA_SCHEMATHESIS_EMAIL,
SYNELIA_SCHEMATHESIS_MDP (défaut : le compte d'amorçage admin@synelia.cloud / Synelia!2026).
"""

from __future__ import annotations

import os

import requests
import schemathesis

URL = os.environ.get("SYNELIA_SCHEMATHESIS_URL", "http://127.0.0.1:4020/v1").rstrip("/")
EMAIL = os.environ.get("SYNELIA_SCHEMATHESIS_EMAIL", "admin@synelia.cloud")
MDP = os.environ.get("SYNELIA_SCHEMATHESIS_MDP", "Synelia!2026")

# Opérations publiques (préfixe de chemin) : on n'y pose pas de jeton, le contrat ne le réclame pas.
CHEMINS_PUBLICS = ("/public/", "/auth/connexion", "/auth/mfa", "/auth/rafraichir", "/auth/sso")


class Identite:
    """Jeton d'accès et organisation active (pas de dataclass : le module est chargé hors sys.modules)."""

    __slots__ = ("jeton", "org_id")

    def __init__(self, jeton: str, org_id: str | None) -> None:
        self.jeton = jeton
        self.org_id = org_id


def se_connecter() -> Identite:
    r = requests.post(
        f"{URL}/auth/connexion",
        json={"email": EMAIL, "motDePasse": MDP},
        timeout=30,
    )
    r.raise_for_status()
    corps = r.json()
    return Identite(jeton=corps["accessToken"], org_id=corps.get("organisationActive"))


@schemathesis.auth(refresh_interval=600, retry_on=[401])
class AuthSynelia:
    """Jeton de session : obtenu une fois, rafraîchi périodiquement ou sur 401."""

    def get(self, case: schemathesis.Case, ctx: schemathesis.AuthContext) -> Identite:
        return se_connecter()

    def set(self, case: schemathesis.Case, data: Identite, ctx: schemathesis.AuthContext) -> None:
        if case.headers is None:
            case.headers = {}
        chemin = case.operation.path
        if chemin.startswith(CHEMINS_PUBLICS):
            case.headers.pop("Authorization", None)
            case.headers.pop("X-Api-Key", None)
            return
        case.headers["Authorization"] = f"Bearer {data.jeton}"
        case.headers.pop("X-Api-Key", None)
        if data.org_id:
            case.headers["X-Organisation-Id"] = data.org_id
