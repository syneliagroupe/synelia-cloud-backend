"""Web Cloud — domaines : commande, disponibilité, transfert, code-auth, renouvellement, agrégat."""

TITULAIRE = {
    "nom": "Synelia",
    "email": "admin@synelia.cloud",
    "telephone": "+2250102030405",
    "adresse": "Abidjan",
    "ville": "Abidjan",
    "codePostal": "01",
    "pays": "CI",
}


async def test_disponibilite(client):
    r = await client.get("/v1/web/domaines/disponibilite", params={"nom": "monmarque.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["disponible"] is True and "prixAnnuel" in d

    r = await client.get("/v1/web/domaines/disponibilite", params={"nom": "google.com"})
    assert r.status_code == 200 and r.json()["disponible"] is False


async def test_commander_cycle(client):
    corps = {
        "nom": "synelia-mon-domaine.ci",
        "dureeAnnees": 1,
        "renouvellementAuto": True,
        "whoisProtege": True,
        "titulaire": TITULAIRE,
    }
    r = await client.post("/v1/web/domaines", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "domaine.commander" and travail["statut"] == "done"

    r = await client.get("/v1/web/domaines")
    assert r.status_code == 200
    doms = r.json()["donnees"]
    dom = next((d for d in doms if d["nom"] == "synelia-mon-domaine.ci"), None)
    assert dom is not None and dom["extension"] == "ci"
    did = dom["id"]

    r = await client.post("/v1/web/domaines", json=corps)
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client.get(f"/v1/web/domaines/{did}")
    assert r.status_code == 200
    agg = r.json()
    assert agg["domaine"]["nom"] == "synelia-mon-domaine.ci"

    r = await client.patch(f"/v1/web/domaines/{did}", json={"renouvellementAuto": False})
    assert r.status_code == 200 and r.json()["renouvellementAuto"] is False

    r = await client.post(f"/v1/web/domaines/{did}/code-auth")
    assert r.status_code == 200 and r.json()["code"]

    r = await client.get(f"/v1/web/domaines/{did}")
    expiration_avant = r.json()["domaine"]["expiration"]

    r = await client.post(f"/v1/web/domaines/{did}/renouvellement", json={"dureeAnnees": 2})
    assert r.status_code == 202 and r.json()["type"] == "domaine.renouveler"

    # Le renouvellement prolonge depuis l'échéance existante, pas depuis aujourd'hui.
    r = await client.get(f"/v1/web/domaines/{did}")
    expiration_apres = r.json()["domaine"]["expiration"]
    assert expiration_apres[:4] == str(int(expiration_avant[:4]) + 2)


async def test_transfert(client):
    corps = {"nom": "transfert-demo.com", "codeAuth": "ABC123", "renouvellementAuto": True}
    r = await client.post("/v1/web/domaines/transferts", json=corps)
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    r = await client.get("/v1/web/domaines")
    assert r.status_code == 200 and any(
        d["nom"] == "transfert-demo.com" for d in r.json()["donnees"]
    )
