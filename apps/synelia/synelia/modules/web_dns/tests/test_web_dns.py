"""Web Cloud — DNS : zones, enregistrements, DNSSEC, modèles."""

from synelia_testing import connexion_lab, sur_lab_reel


def _affirmer_zone_designate(domaine: str):
    """La zone doit exister pour de vrai côté Designate (nom pleinement qualifié)."""
    if not sur_lab_reel():
        return
    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    nom_zone = domaine if domaine.endswith(".") else f"{domaine}."
    assert c.dns.find_zone(nom_zone, ignore_missing=True) is not None, (
        f"zone Designate {nom_zone!r} introuvable : création sans impact OpenStack"
    )


def _affirmer_zone_designate_absente(domaine: str):
    if not sur_lab_reel():
        return
    import time

    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    nom_zone = domaine if domaine.endswith(".") else f"{domaine}."
    # La suppression Designate est asynchrone (PENDING_DELETE → purge par le backend
    # BIND9) : on attend la disparition réelle au lieu d'exiger l'immédiat.
    for _ in range(30):
        if c.dns.find_zone(nom_zone, ignore_missing=True) is None:
            return
        time.sleep(3)
    assert False, f"zone Designate {nom_zone!r} toujours présente : suppression sans impact"


async def test_modeles(client):
    r = await client.get("/v1/web/dns/modeles")
    assert r.status_code == 200
    modeles = r.json()
    assert len(modeles) >= 3 and any(m["id"] == "courrier" for m in modeles)


async def test_cycle_zone_dns(client):
    r = await client.post("/v1/web/dns", json={"domaine": "demo-dns.com"})
    assert r.status_code == 201, r.text
    zone = r.json()
    assert zone["domaine"] == "demo-dns.com" and zone["dnssec"] is False
    zid = zone["id"]
    _affirmer_zone_designate("demo-dns.com")

    r = await client.post("/v1/web/dns", json={"domaine": "demo-dns.com"})
    assert r.status_code == 409

    r = await client.get("/v1/web/dns")
    assert r.status_code == 200 and any(z["domaine"] == "demo-dns.com" for z in r.json()["donnees"])

    r = await client.get(f"/v1/web/dns/{zid}")
    assert r.status_code == 200

    r = await client.post(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"type": "A", "nom": "@", "valeur": "192.168.0.10", "ttl": 3600},
    )
    assert r.status_code == 201, r.text
    assert any(e["type"] == "A" for e in r.json()["enregistrements"])

    r = await client.post(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"type": "A", "nom": "@", "valeur": "192.168.0.11", "ttl": 3600},
    )
    assert r.status_code == 409

    r = await client.put(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"enregistrements": [{"type": "TXT", "nom": "@", "valeur": "v=spf1 -all"}]},
    )
    assert r.status_code == 200
    enregs = r.json()["enregistrements"]
    assert len(enregs) == 1 and enregs[0]["type"] == "TXT"
    eid = enregs[0]["id"]

    r = await client.patch(
        f"/v1/web/dns/{zid}/enregistrements/{eid}",
        json={"type": "TXT", "nom": "@", "valeur": "v=spf1 ~all", "ttl": 1800},
    )
    assert r.status_code == 200 and r.json()["enregistrements"][0]["valeur"] == "v=spf1 ~all"

    r = await client.delete(f"/v1/web/dns/{zid}/enregistrements/{eid}")
    assert r.status_code == 204
    r = await client.get(f"/v1/web/dns/{zid}")
    assert r.json()["enregistrements"] == []

    r = await client.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": True})
    assert r.status_code == 200 and r.json()["dnssec"] is True

    r = await client.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": True})
    assert r.status_code == 409

    r = await client.post(f"/v1/web/dns/{zid}/modeles/courrier", json={"remplacerExistants": False})
    assert r.status_code == 200
    assert any(e["type"] == "MX" for e in r.json()["enregistrements"])

    r = await client.delete(f"/v1/web/dns/{zid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client.delete(f"/v1/web/dns/{zid}", params={"confirmation": "demo-dns.com"})
    assert r.status_code == 204

    r = await client.get("/v1/web/dns")
    assert r.status_code == 200 and all(z["id"] != zid for z in r.json()["donnees"])
    _affirmer_zone_designate_absente("demo-dns.com")
