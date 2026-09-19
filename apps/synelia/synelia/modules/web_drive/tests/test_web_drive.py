"""Drive (Web Cloud) : activation 202, sièges 201/409/402, ouverture 201.

Un domaine n'a qu'un seul serveur (VPS) : Drive s'installe sur l'hébergement déjà en
service pour ce domaine, il n'obtient plus sa propre VM — l'activer sans hébergement
préalable est désormais un vrai refus (409), pas une simulation muette."""


async def _creer_hebergement(client_org, nom: str) -> None:
    import pytest
    from synelia_testing import enregistrer_domaine

    await enregistrer_domaine(client_org, nom)
    r = await client_org.post(
        "/v1/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": nom}
    )
    assert r.status_code == 202, r.text
    data = r.json()
    # Lab injoignable depuis ce host (No route to host 192.168.26.234) → skip honnête
    if data["statut"] == "rolled_back" and "reseau_id" in data.get("erreur", {}).get("message", "").lower():
        pytest.skip(f"lab VPS injoignable (reseau_id absent): {data['erreur']['message']}")
    if data["statut"] == "rolled_back" and "no route to host" in data.get("erreur", {}).get("message", "").lower():
        pytest.skip(f"lab OpenStack injoignable: {data['erreur']['message']}")
    assert data["statut"] == "done", data


async def test_drive_refuse_sans_hebergement(client_org):
    r = await client_org.post(
        "/v1/web/drive", json={"domaine": "sans-vps.ci", "palier": "starter", "sieges": 1}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["statut"] == "failed"
    assert "hébergement" in travail["erreur"]["message"].lower()


async def test_cycle_drive(client_org):
    await _creer_hebergement(client_org, "cloud.ci")
    r = await client_org.post(
        "/v1/web/drive", json={"domaine": "cloud.ci", "palier": "pro", "sieges": 3}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.drive.activate" and travail["statut"] == "done"

    r = await client_org.get("/v1/web/drive")
    assert r.status_code == 200
    drives = r.json()["donnees"]
    assert len(drives) == 1 and drives[0]["actif"] is True
    did = drives[0]["id"]

    r = await client_org.patch(f"/v1/web/drive/{did}", json={"palier": "starter"})
    assert r.status_code == 200 and r.json()["palier"] == "starter"

    r = await client_org.post(f"/v1/web/drive/{did}/ouverture")
    assert r.status_code == 201, r.text
    ouv = r.json()
    assert ouv["url"].startswith("https://") and ouv["methode"] == "redirection"

    r = await client_org.post(
        f"/v1/web/drive/{did}/sieges", json={"userId": "u-1", "quotaTotal": 20}
    )
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "actif"

    r = await client_org.post(f"/v1/web/drive/{did}/sieges", json={"userId": "u-1"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "siege_deja_attribue"

    r = await client_org.get(f"/v1/web/drive/{did}/sieges")
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client_org.get(f"/v1/web/drive/{did}")
    assert r.json()["sieges"]["attribues"] == 1


async def test_quota_drive(client_org):
    await _creer_hebergement(client_org, "quota.ci")
    r = await client_org.post(
        "/v1/web/drive", json={"domaine": "quota.ci", "palier": "starter", "sieges": 2}
    )
    assert r.status_code == 202
    did = next(
        d["id"]
        for d in (await client_org.get("/v1/web/drive")).json()["donnees"]
        if d["domaine"] == "quota.ci"
    )
    for i in range(2):
        rr = await client_org.post(f"/v1/web/drive/{did}/sieges", json={"userId": f"u-{i}"})
        assert rr.status_code == 201, rr.text
    r = await client_org.post(f"/v1/web/drive/{did}/sieges", json={"userId": "u-x"})
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"


async def test_erreurs_drive(client_org):
    # Branches d'erreur simule-couvrables : 404 sur les endpoints détail, 409 doublon
    # d'activation, 422 confirmation, liste filtrée.
    for methode, chemin, kwargs in [
        ("get", "/v1/web/drive/drive-inexistant", {}),
        ("patch", "/v1/web/drive/drive-inexistant", {"json": {"palier": "pro"}}),
        ("delete", "/v1/web/drive/drive-inexistant", {"params": {"confirmation": "x"}}),
        ("post", "/v1/web/drive/drive-inexistant/ouverture", {}),
        ("get", "/v1/web/drive/drive-inexistant/sieges", {}),
        ("post", "/v1/web/drive/drive-inexistant/sieges", {"json": {"userId": "u-1"}}),
    ]:
        r = await getattr(client_org, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)

    await _creer_hebergement(client_org, "erreurs.ci")
    corps = {"domaine": "erreurs.ci", "palier": "pro", "sieges": 1}
    r = await client_org.post("/v1/web/drive", json=corps)
    assert r.status_code == 202, r.text
    r = await client_org.post("/v1/web/drive", json=corps)
    assert r.status_code == 409

    did = next(
        d["id"]
        for d in (await client_org.get("/v1/web/drive")).json()["donnees"]
        if d["domaine"] == "erreurs.ci"
    )
    r = await client_org.delete(f"/v1/web/drive/{did}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client_org.delete(f"/v1/web/drive/{did}", params={"confirmation": "erreurs.ci"})
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
