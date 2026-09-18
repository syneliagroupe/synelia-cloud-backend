"""Tests du module Observabilité : alertes, événements, journaux, métriques."""


async def test_crud_regle_alerte(client_org):
    corps = {
        "cible": "vm-app-01",
        "metrique": "cpu",
        "seuil": "> 85%",
        "canaux": ["email", "sms"],
        "plage": "24/7",
    }
    r = await client_org.post("/v1/observabilite/alertes", json=corps)
    assert r.status_code == 201, r.text
    regle = r.json()
    assert regle["cible"] == "vm-app-01" and regle["actif"] is True
    alerte_id = regle["id"]

    r = await client_org.get("/v1/observabilite/alertes")
    assert r.status_code == 200 and r.json()["pagination"]["total"] == 1

    r = await client_org.get(f"/v1/observabilite/alertes/{alerte_id}")
    assert r.status_code == 200 and r.json()["metrique"] == "cpu"

    r = await client_org.patch(
        f"/v1/observabilite/alertes/{alerte_id}", json={**corps, "actif": False}
    )
    assert r.status_code == 200 and r.json()["actif"] is False

    r = await client_org.delete(
        f"/v1/observabilite/alertes/{alerte_id}", params={"confirmation": "vm-app-01"}
    )
    assert r.status_code == 204

    r = await client_org.get("/v1/observabilite/alertes")
    assert r.json()["pagination"]["total"] == 0


async def test_tester_regle_alerte(client_org):
    r = await client_org.post(
        "/v1/observabilite/alertes",
        json={"cible": "vm-app-01", "metrique": "ram", "seuil": "> 90%", "canaux": ["email"]},
    )
    alerte_id = r.json()["id"]
    r = await client_org.post(f"/v1/observabilite/alertes/{alerte_id}/test")
    assert r.status_code == 200 and r.json()["envoye"] is True


async def test_evenements_supervision(client_org):
    r = await client_org.get("/v1/observabilite/evenements")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert "donnees" in corps and "pagination" in corps
    assert isinstance(corps["donnees"], list)


async def test_journaux(client_org):
    r = await client_org.get("/v1/observabilite/journaux")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert isinstance(corps["lignes"], list)
    assert "lienVictoriaLogs" in corps


async def test_metriques_zero_remplies(client_org):
    r = await client_org.get("/v1/observabilite/metriques", params={"fenetre": "24h"})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["series"] and corps["tuiles"]
    serie = corps["series"][0]
    assert serie["fenetre"] == "24h" and len(serie["points"]) > 0
    assert all(p["valeur"] == 0 for p in serie["points"])
