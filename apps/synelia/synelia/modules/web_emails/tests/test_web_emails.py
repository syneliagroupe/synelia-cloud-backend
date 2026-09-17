"""Messagerie (Web Cloud emails) : activation 202, boîtes 201/409/402, alias, authentification, webmail."""


async def test_cycle_messagerie(client):
    r = await client.post(
        "/v1/web/emails", json={"domaine": "exemple.ci", "palier": "pro", "boites": 4}
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.email.activate" and travail["statut"] == "done"

    r = await client.get("/v1/web/emails")
    assert r.status_code == 200
    messageries = r.json()["donnees"]
    assert len(messageries) == 1
    mess = messageries[0]
    assert mess["actif"] is True
    assert mess["authentification"]["spf"] == "valide"
    assert mess["authentification"]["dkim"] == "valide"
    assert mess["authentification"]["dmarc"]
    mid = mess["id"]

    r = await client.post("/v1/web/emails", json={"domaine": "exemple.ci", "palier": "pro"})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "a@exemple.ci", "nom": "A", "quotaGo": 5}
    )
    assert r.status_code == 201, r.text
    assert r.json()["adresse"] == "a@exemple.ci"

    r = await client.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "a@exemple.ci", "nom": "A"}
    )
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client.patch(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", json={"quotaGo": 8, "mfa": True}
    )
    assert r.status_code == 200 and r.json()["quotaGo"] == 8 and r.json()["mfa"] is True

    r = await client.put(
        f"/v1/web/emails/{mid}/alias",
        json={"alias": [{"de": "contact@exemple.ci", "vers": ["a@exemple.ci"]}]},
    )
    assert r.status_code == 200 and r.json()["alias"][0]["de"] == "contact@exemple.ci"

    r = await client.post(f"/v1/web/emails/{mid}/authentification/verification")
    assert r.status_code == 200 and r.json()["spf"] == "valide"

    r = await client.post(f"/v1/web/emails/{mid}/ouverture", json={"adresse": "a@exemple.ci"})
    assert r.status_code == 201, r.text
    ouv = r.json()
    assert ouv["url"].startswith("https://") and ouv["methode"] == "redirection"

    r = await client.delete(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", params={"confirmation": "mauvais"}
    )
    assert r.status_code == 422

    r = await client.delete(
        f"/v1/web/emails/{mid}/boites/a@exemple.ci", params={"confirmation": "a@exemple.ci"}
    )
    assert r.status_code == 204
    r = await client.get(f"/v1/web/emails/{mid}")
    assert len(r.json()["boites"]) == 0

    r = await client.delete(f"/v1/web/emails/{mid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client.delete(f"/v1/web/emails/{mid}", params={"confirmation": "exemple.ci"})
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.email.deactivate" and travail["statut"] == "done"

    r = await client.get(f"/v1/web/emails/{mid}")
    assert r.status_code == 404


async def test_quota_boites(client):
    r = await client.post("/v1/web/emails", json={"domaine": "quota.ci", "palier": "starter"})
    assert r.status_code == 202
    mess = next(
        m
        for m in (await client.get("/v1/web/emails")).json()["donnees"]
        if m["domaine"] == "quota.ci"
    )
    mid = mess["id"]
    for i in range(mess["boitesIncluses"]):
        rr = await client.post(
            f"/v1/web/emails/{mid}/boites", json={"adresse": f"b{i}@quota.ci", "nom": f"B{i}"}
        )
        assert rr.status_code == 201, rr.text
    r = await client.post(
        f"/v1/web/emails/{mid}/boites", json={"adresse": "trop@quota.ci", "nom": "Trop"}
    )
    assert r.status_code == 402 and r.json()["erreur"]["code"] == "quota_depasse"

    # Suppression avec des boîtes encore actives : l'exécuteur doit les retirer avant
    # le domaine (Zimbra refuse `DeleteDomainRequest` sur un domaine non vide).
    r = await client.delete(f"/v1/web/emails/{mid}", params={"confirmation": "quota.ci"})
    assert r.status_code == 202, r.text
    r = await client.get(f"/v1/web/emails/{mid}")
    assert r.status_code == 404
