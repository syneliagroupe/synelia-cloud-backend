"""Relais SMTP (Web Cloud) : relais par org, clés, identifiants, messages, test, webhooks."""


async def test_cycle_relais(client):
    r = await client.get("/v1/web/smtp")
    assert r.status_code == 200
    assert r.json()["actif"] is False  # relais par défaut inactif

    r = await client.post(
        "/v1/web/smtp", json={"domainesAutorises": ["exemple.ci"], "quotaJour": 500}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "smtp.activate" and r.json()["statut"] == "done"

    relais = (await client.get("/v1/web/smtp")).json()
    assert relais["actif"] is True
    assert relais["hote"] == "smtp.synelia.cloud"
    assert 587 in relais["ports"]

    r = await client.post("/v1/web/smtp", json={"domainesAutorises": ["autre.ci"]})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "relais_deja_actif"

    r = await client.patch("/v1/web/smtp", json={"domainesAutorises": ["exemple.ci", "b.ci"]})
    assert r.status_code == 200 and "b.ci" in r.json()["domainesAutorises"]

    # Régression : `quotaJour` (champ plat du contrat) doit se répercuter sur le quota
    # imbriqué réellement appliqué par le relais, pas se perdre à côté de `quota`.
    r = await client.patch("/v1/web/smtp", json={"quotaJour": 4000})
    assert r.status_code == 200 and r.json()["quota"]["parJour"] == 4000
    relais = (await client.get("/v1/web/smtp")).json()
    assert relais["quota"]["parJour"] == 4000

    r = await client.post(
        "/v1/web/smtp/test", json={"destinataire": "x@exemple.ci", "de": "y@exemple.ci"}
    )
    assert r.status_code == 200 and r.json()["envoye"] is True

    r = await client.get("/v1/web/smtp/messages")
    assert r.status_code == 200 and r.json()["donnees"] == []


async def test_cles_smtp(client):
    await client.post("/v1/web/smtp", json={"domainesAutorises": ["exemple.ci"]})
    r = await client.post("/v1/web/smtp/cles", json={"nom": "app-prod", "quotaJour": 200})
    assert r.status_code == 201, r.text
    secret = r.json()
    assert secret["motDePasse"] and secret["hote"] == "smtp.synelia.cloud"
    cle = secret["cle"]
    cid = cle["id"]

    r = await client.patch(f"/v1/web/smtp/cles/{cid}", json={"quotaJour": 300})
    assert r.status_code == 200 and r.json()["quotaJour"] == 300

    r = await client.delete(f"/v1/web/smtp/cles/{cid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client.delete(f"/v1/web/smtp/cles/{cid}", params={"confirmation": "app-prod"})
    assert r.status_code == 204
    cles = (await client.get("/v1/web/smtp/cles")).json()
    assert cles[0]["statut"] == "revoquee"


async def test_identifiants_regenerer(client):
    await client.post("/v1/web/smtp", json={"domainesAutorises": ["exemple.ci"]})
    r = await client.post("/v1/web/smtp/identifiants", json={"confirmation": "regenerer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["motDePasse"] and body["identifiant"]


async def test_webhooks_smtp(client):
    await client.post("/v1/web/smtp", json={"domainesAutorises": ["exemple.ci"]})
    r = await client.post(
        "/v1/web/smtp/webhooks",
        json={"url": "https://app.ci/cb", "evenements": ["rebond", "rejete"]},
    )
    assert r.status_code == 201, r.text
    w = r.json()
    wid = w["id"]
    assert w["actif"] is True and w["secretDefini"] is False

    r = await client.patch(
        f"/v1/web/smtp/webhooks/{wid}",
        json={"url": "https://app.ci/cb2", "evenements": ["rebond"], "actif": False},
    )
    assert r.status_code == 200 and r.json()["actif"] is False

    r = await client.post(f"/v1/web/smtp/webhooks/{wid}/test")
    assert r.status_code == 200 and r.json()["envoye"] is True

    r = await client.delete(f"/v1/web/smtp/webhooks/{wid}")
    assert r.status_code == 204
    assert (await client.get("/v1/web/smtp/webhooks")).json() == []


async def test_erreurs_smtp(client):
    # Branches d'erreur simule-couvrables : PATCH avant activation (404), 404 sur les
    # endpoints détail (clés, webhooks), 422 confirmation, liste filtrée.
    r = await client.patch("/v1/web/smtp", json={"domainesAutorises": ["x.ci"]})
    assert r.status_code == 404

    for methode, chemin, kwargs in [
        ("patch", "/v1/web/smtp/cles/cle-inexistante", {"json": {"quotaJour": 1}}),
        ("delete", "/v1/web/smtp/cles/cle-inexistante", {"params": {"confirmation": "x"}}),
        ("delete", "/v1/web/smtp/webhooks/wh-inexistant", {}),
    ]:
        r = await getattr(client, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)

    await client.post("/v1/web/smtp", json={"domainesAutorises": ["exemple.ci"]})
    r = await client.post("/v1/web/smtp/cles", json={"nom": "ci", "quotaJour": 10})
    assert r.status_code == 201, r.text
    cid = r.json()["cle"]["id"]
    r = await client.delete(f"/v1/web/smtp/cles/{cid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client.get("/v1/web/smtp/cles", params={"statut": "active"})
    assert r.status_code == 200 and any(c["id"] == cid for c in r.json())
