"""Module de référence : création d'un Espace Cloud = 202 + travail exécuté, quota, confirmation."""

from synelia_testing import connexion_lab, sur_lab_reel


def _affirmer_projet_keystone(nom: str, present: bool):
    """Le projet Keystone `espace-<code>` doit exister (création) puis disparaître
    (suppression) sur lab réel : preuve d'impact OpenStack, pas de ligne DB seule."""
    if not sur_lab_reel():
        return
    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    trouve = c.identity.find_project(nom, ignore_missing=True)
    if present:
        assert trouve is not None, f"projet Keystone {nom!r} introuvable : espace sans impact"
    else:
        assert trouve is None, f"projet Keystone {nom!r} toujours présent : suppression sans impact"


async def test_cycle_espace(client):
    corps = {
        "code": "prod-abj",
        "offerId": "offre-standard",
        "site": "ABJ",
        "cidr": "10.10.0.0/16",
        "quota": {"vcpu": 16, "ramGo": 64, "stockageTo": 2},
    }
    r = await client.post("/v1/espaces", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "espace.create" and travail["statut"] == "done"
    assert all(t["statut"] == "ok" for t in travail["taches"])

    r = await client.get("/v1/espaces")
    assert r.status_code == 200
    espaces = [e for e in r.json()["donnees"] if e["code"] == "prod-abj"]
    assert len(espaces) == 1 and espaces[0]["statut"] == "active"
    eid = espaces[0]["id"]
    _affirmer_projet_keystone("espace-prod-abj", True)

    r = await client.get(f"/v1/travaux/{travail['id']}")
    assert r.status_code == 200 and r.json()["statut"] == "done"

    r = await client.post("/v1/espaces", json=corps)
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client.delete(f"/v1/espaces/{eid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422 and r.json()["erreur"]["code"] == "confirmation_invalide"

    r = await client.put(f"/v1/espaces/{eid}/quota", json={"vcpu": 8, "ramGo": 16, "stockageTo": 1})
    assert r.status_code == 200 and r.json()["quota"]["vcpu"] == 8

    r = await client.delete(f"/v1/espaces/{eid}", params={"confirmation": "prod-abj"})
    assert r.status_code == 202
    r = await client.get("/v1/espaces")
    assert all(e["code"] != "prod-abj" for e in r.json()["donnees"])
    _affirmer_projet_keystone("espace-prod-abj", False)

    r = await client.get(
        "/v1/audit"
    )  # module audit pas encore écrit → 404 chemin inconnu accepté ici
    assert r.status_code in (200, 404)
