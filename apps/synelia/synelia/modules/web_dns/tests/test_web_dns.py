"""Web Cloud — DNS : zones, enregistrements, DNSSEC, modèles."""

from synelia_testing import connexion_lab, sur_lab_reel


def _recordsets_designate(domaine: str, type_: str | None = None) -> list | None:
    """Recordsets Designate réels de la zone (`None` hors lab) : preuve d'impact —
    les enregistrements créés par l'API doivent exister côté Designate, pas seulement
    dans la fiche DB."""
    if not sur_lab_reel():
        return None
    c = connexion_lab()
    assert c is not None, "lab réel injoignable"
    nom_zone = domaine if domaine.endswith(".") else f"{domaine}."
    zone = c.dns.find_zone(nom_zone, ignore_missing=True)
    assert zone is not None, f"zone Designate {nom_zone!r} introuvable"
    tous = list(c.dns.recordsets(zone.id))
    if type_ is None:
        return tous
    return [r for r in tous if r.type == type_]


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


async def test_erreurs_dns(client):
    # Branches d'erreur simule-couvrables : 404 zone inconnue (GET/DELETE), 404 modèle
    # inconnu, filtre liste `dnssec`, 409 doublon d'enregistrement.
    r = await client.get("/v1/web/dns/zone-inexistante")
    assert r.status_code == 404
    r = await client.delete("/v1/web/dns/zone-inexistante", params={"confirmation": "x"})
    assert r.status_code == 404

    r = await client.post("/v1/web/dns", json={"domaine": "erreurs-dns.com"})
    assert r.status_code == 201, r.text
    zid = r.json()["id"]

    r = await client.get("/v1/web/dns", params={"dnssec": "true"})
    assert r.status_code == 200 and all(z["id"] != zid for z in r.json()["donnees"])
    r = await client.get("/v1/web/dns", params={"dnssec": "false"})
    assert r.status_code == 200 and any(z["id"] == zid for z in r.json()["donnees"])

    r = await client.post(
        f"/v1/web/dns/{zid}/modeles/modele-inconnu", json={"remplacerExistants": False}
    )
    assert r.status_code == 404

    corps_a = {"type": "A", "nom": "@", "valeur": "192.168.0.10", "ttl": 3600}
    r = await client.post(f"/v1/web/dns/{zid}/enregistrements", json=corps_a)
    assert r.status_code == 201, r.text
    r = await client.post(f"/v1/web/dns/{zid}/enregistrements", json=corps_a)
    assert r.status_code == 409

    r = await client.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": False})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "dnssec_etat_identique"

    r = await client.delete(f"/v1/web/dns/{zid}", params={"confirmation": "erreurs-dns.com"})
    assert r.status_code == 204


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
    amont_a = _recordsets_designate("demo-dns.com", "A")
    if amont_a is not None:
        assert any(
            "192.168.0.10" in (rs.records or []) for rs in amont_a
        ), "enregistrement A absent de Designate : écriture sans impact OpenStack"

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
    amont_txt = _recordsets_designate("demo-dns.com", "TXT")
    if amont_txt is not None:
        assert any(
            any("v=spf1 ~all" in (v or "") for v in (rs.records or [])) for rs in amont_txt
        ), "TXT modifié absent de Designate : modification sans impact OpenStack"

    r = await client.delete(f"/v1/web/dns/{zid}/enregistrements/{eid}")
    assert r.status_code == 204
    amont_txt_apres = _recordsets_designate("demo-dns.com", "TXT")
    if amont_txt_apres is not None:
        # NS/SOA par défaut exclus : seuls nos TXT applicatifs comptent.
        assert not any(
            (rs.name or "").rstrip(".") == "demo-dns.com" for rs in amont_txt_apres
        ), "TXT supprimé toujours présent dans Designate : suppression sans impact"
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
