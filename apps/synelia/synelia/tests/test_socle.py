"""Le socle : santé, erreurs au format du contrat, connexion, RBAC, travaux."""

import re
from pathlib import Path

# Phase 11 (« IA & Agents », `docs/PLAN-DIRECTEUR.md`) : ces 4 permissions sont déclarées dans
# `rbac.json` mais hors contrat OpenAPI pour l'instant — aucune route ne peut donc les
# appliquer. Toute permission *hors* de cette liste doit être référencée par un vrai
# `exige(...)`/`exige_admin(...)` : c'est exactement la classe de bug qu'a été `ia.endpoint.deploy`
# (permission qui gardait un bouton visible côté frontend sans aucune application réelle côté
# backend), avant sa suppression.
PERMISSIONS_PHASE_11_NON_APPLIQUEES = {
    "ia.agent.publish",
    "ia.budget.update",
    "ia.routing.update",
    "ia.tool.register",
}


def _permissions_appliquees() -> set[str]:
    racine = Path(__file__).resolve().parents[1] / "modules"
    motif = re.compile(r"""exige(?:_admin)?\(\s*["']([a-zA-Z0-9_.]+)["']""")
    trouvees: set[str] = set()
    for fichier in racine.rglob("*.py"):
        trouvees |= set(motif.findall(fichier.read_text(encoding="utf-8")))
    return trouvees


async def test_matrice_rbac_sans_permission_orpheline(client):
    """Toute permission déclarée dans `rbac.json` doit être appliquée par au moins un vrai
    `exige(...)`/`exige_admin(...)` dans le code des routeurs — sauf les 4 placeholders de la
    phase 11, listés explicitement. Sans ce garde-fou, une permission peut rester purement
    déclarative (gardant un bouton visible côté frontend, sans aucune application réelle côté
    backend) sans que rien ne le signale — vécu en direct avec `ia.endpoint.deploy`."""
    r = await client.get("/v1/rbac/matrice")
    declarees = {action["id"] for action in r.json()}
    appliquees = _permissions_appliquees()
    orphelines = declarees - appliquees - PERMISSIONS_PHASE_11_NON_APPLIQUEES
    assert not orphelines, f"Permissions déclarées mais jamais appliquées : {orphelines}"


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["statut"] == "ok"


async def test_erreur_404_au_format_contrat(client):
    r = await client.get("/v1/chemin-inconnu")
    assert r.status_code == 404
    corps = r.json()
    assert corps["erreur"]["code"] == "introuvable" and corps["erreur"]["correlationId"]


async def test_non_authentifie(client):
    r = await client.get("/v1/moi", headers={"Authorization": ""})
    assert r.status_code == 401 and r.json()["erreur"]["code"] == "non_authentifie"


async def test_connexion_et_moi(client):
    assert client.jeton
    r = await client.get("/v1/moi")
    assert r.status_code == 200
    assert r.json()["utilisateur"]["email"] == "admin@synelia.cloud"


async def test_rafraichissement_rotatif(client):
    r = await client.post(
        "/v1/auth/connexion", json={"email": "admin@synelia.cloud", "motDePasse": "Synelia!2026"}
    )
    refresh = r.json()["refreshToken"]
    r2 = await client.post("/v1/auth/rafraichir", json={"refreshToken": refresh})
    assert r2.status_code == 200 and r2.json()["accessToken"]
    r3 = await client.post("/v1/auth/rafraichir", json={"refreshToken": refresh})
    assert r3.status_code == 401  # réutilisation détectée


async def test_validation_422_avec_champs(client):
    r = await client.post("/v1/auth/connexion", json={"email": "pas-un-email"})
    assert r.status_code == 422
    assert "champs" in r.json()


async def test_matrice_rbac(client):
    r = await client.get("/v1/rbac/matrice")
    # 37, pas 38 : `ia.endpoint.deploy` (inférence dédiée) a été supprimé côté frontend — sans
    # GPU sur cette plateforme, ce n'était plus une permission qui pouvait un jour devenir réelle.
    # `rbac.json` était resté périmé côté backend jusqu'à ce que `tools/contrat_sync.py` soit
    # rejoué ; voir la décision « Inférence dédiée » dans le CLAUDE.md du frontend.
    assert r.status_code == 200 and len(r.json()) == 37
