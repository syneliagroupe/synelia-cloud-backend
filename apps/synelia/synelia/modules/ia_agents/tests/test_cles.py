import httpx
import pytest
import respx

pytestmark = pytest.mark.anyio

LITELLM_URL = "http://litellm:4000"
AGENT = "/v1/ia/agents/agent-demo-support/invoquer"


def _mock_llm(jetons_entree: int = 10, jetons_sortie: int = 5) -> None:
    respx.post(f"{LITELLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": jetons_entree,
                    "completion_tokens": jetons_sortie,
                    "cost": 0.00001,
                },
            },
        )
    )


async def test_creer_cle_ia_renvoie_le_secret_une_fois(client):
    r = await client.post("/v1/ia/cles", json={"nom": "Clé prod", "espaceId": "espace-demo-abj"})
    assert r.status_code == 201, r.text
    corps = r.json()
    assert corps["secret"].startswith(corps["cle"]["prefixe"])
    assert corps["cle"]["quotaJetonsMois"] == 1_000_000
    assert corps["cle"]["statut"] == "active"
    assert corps["cle"]["modelesAutorises"] == ["tous"]

    # relue ensuite, la fiche ne renvoie plus le secret.
    r = await client.get(f"/v1/ia/cles/{corps['cle']['id']}")
    assert r.status_code == 200
    assert "secret" not in r.json()


async def test_cle_ia_secret_invalide_401(client):
    r = await client.post(AGENT, json={"message": "x"}, headers={"X-Cle-IA": "nawak.inconnu"})
    assert r.status_code == 401
    assert r.json()["erreur"]["code"] == "non_authentifie"


@respx.mock
async def test_cle_ia_modele_non_autorise(client):
    r = await client.post(
        "/v1/ia/cles",
        json={
            "nom": "Clé restreinte",
            "espaceId": "espace-demo-abj",
            "modelesAutorises": ["mistralai/mistral-small-3.2-24b-instruct"],
        },
    )
    secret = r.json()["secret"]
    _mock_llm()
    r = await client.post(AGENT, json={"message": "Bonjour"}, headers={"X-Cle-IA": secret})
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "ia_modele_non_autorise"


@respx.mock
async def test_cle_ia_residence_max_bloque_modele_externe(client):
    r = await client.post(
        "/v1/ia/cles",
        json={"nom": "Clé souveraine", "espaceId": "espace-demo-abj", "residenceMax": "reglementee"},
    )
    secret = r.json()["secret"]
    _mock_llm()
    r = await client.post(AGENT, json={"message": "Bonjour"}, headers={"X-Cle-IA": secret})
    assert r.status_code == 403
    assert r.json()["erreur"]["code"] == "ia_residence_depassee"


@respx.mock
async def test_cle_ia_quota_jetons_bloque_puis_credite(client):
    r = await client.post(
        "/v1/ia/cles",
        json={"nom": "Clé quota serré", "espaceId": "espace-demo-abj", "quotaJetonsMois": 10},
    )
    secret = r.json()["secret"]
    _mock_llm(jetons_entree=8, jetons_sortie=4)  # 12 jetons consommés par appel

    r1 = await client.post(AGENT, json={"message": "1"}, headers={"X-Cle-IA": secret})
    assert r1.status_code == 200, r1.text  # premier appel : le compteur part de 0

    r2 = await client.post(AGENT, json={"message": "2"}, headers={"X-Cle-IA": secret})
    assert r2.status_code == 402
    assert r2.json()["erreur"]["code"] == "quota_depasse"


@respx.mock
async def test_cle_ia_depassement_alerter_laisse_passer(client):
    r = await client.post(
        "/v1/ia/cles",
        json={
            "nom": "Clé alerte",
            "espaceId": "espace-demo-abj",
            "quotaJetonsMois": 1,
            "auDepassement": "alerter",
        },
    )
    secret = r.json()["secret"]
    _mock_llm(jetons_entree=8, jetons_sortie=4)

    r1 = await client.post(AGENT, json={"message": "1"}, headers={"X-Cle-IA": secret})
    assert r1.status_code == 200
    r2 = await client.post(AGENT, json={"message": "2"}, headers={"X-Cle-IA": secret})
    assert r2.status_code == 200, "alerter : dépasser le quota ne bloque pas l'appel"


