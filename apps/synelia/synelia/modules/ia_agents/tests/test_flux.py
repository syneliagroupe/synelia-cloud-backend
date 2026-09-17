"""Exécution réelle des flux d'orchestration : branchement, boucle, pause humaine."""

from __future__ import annotations

import httpx
import pytest
import respx

pytestmark = pytest.mark.anyio

LITELLM_URL = "http://litellm:4000"


def _etape(id_: str, type_: str, **kw) -> dict:
    return {
        "id": id_,
        "type": type_,
        "nom": kw.pop("nom", id_),
        "source": kw.pop("source", "Plateforme"),
        "detail": kw.pop("detail", ""),
        "executions24h": 0,
        "latenceMs": 0,
        "coutPourMille": 0,
        "tauxErreurPct": 0,
        **kw,
    }


async def _creer_agent(client) -> str:
    r = await client.post(
        "/v1/ia/agents",
        json={
            "nom": "Agent de test flux",
            "consigne": "Réponds uniquement par le mot « positif » ou « negatif ».",
            "modele": "meta-llama/llama-3.3-70b-instruct",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@respx.mock
async def test_flux_branchement_reel(client):
    """declencheur → agent (réel via LiteLLM mocké) → routeur à deux branches → reponse."""
    agent_id = await _creer_agent(client)
    respx.post(f"{LITELLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "positif"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.000002},
            },
        )
    )

    flux = {
        "nom": "Flux de test — branchement",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "etapes": [
            _etape("e-decl", "declencheur"),
            _etape("e-agent", "agent", agentId=agent_id),
            _etape(
                "e-route",
                "routeur",
                modeRoutage="premiere",
                branches=[
                    {
                        "id": "b-positif",
                        "nom": "Positif",
                        "condition": "positif",
                        "partPct": 50,
                        "etapes": [
                            _etape(
                                "e-rep-positif",
                                "reponse",
                                # La clé de gabarit contient un tiret (id d'étape en kebab-case,
                                # comme dans les données de démo) : régression réelle trouvée en
                                # vérification E2E, le premier gabarit n'acceptait pas le tiret.
                                detail="Merci, retour classé : [{{sortie_e-agent}}]",
                            )
                        ],
                    },
                    {
                        "id": "b-repli",
                        "nom": "Repli",
                        "condition": "",
                        "partPct": 50,
                        "parDefaut": True,
                        "etapes": [_etape("e-rep-repli", "reponse", detail="Repli.")],
                    },
                ],
            ),
        ],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    assert r.status_code == 201, r.text
    flux_id = r.json()["id"]

    r = await client.post(f"/v1/ia/flux/{flux_id}/executer", json={"entree": "J'ai un souci"})
    assert r.status_code == 202, r.text
    corps = r.json()
    assert corps["statut"] == "done", corps

    taches = {t["nom"]: t for t in corps["taches"]}
    assert taches["e-decl"]["statut"] == "ok"
    assert "positif" in taches["e-agent"]["message"]
    # La branche « Positif » doit avoir été retenue, pas « Repli ».
    assert "Positif" in taches["e-route"]["message"]
    # Le gabarit {{sortie_e-agent}} doit être substitué par la vraie sortie de l'agent — pas
    # laissé tel quel (régression : le tiret de l'id d'étape doit être accepté).
    assert "[positif]" in taches["e-route"]["message"]
    assert "{{" not in taches["e-route"]["message"]

    # Les stats réelles de l'étape agent doivent avoir bougé (plus des zéros du départ).
    r = await client.get(f"/v1/ia/flux/{flux_id}")
    agent_step = next(e for e in r.json()["etapes"] if e["id"] == "e-agent")
    assert agent_step["executions24h"] == 1
    assert agent_step["latenceMs"] >= 0


async def test_flux_boucle_sur_variable(client):
    """boucle : rejoue son corps pour chaque élément d'une variable liste."""
    flux = {
        "nom": "Flux de test — boucle",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "variables": [
            {"cle": "items", "portee": "environnement", "valeur": "[]", "description": "x"},
        ],
        "etapes": [
            _etape("e-decl", "declencheur"),
            _etape(
                "e-boucle",
                "boucle",
                surItems="items",
                maxIterations=5,
                corps=[_etape("e-code", "code", detail="str(variables.get('item')) + '!'")],
            ),
        ],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    assert r.status_code == 201, r.text
    flux_id = r.json()["id"]

    r = await client.post(
        f"/v1/ia/flux/{flux_id}/executer",
        json={"entree": "go", "variables": {"items": ["a", "b", "c"]}},
    )
    assert r.status_code == 202, r.text
    corps = r.json()
    assert corps["statut"] == "done", corps


async def test_flux_pause_humaine_puis_reprise(client):
    """humain : le travail doit réellement se mettre en pause, puis reprendre sur décision."""
    flux = {
        "nom": "Flux de test — validation humaine",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "etapes": [
            _etape("e-decl", "declencheur"),
            _etape("e-humain", "humain", detail="Confirmez-vous l'envoi ?"),
            _etape("e-rep", "reponse", detail="Décision reçue : {{derniereSortie}}."),
        ],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    assert r.status_code == 201, r.text
    flux_id = r.json()["id"]

    r = await client.post(f"/v1/ia/flux/{flux_id}/executer", json={"entree": "test"})
    assert r.status_code == 202, r.text
    corps = r.json()
    # Toujours `running` — ni `done` ni `failed` : une vraie pause, pas un passe-plat silencieux.
    assert corps["statut"] == "running", corps
    travail_id = corps["id"]
    tache_humain = next(t for t in corps["taches"] if t["nom"] == "e-humain")
    assert "attente" in tache_humain["message"].lower() or "validation" in tache_humain["message"].lower()

    # Tant qu'aucune décision n'est prise, le travail reste en l'état si on le relit.
    r = await client.get(f"/v1/travaux/{travail_id}")
    assert r.json()["statut"] == "running"

    r = await client.post(
        f"/v1/ia/flux/{flux_id}/executions/{travail_id}/reprendre",
        json={"decision": "approuve", "commentaire": "OK pour moi"},
    )
    assert r.status_code == 202, r.text
    corps = r.json()
    assert corps["statut"] == "done", corps
    tache_rep = next(t for t in corps["taches"] if t["nom"] == "e-rep")
    assert tache_rep["message"] == "Décision reçue : approuve."


async def test_flux_reprise_sans_attente_409(client):
    flux = {
        "nom": "Flux sans pause",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "etapes": [_etape("e-decl", "declencheur"), _etape("e-rep", "reponse", detail="ok")],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    flux_id = r.json()["id"]
    r = await client.post(f"/v1/ia/flux/{flux_id}/executer", json={"entree": "test"})
    travail_id = r.json()["id"]

    r = await client.post(
        f"/v1/ia/flux/{flux_id}/executions/{travail_id}/reprendre",
        json={"decision": "approuve"},
    )
    assert r.status_code == 409, r.text


async def test_suppression_flux_pendant_pause_termine_le_travail(client):
    """Bug réel (démo) : un travail en pause `humain` dont le flux est supprimé restait `running`
    pour toujours — plus aucun `POST .../reprendre` possible (flux 404), plus aucune autre voie
    ne le faisait terminer. La suppression doit désormais faire basculer ce travail vers un état
    terminal (`rolled_back`) avec un motif explicite, sans jamais toucher un flux qui existe
    encore et dont la pause est légitime."""
    flux = {
        "nom": "Flux de test — supprimé pendant la pause",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "etapes": [
            _etape("e-decl", "declencheur"),
            _etape("e-humain", "humain", detail="Confirmez-vous l'envoi ?"),
            _etape("e-rep", "reponse", detail="Décision reçue : {{derniereSortie}}."),
        ],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    assert r.status_code == 201, r.text
    flux_id = r.json()["id"]

    r = await client.post(f"/v1/ia/flux/{flux_id}/executer", json={"entree": "test"})
    assert r.status_code == 202, r.text
    corps = r.json()
    assert corps["statut"] == "running", corps
    travail_id = corps["id"]

    r = await client.delete(f"/v1/ia/flux/{flux_id}", params={"confirmation": flux["nom"]})
    assert r.status_code == 204, r.text

    # Le flux a bien disparu : reprendre l'exécution n'est plus possible.
    r = await client.get(f"/v1/ia/flux/{flux_id}")
    assert r.status_code == 404, r.text

    # ... mais le travail, lui, n'est plus un zombie `running` — il est terminé, motif explicite.
    r = await client.get(f"/v1/travaux/{travail_id}")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["statut"] == "rolled_back", corps
    assert "supprimé" in corps["erreur"]["message"].lower()


async def test_suppression_flux_sans_travail_en_cours_ne_touche_rien(client):
    """Cas nominal : un flux sans exécution en vol se supprime sans effet de bord sur `travaux`."""
    flux = {
        "nom": "Flux de test — sans exécution",
        "declencheur": {"type": "message", "libelle": "Message entrant", "detail": "test"},
        "etapes": [_etape("e-decl", "declencheur"), _etape("e-rep", "reponse", detail="ok")],
    }
    r = await client.post("/v1/ia/flux", json=flux)
    assert r.status_code == 201, r.text
    flux_id = r.json()["id"]

    r = await client.delete(f"/v1/ia/flux/{flux_id}", params={"confirmation": flux["nom"]})
    assert r.status_code == 204, r.text
