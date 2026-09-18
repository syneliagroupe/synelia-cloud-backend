"""Tests des services managés : catalogue hébergé et cycle de vie des souscriptions."""

from __future__ import annotations


async def _id_service(client_org, nom: str) -> str:
    r = await client_org.get("/v1/services")
    assert r.status_code == 200
    for s in r.json()["donnees"]:
        if s["nom"] == nom:
            return s["id"]
    raise AssertionError(f"service {nom} introuvable")


async def test_catalogue(client_org):
    r = await client_org.get("/v1/catalogue/services")
    assert r.status_code == 200, r.text
    fiches = r.json()["donnees"]
    slugs = [f["slug"] for f in fiches]
    assert len(slugs) == 13 and "drive-pro" in slugs

    r = await client_org.get("/v1/catalogue/services/drive-pro")
    assert r.status_code == 200 and r.json()["solutionOSS"] == "Nextcloud"

    r = await client_org.get("/v1/catalogue/services/inconnu")
    assert r.status_code == 404

    r = await client_org.get("/v1/catalogue/services/drive-pro/configuration")
    assert r.status_code == 200 and r.json()["slug"] == "drive-pro" and r.json()["sections"]

    r = await client_org.get("/v1/catalogue/services/inconnu/configuration")
    assert r.status_code == 404

    r = await client_org.get("/v1/catalogue/services-partages")
    assert r.status_code == 200 and len(r.json()) == 13

    r = await client_org.get("/v1/catalogue/contrat-integration")
    assert r.status_code == 200 and r.json()["capacites"]


async def test_cycle_souscription(client_org):
    corps = {
        "catalogSlug": "drive-pro",
        "nom": "Drive Pro Test",
        "mode": "mutualise",
        "site": "ABJ",
        "palier": "starter",
        "sieges": 5,
        "sso": True,
    }
    r = await client_org.post("/v1/services", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "service_manage.subscribe" and travail["statut"] == "done"

    sid = await _id_service(client_org, "Drive Pro Test")

    r = await client_org.get(f"/v1/services/{sid}")
    assert r.status_code == 200 and r.json()["id"] == sid and r.json()["statut"] == "operationnel"
    assert r.json()["urlNative"].startswith("https://drive-pro-")

    r = await client_org.patch(f"/v1/services/{sid}", json={"nom": "Renommé"})
    assert r.status_code == 200 and r.json()["nom"] == "Renommé"

    r = await client_org.put(f"/v1/services/{sid}/configuration", json={"valeurs": {"inconnu": 1}})
    assert r.status_code == 422

    r = await client_org.put(
        f"/v1/services/{sid}/configuration", json={"valeurs": {"motDePasseObligatoire": True}}
    )
    assert r.status_code == 200 and r.json()["configuration"]["slug"] == "drive-pro"

    r = await client_org.get(f"/v1/services/{sid}/configuration")
    assert r.status_code == 200

    r = await client_org.post(f"/v1/services/{sid}/export", json={"format": "zip"})
    assert r.status_code == 202

    r = await client_org.get(f"/v1/services/{sid}/exports")
    assert r.status_code == 200 and len(r.json()) >= 1

    r = await client_org.get(f"/v1/services/{sid}/metriques")
    assert r.status_code == 200 and r.json()["series"][0]["points"] == []

    r = await client_org.post(f"/v1/services/{sid}/mise-a-jour", json={})
    assert r.status_code == 202, r.text

    r = await client_org.post(f"/v1/services/{sid}/mise-a-jour", json={})
    assert r.status_code == 409

    r = await client_org.get(f"/v1/services/{sid}/versions")
    assert r.status_code == 200 and len(r.json()) == 2

    r = await client_org.put(f"/v1/services/{sid}/sso", json={"actif": False, "groupMappings": []})
    assert r.status_code == 200 and r.json()["sso"]["actif"] is False

    r = await client_org.post(f"/v1/services/{sid}/ouverture")
    assert r.status_code == 201 and r.json()["url"]

    r = await client_org.post(
        f"/v1/services/{sid}/sieges", json={"userId": "user-1", "quotaTotal": 100}
    )
    assert r.status_code == 201, r.text
    siege_id = r.json()["id"]

    r = await client_org.post(f"/v1/services/{sid}/sieges", json={"userId": "user-1"})
    assert r.status_code == 409

    r = await client_org.get(f"/v1/services/{sid}/sieges")
    assert r.status_code == 200 and r.json()["pagination"]["total"] >= 1

    r = await client_org.patch(f"/v1/services/{sid}/sieges/{siege_id}", json={"statut": "suspendu"})
    assert r.status_code == 200 and r.json()["statut"] == "suspendu"

    r = await client_org.delete(f"/v1/services/{sid}/sieges/{siege_id}")
    assert r.status_code == 204

    r = await client_org.post(
        f"/v1/services/{sid}/versions/rollback", json={"confirmation": "Renommé"}
    )
    assert r.status_code == 202, r.text

    r = await client_org.delete(f"/v1/services/{sid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/services/{sid}", params={"confirmation": "Renommé"})
    assert r.status_code == 202


async def test_siege_quota(client_org):
    corps = {
        "catalogSlug": "drive-pro",
        "nom": "Quota",
        "mode": "mutualise",
        "site": "ABJ",
        "palier": "starter",
        "sieges": 10,
    }
    r = await client_org.post("/v1/services", json=corps)
    assert r.status_code == 202, r.text
    sid = await _id_service(client_org, "Quota")

    r = await client_org.post(f"/v1/services/{sid}/sieges", json={"userId": "x"})
    assert r.status_code == 402
