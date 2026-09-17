"""Drive (Web Cloud) : activation 202, sièges 201/409/402, ouverture 201.

Un domaine n'a qu'un seul serveur (VPS) : Drive s'installe sur l'hébergement déjà en
service pour ce domaine, il n'obtient plus sa propre VM — l'activer sans hébergement
préalable est désormais un vrai refus (409), pas une simulation muette."""


async def _creer_hebergement(client, nom: str) -> None:
    r = await client.post(
        "/v1/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": nom}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"


async def test_drive_refuse_sans_hebergement(client):
    r = await client.post(
        "/v1/web/drive", json={"domaine": "sans-vps.ci", "palier": "starter", "sieges": 1}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["statut"] == "failed"
    assert "hébergement" in travail["erreur"]["message"].lower()


async def test_cycle_drive(client):
    await _creer_hebergement(client, "cloud.ci")
    r = await client.post(
        "/v1/web/drive", json={"domaine": "cloud.ci", "palier": "pro", "sieges": 3}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.drive.activate" and travail["statut"] == "done"

    r = await client.get("/v1/web/drive")
    assert r.status_code == 200
    drives = r.json()["donnees"]
    assert len(drives) == 1 and drives[0]["actif"] is True
    did = drives[0]["id"]

    r = await client.patch(f"/v1/web/drive/{did}", json={"palier": "starter"})
    assert r.status_code == 200 and r.json()["palier"] == "starter"

    r = await client.post(f"/v1/web/drive/{did}/ouverture")
    assert r.status_code == 201, r.text
    ouv = r.json()
    assert ouv["url"].startswith("https://") and ouv["methode"] == "redirection"

    r = await client.post(f"/v1/web/drive/{did}/sieges", json={"userId": "u-1", "quotaTotal": 20})
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "actif"

    r = await client.post(f"/v1/web/drive/{did}/sieges", json={"userId": "u-1"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "siege_deja_attribue"

    r = await client.get(f"/v1/web/drive/{did}/sieges")
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client.get(f"/v1/web/drive/{did}")
    assert r.json()["sieges"]["attribues"] == 1


async def test_quota_drive(client):
    await _creer_hebergement(client, "quota.ci")
    r = await client.post(
        "/v1/web/drive", json={"domaine": "quota.ci", "palier": "starter", "sieges": 2}
    )
    assert r.status_code == 202
    did = next(
        d["id"]
        for d in (await client.get("/v1/web/drive")).json()["donnees"]
        if d["domaine"] == "quota.ci"
    )
    for i in range(2):
        rr = await client.post(f"/v1/web/drive/{did}/sieges", json={"userId": f"u-{i}"})
        assert rr.status_code == 201, rr.text
    r = await client.post(f"/v1/web/drive/{did}/sieges", json={"userId": "u-x"})
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"
