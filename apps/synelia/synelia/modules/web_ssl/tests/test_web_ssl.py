"""SSL (Web Cloud) : offres, commande 202, validation, renouvellement, révocation."""


async def test_offres_certificat(client):
    r = await client.get("/v1/web/ssl/offres")
    assert r.status_code == 200
    offres = r.json()
    types = {o["type"] for o in offres}
    assert {"letsencrypt", "dv", "wildcard", "ov", "ev"} <= types
    le = next(o for o in offres if o["type"] == "letsencrypt")
    assert le["prixAnnuel"] == 0


async def test_cycle_certificat(client):
    r = await client.post(
        "/v1/web/ssl",
        json={
            "hote": "www.ci",
            "type": "letsencrypt",
            "validationDomaine": "dns",
            "renouvellementAuto": True,
        },
    )
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "web.ssl.commande" and travail["statut"] == "done"

    r = await client.get("/v1/web/ssl")
    assert r.status_code == 200
    certifs = r.json()["donnees"]
    assert len(certifs) == 1 and certifs[0]["etat"] == "actif"
    cid = certifs[0]["id"]

    r = await client.post(f"/v1/web/ssl/{cid}/validation")
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "certificat_deja_valide"

    r = await client.patch(f"/v1/web/ssl/{cid}", json={"renouvellementAuto": False})
    assert r.status_code == 200 and r.json()["renouvellementAuto"] is False

    r = await client.post(f"/v1/web/ssl/{cid}/renouvellement", json={"dureeAnnees": 1})
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "web.ssl.renew" and r.json()["statut"] == "done"
    cert = (await client.get(f"/v1/web/ssl/{cid}")).json()
    assert cert["etat"] == "actif"

    r = await client.delete(f"/v1/web/ssl/{cid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422
    r = await client.delete(f"/v1/web/ssl/{cid}", params={"confirmation": "www.ci"})
    assert r.status_code == 204
    cert = (await client.get(f"/v1/web/ssl/{cid}")).json()
    assert cert["etat"] == "revoque"


async def test_commande_le_expire_90j(client):
    r = await client.post(
        "/v1/web/ssl", json={"hote": "le.ci", "type": "letsencrypt", "validationDomaine": "http"}
    )
    assert r.status_code == 202
    cert = next(
        c for c in (await client.get("/v1/web/ssl")).json()["donnees"] if c["hote"] == "le.ci"
    )
    from datetime import date, timedelta

    attendu = date.today() + timedelta(days=90)
    assert date.fromisoformat(cert["expire"]) == attendu


async def test_erreurs_ssl(client):
    # Branches d'erreur simule-couvrables : 404 sur les endpoints détail, 409 doublon
    # de commande, filtre liste.
    for methode, chemin, kwargs in [
        ("get", "/v1/web/ssl/cert-inexistant", {}),
        ("patch", "/v1/web/ssl/cert-inexistant", {"json": {"renouvellementAuto": True}}),
        ("delete", "/v1/web/ssl/cert-inexistant", {"params": {"confirmation": "x"}}),
        ("post", "/v1/web/ssl/cert-inexistant/validation", {}),
        ("post", "/v1/web/ssl/cert-inexistant/renouvellement", {"json": {"dureeAnnees": 1}}),
    ]:
        r = await getattr(client, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)

    corps = {"hote": "dup.ci", "type": "letsencrypt", "validationDomaine": "dns"}
    r = await client.post("/v1/web/ssl", json=corps)
    assert r.status_code == 202, r.text
    r = await client.post("/v1/web/ssl", json=corps)
    assert r.status_code == 409

    r = await client.get("/v1/web/ssl", params={"etat": "actif"})
    assert r.status_code == 200
