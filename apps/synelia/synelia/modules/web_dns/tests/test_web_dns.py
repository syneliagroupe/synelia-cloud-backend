"""Web Cloud — DNS : zones, enregistrements, DNSSEC, modèles."""

import uuid

from synelia_testing import connexion_lab, sur_lab_reel

# Designate est global : un domaine fixe collisionne dès le 2e run sur un lab partagé
# (« 409 Duplicate Zone »), contrairement aux dépôts SQLite éphémères des tests. On
# dérive donc des noms uniques par run pour toute zone réellement créée côté amont.
_SUFFIXE = uuid.uuid4().hex[:8]
DOMAINE_CYCLE = f"demo-dns-{_SUFFIXE}.com"
DOMAINE_ERREURS = f"erreurs-dns-{_SUFFIXE}.com"


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


async def test_modeles(client_org):
    r = await client_org.get("/v1/web/dns/modeles")
    assert r.status_code == 200
    modeles = r.json()
    assert len(modeles) >= 3 and any(m["id"] == "courrier" for m in modeles)


async def test_erreurs_dns(client_org):
    # Branches d'erreur simule-couvrables : 404 zone inconnue (GET/DELETE), 404 modèle
    # inconnu, filtre liste `dnssec`, 409 doublon d'enregistrement.
    r = await client_org.get("/v1/web/dns/zone-inexistante")
    assert r.status_code == 404
    r = await client_org.delete("/v1/web/dns/zone-inexistante", params={"confirmation": "x"})
    assert r.status_code == 404

    r = await client_org.post("/v1/web/dns", json={"domaine": DOMAINE_ERREURS})
    assert r.status_code == 201, r.text
    zid = r.json()["id"]

    r = await client_org.get("/v1/web/dns", params={"dnssec": "true"})
    assert r.status_code == 200 and all(z["id"] != zid for z in r.json()["donnees"])
    r = await client_org.get("/v1/web/dns", params={"dnssec": "false"})
    assert r.status_code == 200 and any(z["id"] == zid for z in r.json()["donnees"])

    r = await client_org.post(
        f"/v1/web/dns/{zid}/modeles/modele-inconnu", json={"remplacerExistants": False}
    )
    assert r.status_code == 404

    corps_a = {"type": "A", "nom": "@", "valeur": "192.168.0.10", "ttl": 3600}
    r = await client_org.post(f"/v1/web/dns/{zid}/enregistrements", json=corps_a)
    assert r.status_code == 201, r.text
    r = await client_org.post(f"/v1/web/dns/{zid}/enregistrements", json=corps_a)
    assert r.status_code == 409

    r = await client_org.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": False})
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "dnssec_etat_identique"

    r = await client_org.delete(f"/v1/web/dns/{zid}", params={"confirmation": DOMAINE_ERREURS})
    assert r.status_code == 204


async def test_cycle_zone_dns(client_org):
    r = await client_org.post("/v1/web/dns", json={"domaine": DOMAINE_CYCLE})
    assert r.status_code == 201, r.text
    zone = r.json()
    assert zone["domaine"] == DOMAINE_CYCLE and zone["dnssec"] is False
    zid = zone["id"]
    _affirmer_zone_designate(DOMAINE_CYCLE)

    r = await client_org.post("/v1/web/dns", json={"domaine": DOMAINE_CYCLE})
    assert r.status_code == 409

    r = await client_org.get("/v1/web/dns")
    assert r.status_code == 200 and any(z["domaine"] == DOMAINE_CYCLE for z in r.json()["donnees"])

    r = await client_org.get(f"/v1/web/dns/{zid}")
    assert r.status_code == 200

    r = await client_org.post(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"type": "A", "nom": "@", "valeur": "192.168.0.10", "ttl": 3600},
    )
    assert r.status_code == 201, r.text
    assert any(e["type"] == "A" for e in r.json()["enregistrements"])
    amont_a = _recordsets_designate(DOMAINE_CYCLE, "A")
    if amont_a is not None:
        assert any("192.168.0.10" in (rs.records or []) for rs in amont_a), (
            "enregistrement A absent de Designate : écriture sans impact OpenStack"
        )

    r = await client_org.post(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"type": "A", "nom": "@", "valeur": "192.168.0.11", "ttl": 3600},
    )
    assert r.status_code == 409

    r = await client_org.put(
        f"/v1/web/dns/{zid}/enregistrements",
        json={"enregistrements": [{"type": "TXT", "nom": "@", "valeur": "v=spf1 -all"}]},
    )
    assert r.status_code == 200
    enregs = r.json()["enregistrements"]
    assert len(enregs) == 1 and enregs[0]["type"] == "TXT"
    eid = enregs[0]["id"]

    r = await client_org.patch(
        f"/v1/web/dns/{zid}/enregistrements/{eid}",
        json={"type": "TXT", "nom": "@", "valeur": "v=spf1 ~all", "ttl": 1800},
    )
    assert r.status_code == 200 and r.json()["enregistrements"][0]["valeur"] == "v=spf1 ~all"
    amont_txt = _recordsets_designate(DOMAINE_CYCLE, "TXT")
    if amont_txt is not None:
        assert any(
            any("v=spf1 ~all" in (v or "") for v in (rs.records or [])) for rs in amont_txt
        ), "TXT modifié absent de Designate : modification sans impact OpenStack"

    r = await client_org.delete(f"/v1/web/dns/{zid}/enregistrements/{eid}")
    assert r.status_code == 204
    amont_txt_apres = _recordsets_designate(DOMAINE_CYCLE, "TXT")
    if amont_txt_apres is not None:
        # NS/SOA par défaut exclus : seuls nos TXT applicatifs comptent.
        assert not any((rs.name or "").rstrip(".") == DOMAINE_CYCLE for rs in amont_txt_apres), (
            "TXT supprimé toujours présent dans Designate : suppression sans impact"
        )
    r = await client_org.get(f"/v1/web/dns/{zid}")
    assert r.json()["enregistrements"] == []

    r = await client_org.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": True})
    assert r.status_code == 200 and r.json()["dnssec"] is True

    r = await client_org.put(f"/v1/web/dns/{zid}/dnssec", json={"actif": True})
    assert r.status_code == 409

    r = await client_org.post(
        f"/v1/web/dns/{zid}/modeles/courrier", json={"remplacerExistants": False}
    )
    assert r.status_code == 200
    assert any(e["type"] == "MX" for e in r.json()["enregistrements"])

    r = await client_org.delete(f"/v1/web/dns/{zid}", params={"confirmation": "mauvais"})
    assert r.status_code == 422

    r = await client_org.delete(f"/v1/web/dns/{zid}", params={"confirmation": DOMAINE_CYCLE})
    assert r.status_code == 204

    r = await client_org.get("/v1/web/dns")
    assert r.status_code == 200 and all(z["id"] != zid for z in r.json()["donnees"])
    _affirmer_zone_designate_absente(DOMAINE_CYCLE)
