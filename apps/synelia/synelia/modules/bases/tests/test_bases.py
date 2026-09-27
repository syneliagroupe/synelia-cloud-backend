"""Bases managées : cycle de vie, identifiants, réplicas, restauration."""

from synelia_testing import connexion_lab, sur_lab_reel


async def _espace(client_org) -> str:
    existants = (await client_org.get("/v1/espaces")).json()["donnees"]
    for e in existants:
        if e["code"] == "demo-abj":
            return e["id"]
    r = await client_org.post(
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
    return (await client_org.get("/v1/espaces")).json()["donnees"][0]["id"]


async def test_cycle_base(client_org):
    espace_id = await _espace(client_org)
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
    r = await client_org.post("/v1/bases", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "base.create" and travail["statut"] == "done"

    r = await client_org.get("/v1/bases")
    assert r.status_code == 200
    bases = r.json()["donnees"]
    assert len(bases) == 1 and bases[0]["nom"] == "app-prod"
    bid = bases[0]["id"]
    # Impact Nova réel : `base.create` provisionne `db-<8 premiers de l'id>`.
    # Instantané impossible avant création (l'id n'existe pas encore) : on exige
    # au moins un serveur au préfixe exact, introuvable en fumée simulée.
    if sur_lab_reel():
        c = connexion_lab()
        assert c is not None, "lab réel injoignable"
        trouves = list(c.compute.servers(name=f"db-{bid[:8]}", all_projects=True))
        assert trouves, f"aucun serveur Nova db-{bid[:8]} : base sans impact OpenStack"

    r = await client_org.get(f"/v1/bases/{bid}/identifiants")
    assert r.status_code == 200
    ident = r.json()
    # Le host est désormais l'IP privée réelle de la VM amont (aucune IP flottante :
    # la base reste sur le réseau privé de l'Espace Cloud), plus de nom DNS interne fabriqué.
    assert ident["host"].count(".") == 3 and ident["utilisateur"] == "synelia_postgresql"

    r = await client_org.post(f"/v1/bases/{bid}/identifiants/rotation", json={})
    assert r.status_code == 200
    rotation = r.json()
    assert rotation["motDePasse"]

    r = await client_org.get(f"/v1/bases/{bid}/metriques", params={"fenetre": "7j"})
    assert r.status_code == 200
    series = r.json()["series"]
    # Pas de supervision temps réel branchée pour les bases managées : les séries sont
    # déclarées (métrique/unité/fenêtre honorée) mais sans points historiques pour l'instant.
    assert {s["metrique"] for s in series} == {"cpu", "ram", "disque", "connexions"}
    assert all(s["fenetre"] == "7j" and s["points"] == [] for s in series)

    # Une fenêtre inconnue retombe sur la valeur par défaut plutôt que de planter.
    r = await client_org.get(f"/v1/bases/{bid}/metriques", params={"fenetre": "n_importe_quoi"})
    assert r.status_code == 200
    assert all(s["fenetre"] == "24h" for s in r.json()["series"])

    r = await client_org.post(f"/v1/bases/{bid}/replicas", json={"site": "ABJ"})
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.post(
        f"/v1/bases/{bid}/restauration",
        json={"instant": "2026-09-01T10:00:00Z", "nomCible": "app-prod-restore"},
    )
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.delete(f"/v1/bases/{bid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/bases/{bid}", params={"confirmation": "app-prod"})
    assert r.status_code == 202 and r.json()["statut"] == "done"

    r = await client_org.get("/v1/bases")
    assert r.json()["pagination"]["total"] == 0