@respx.mock
async def test_cle_ia_debit_max_par_minute(client):
    r = await client.post(
        "/v1/ia/cles", json={"nom": "Clé débit", "espaceId": "espace-demo-abj", "debitMaxParMinute": 1}
    )
    secret = r.json()["secret"]
    _mock_llm()

    r1 = await client.post(AGENT, json={"message": "1"}, headers={"X-Cle-IA": secret})
    assert r1.status_code == 200
    r2 = await client.post(AGENT, json={"message": "2"}, headers={"X-Cle-IA": secret})
    assert r2.status_code == 429


async def test_revoquer_cle_ia(client):
    r = await client.post("/v1/ia/cles", json={"nom": "Clé jetable", "espaceId": "espace-demo-abj"})
    cle_id = r.json()["cle"]["id"]
    secret = r.json()["secret"]

    r = await client.delete(f"/v1/ia/cles/{cle_id}")
    assert r.status_code == 422, "sans confirmation, la révocation doit être refusée"
    r = await client.delete(f"/v1/ia/cles/{cle_id}", params={"confirmation": "Clé jetable"})
    assert r.status_code == 204

    r = await client.post(AGENT, json={"message": "x"}, headers={"X-Cle-IA": secret})
    assert r.status_code == 401


@respx.mock
async def test_rotationner_cle_ia_invalide_l_ancien_secret(client):
    r = await client.post("/v1/ia/cles", json={"nom": "Clé tournante", "espaceId": "espace-demo-abj"})
    cle_id = r.json()["cle"]["id"]
    ancien_prefixe = r.json()["cle"]["prefixe"]
    ancien_secret = r.json()["secret"]
    _mock_llm()

    r = await client.post(f"/v1/ia/cles/{cle_id}/rotation", json={})
    assert r.status_code == 200, r.text
    corps = r.json()
    nouveau_secret = corps["secret"]
    assert corps["cle"]["prefixe"] == ancien_prefixe, "le préfixe visible ne change pas à la rotation"
    assert nouveau_secret != ancien_secret

    # l'ancien secret ne fonctionne plus.
    r = await client.post(AGENT, json={"message": "x"}, headers={"X-Cle-IA": ancien_secret})
    assert r.status_code == 401

    # le nouveau fonctionne.
    r = await client.post(AGENT, json={"message": "x"}, headers={"X-Cle-IA": nouveau_secret})
    assert r.status_code == 200, r.text


async def test_rotationner_cle_ia_revoquee_409(client):
    r = await client.post("/v1/ia/cles", json={"nom": "Clé morte", "espaceId": "espace-demo-abj"})
    cle_id = r.json()["cle"]["id"]
    await client.delete(f"/v1/ia/cles/{cle_id}", params={"confirmation": "Clé morte"})

    r = await client.post(f"/v1/ia/cles/{cle_id}/rotation", json={})
    assert r.status_code == 409
    assert r.json()["erreur"]["code"] == "cle_ia_non_active"


@respx.mock
async def test_cle_ia_cout_fractionnaire_finit_par_epuiser_le_budget(client):
    """Un modèle bon marché coûte une fraction de FCFA par appel (`round(cout_fcfa, 4)` dans
    `service._completer`) : arrondir chaque appel à l'entier avant de créditer `budgetConsomme`
    laisserait un plafond `bloquer` de 1 FCFA inatteignable indéfiniment. Le reste fractionnaire
    doit se reporter d'appel en appel jusqu'à franchir l'entier."""
    r = await client.post(
        "/v1/ia/cles",
        json={
            "nom": "Clé modèle bon marché",
            "espaceId": "espace-demo-abj",
            "budgetMensuel": 1,
            "debitMaxParMinute": 100_000,
            "quotaJetonsMois": 10_000_000,
        },
    )
    secret = r.json()["secret"]
    # cost 0.00001 USD * TAUX_USD_XOF (610) = 0,0061 FCFA/appel, arrondit à 0 si non reporté.
    _mock_llm()

    dernier = None
    for _ in range(200):
        dernier = await client.post(AGENT, json={"message": "x"}, headers={"X-Cle-IA": secret})
        if dernier.status_code == 402:
            break
    assert dernier is not None and dernier.status_code == 402, (
        "le plafond doit finir par se déclencher malgré un coût unitaire sous le FCFA"
    )
