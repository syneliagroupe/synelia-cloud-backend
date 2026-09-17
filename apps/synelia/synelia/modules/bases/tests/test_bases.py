"""Bases managées : cycle de vie, identifiants, réplicas, restauration."""


async def _espace(client) -> str:
    existants = (await client.get("/v1/espaces")).json()["donnees"]
    for e in existants:
        if e["code"] == "demo-abj":
            return e["id"]
    r = await client.post(
        "/v1/espaces",
        json={
            "code": "demo-abj",
            "offerId": "offre-standard",
            "site": "ABJ",
            "cidr": "10.10.0.0/16",
            "quota": {"vcpu": 16, "ramGo": 64, "stockageTo": 2},
        },
    )
    assert r.status_code == 202, r.text
    return (await client.get("/v1/espaces")).json()["donnees"][0]["id"]


async def test_cycle_base(client):
    espace_id = await _espace(client)
    corps = {
        "espaceId": espace_id,
        "nom": "app-prod",
        "moteur": "postgresql",
        "version": "16",
        "palier": "m1",
        "ha": True,
        "tailleGo": 50,
        "pitr": True,
        "replicas": 1,
    }
    r = await client.post("/v1/bases", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "base.create" and travail["statut"] == "done"

    r = await client.get("/v1/bases")
    assert r.status_code == 200
    bases = r.json()["donnees"]
    assert len(bases) == 1 and bases[0]["nom"] == "app-prod"
    bid = bases[0]["id"]

    r = await client.get(f"/v1/bases/{bid}/identifiants")
    assert r.status_code == 200
    ident = r.json()
    # Le host est désormais l'IP privée réelle de la VM amont (aucune IP flottante :
    # la base reste sur le réseau privé de l'Espace Cloud), plus de nom DNS interne fabriqué.
    assert ident["host"].count(".") == 3 and ident["utilisateur"] == "synelia_postgresql"

    r = await client.post(f"/v1/bases/{bid}/identifiants/rotation", json={})
    assert r.status_code == 200
    rotation = r.json()
    assert rotation["motDePasse"]

    r = await client.get(f"/v1/bases/{bid}/metriques")
    assert r.status_code == 200 and r.json()["series"] == []

    r = await client.post(f"/v1/bases/{bid}/replicas", json={"site": "ABJ"})
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client.post(
        f"/v1/bases/{bid}/restauration",
        json={"instant": "2026-09-01T10:00:00Z", "nomCible": "app-prod-restore"},
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client.delete(f"/v1/bases/{bid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client.delete(f"/v1/bases/{bid}", params={"confirmation": "app-prod"})
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client.get("/v1/bases")
    assert r.json()["pagination"]["total"] == 0
