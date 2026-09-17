import httpx
import pytest
import respx

pytestmark = pytest.mark.anyio

LITELLM_URL = "http://litellm:4000"


async def test_liste_modeles_ia(client):
    r = await client.get("/v1/ia/modeles", params={"parPage": 50})
    assert r.status_code == 200
    corps = r.json()
    par_slug = {d["slug"]: d for d in corps["donnees"]}
    assert par_slug["meta-llama/llama-3.3-70b-instruct"]["invocable"] is True
    assert par_slug["synelia/bge-m3"]["invocable"] is False


async def test_obtenir_modele_ia(client):
    r = await client.get("/v1/ia/modeles/m-llama-70b")
    assert r.status_code == 200
    assert r.json()["slug"] == "meta-llama/llama-3.3-70b-instruct"


async def test_obtenir_modele_ia_inconnu_404(client):
    r = await client.get("/v1/ia/modeles/inexistant")
    assert r.status_code == 404


async def test_creer_agent_ia(client):
    r = await client.post(
        "/v1/ia/agents",
        json={
            "nom": "Assistant recette",
            "consigne": "Réponds brièvement en français.",
            "modele": "meta-llama/llama-3.3-70b-instruct",
        },
    )
    assert r.status_code == 201, r.text
    corps = r.json()
    assert corps["statut"] == "brouillon"
    assert corps["temperature"] == 0.7
    assert corps["topP"] == 1
    assert corps["jetonsMax"] == 1024


async def test_creer_agent_ia_modele_inconnu(client):
    r = await client.post(
        "/v1/ia/agents",
        json={"nom": "Mauvais agent", "consigne": "x", "modele": "nawak/inconnu"},
    )
    assert r.status_code == 422
    assert r.json()["champs"]["modele"]


async def test_lister_puis_modifier_puis_supprimer_agent_ia(client):
    r = await client.post(
        "/v1/ia/agents",
        json={"nom": "Agent jetable", "consigne": "x", "modele": "meta-llama/llama-3.3-70b-instruct"},
    )
    agent_id = r.json()["id"]

    r = await client.get("/v1/ia/agents")
    assert r.status_code == 200
    assert any(d["id"] == agent_id for d in r.json()["donnees"])

    r = await client.patch(f"/v1/ia/agents/{agent_id}", json={"nom": "Agent renommé"})
    assert r.status_code == 200
    assert r.json()["nom"] == "Agent renommé"

    r = await client.delete(f"/v1/ia/agents/{agent_id}")
    assert r.status_code == 422, "sans confirmation, la suppression doit être refusée"

    r = await client.delete(f"/v1/ia/agents/{agent_id}", params={"confirmation": "Agent renommé"})
    assert r.status_code == 204

    r = await client.get(f"/v1/ia/agents/{agent_id}")
    assert r.status_code == 404


@respx.mock
async def test_invoquer_agent_reel(client):
    respx.post(f"{LITELLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Bonjour, comment puis-je vous aider ?"}}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 9, "cost": 0.00001168},
            },
        )
    )
    r = await client.post(
        "/v1/ia/agents/agent-demo-support/invoquer", json={"message": "Bonjour !"}
    )
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["reponse"] == "Bonjour, comment puis-je vous aider ?"
    assert corps["jetonsEntree"] == 42
    assert corps["jetonsSortie"] == 9
    assert corps["coutFcfa"] > 0
    assert corps["latenceMs"] >= 0


async def test_invoquer_agent_modele_non_invocable(client):
    r = await client.post(
        "/v1/ia/agents",
        json={
            "nom": "Agent embedding",
            "consigne": "x",
            "modele": "synelia/bge-m3",
        },
    )
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    r = await client.post(f"/v1/ia/agents/{agent_id}/invoquer", json={"message": "Bonjour"})
    assert r.status_code == 422
    assert r.json()["erreur"]["code"] == "non_porte"


@respx.mock
async def test_invoquer_agent_litellm_indisponible(client):
    respx.post(f"{LITELLM_URL}/chat/completions").mock(
        side_effect=httpx.ConnectError("connexion refusée")
    )
    r = await client.post(
        "/v1/ia/agents/agent-demo-support/invoquer", json={"message": "Bonjour !"}
    )
    assert r.status_code == 424
    assert r.json()["erreur"]["code"] == "amont_indisponible"


@respx.mock
async def test_invoquer_agent_contenu_null_raisonnement(client):
    """Reproduction en direct (2026-09-07) : `deepseek/deepseek-v4-flash` (modèle de
    raisonnement) peut consommer tout `max_tokens` en jetons de raisonnement internes et
    atteindre `finish_reason: "length"` sans jamais émettre de `content` — vérifié contre
    l'API OpenRouter réelle avec un agent à `jetonsMax` bas. Doit rester un 424 franc, pas un
    500 de validation de réponse (`reponse` exigé `str`)."""
    respx.post(f"{LITELLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning_content": "..."},
                    }
                ],
                "usage": {"prompt_tokens": 32, "completion_tokens": 128},
            },
        )
    )
    r = await client.post(
        "/v1/ia/agents/agent-demo-support/invoquer", json={"message": "Bonjour !"}
    )
    assert r.status_code == 424
    assert r.json()["erreur"]["code"] == "amont_indisponible"
