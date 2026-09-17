"""Tests du module PRA : plans, bascules, retours, exercices."""


def _pra() -> dict:
    return {
        "nom": "pra-prod",
        "siteSource": "ABJ",
        "siteRepli": "GBM",
        "rpoCibleMin": 30,
        "rtoCibleMin": 120,
        "groupes": [
            {"ordre": 1, "nom": "Bases", "ressources": ["vm-bd-01"], "dependances": []},
            {"ordre": 2, "nom": "Front", "ressources": ["vm-web-01"], "dependances": ["Bases"]},
        ],
        "replication": {"mode": "planifie", "retardS": 300},
    }


async def test_cycle_plan_pra(client):
    r = await client.post("/v1/pra", json=_pra())
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["nom"] == "pra-prod" and plan["statut"] == "jamais_teste"
    pra_id = plan["id"]

    r = await client.get(f"/v1/pra/{pra_id}")
    assert r.status_code == 200 and r.json()["exercices"] == []

    r = await client.patch(f"/v1/pra/{pra_id}", json={**_pra(), "nom": "pra-prod-v2"})
    assert r.status_code == 200 and r.json()["nom"] == "pra-prod-v2"

    r = await client.delete(f"/v1/pra/{pra_id}", params={"confirmation": "pra-prod-v2"})
    assert r.status_code == 204


async def test_modifier_plan_pra_sans_dependances(client):
    """Régression : `dependances` est optionnel sur `GroupePra` (le contrat de création), mais
    requis sur le `Groupe` stocké — un client qui l'omet sur un PATCH (comme le fait le
    formulaire d'édition de l'app mobile) faisait planter la revalidation de `Depot.modifier`
    en 500 nu avant le correctif de `modifier_plan_pra`."""
    r = await client.post("/v1/pra", json=_pra())
    pra_id = r.json()["id"]

    r = await client.patch(
        f"/v1/pra/{pra_id}",
        json={
            **_pra(),
            "rtoCibleMin": 90,
            "groupes": [{"ordre": 1, "nom": "Bases", "ressources": ["vm-bd-01", "vm-bd-02"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["rtoCibleMin"] == 90
    assert r.json()["groupes"] == [
        {"ordre": 1, "nom": "Bases", "ressources": ["vm-bd-01", "vm-bd-02"], "dependances": []}
    ]


async def test_replication_continue_refusee(client):
    r = await client.post(
        "/v1/pra", json={**_pra(), "replication": {"mode": "continu", "retardS": 0}}
    )
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "non_porte"
    assert "réplication continue" in r.json()["erreur"]["message"]


async def test_bascule_test_et_exercice(client):
    r = await client.post("/v1/pra", json=_pra())
    pra_id = r.json()["id"]

    r = await client.post(f"/v1/pra/{pra_id}/bascule", json={"type": "test"})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "dr.failover.test" and r.json()["statut"] == "done"

    r = await client.get(f"/v1/pra/{pra_id}/exercices")
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["type"] == "test" and r.json()[0]["succes"] is True


async def test_bascule_reelle_avec_confirmation(client):
    r = await client.post("/v1/pra", json=_pra())
    pra_id = r.json()["id"]

    r = await client.post(f"/v1/pra/{pra_id}/bascule", json={"type": "reel"})
    assert r.status_code == 422

    r = await client.post(
        f"/v1/pra/{pra_id}/bascule", json={"type": "reel", "confirmation": "pra-prod"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "dr.failover.real"

    r = await client.get(f"/v1/pra/{pra_id}/exercices")
    assert len(r.json()) == 1 and r.json()[0]["type"] == "reel"


async def test_retour_site_source(client):
    r = await client.post("/v1/pra", json=_pra())
    pra_id = r.json()["id"]

    r = await client.post(f"/v1/pra/{pra_id}/retour", json={"confirmation": "pra-prod"})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "dr.failover.retour"
