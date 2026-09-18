"""Organisations : création (avec administrateur), liste, synthèse, suspension, emprunt d'identité.

Reste sur `client` (admin plateforme) : `org.manage` est réservé `super_admin` par le
RBAC (créer/modifier une Organisation est une action d'équipe — un client s'inscrit via
`/auth/inscription`, il ne crée pas d'organisation par cette API). Le côté client est
vérifié par `test_rbac_client.py` (accès refusé là où il faut)."""


async def test_cycle_organisation(client):
    r = await client.get("/v1/organisations")
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] >= 1

    corps = {
        "nom": "Acme CI",
        "pays": "CI",
        "secteur": "Fintech",
        "administrateur": {"nom": "Awa K.", "email": "awa@acme.ci"},
    }
    r = await client.post("/v1/organisations", json=corps)
    assert r.status_code == 201, r.text
    org = r.json()
    assert org["nom"] == "Acme CI" and org["statut"] == "active"
    oid = org["id"]
    assert org["utilisateurs"] == 1

    r = await client.get(f"/v1/organisations/{oid}")
    assert r.status_code == 200 and r.json()["nom"] == "Acme CI"

    r = await client.get(f"/v1/organisations/{oid}/synthese")
    assert r.status_code == 200
    syn = r.json()
    assert syn["organisation"]["id"] == oid
    assert syn["synthese"]["espaces"] == 0
    assert syn["impayes"] == [] and syn["tickets"] == []

    r = await client.patch(f"/v1/organisations/{oid}", json={"secteur": "Banque"})
    assert r.status_code == 200 and r.json()["secteur"] == "Banque"

    r = await client.post(
        f"/v1/organisations/{oid}/suspension",
        params={"confirmation": "mauvais"},
        json={"motif": "test"},
    )
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"

    r = await client.post(
        f"/v1/organisations/{oid}/suspension",
        params={"confirmation": "Acme CI"},
        json={"motif": "impayés"},
    )
    assert r.status_code == 200 and r.json()["statut"] == "suspendue"

    r = await client.delete(f"/v1/organisations/{oid}/suspension")
    assert r.status_code == 200 and r.json()["statut"] == "active"

    r = await client.post(
        f"/v1/organisations/{oid}/emprunt-identite", json={"motif": "support", "dureeMin": 5}
    )
    assert r.status_code == 201 and r.json()["accessToken"]


async def test_organisations_doublon_nom(client):
    corps = {"nom": "Unique Org", "pays": "CI"}
    r = await client.post("/v1/organisations", json=corps)
    assert r.status_code == 201
    r = await client.post("/v1/organisations", json=corps)
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"


async def test_utilisateurs(client):
    r = await client.get("/v1/utilisateurs")
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] >= 1
