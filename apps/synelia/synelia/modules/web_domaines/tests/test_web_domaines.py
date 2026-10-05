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


async def test_parametres_entree_web(client_org):
    r = await client_org.get("/v1/web/domaines/parametres-entree")
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["dnsEntreeA"] == "198.244.179.212"
    assert corps["dnsEntreeWildcardCname"] == "dev01.ovh.smile.ci"


async def test_disponibilite(client_org, monkeypatch):
    from synelia.modules.web_domaines import service as domaines_service
    from synelia_openstack.registrar import RegistrarSimule

    appels_dns: list[str] = []

    class RegistrarDnsSpy(RegistrarSimule):
        def configurer_enregistrements_entree(
            self, domaine: str, ip_apex: str, cname_wildcard: str, *, ttl: int = 3600
        ) -> None:
            appels_dns.append(domaine)

        def domaine_sous_gestion(self, nom: str) -> bool:
            return True

    monkeypatch.setattr(domaines_service, "amont", RegistrarDnsSpy)

    r = await client_org.get("/v1/web/domaines/disponibilite", params={"nom": "monmarque.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["disponible"] is True and "prixAnnuel" in d

    r = await client_org.get("/v1/web/domaines/disponibilite", params={"nom": "google.com"})
    assert r.status_code == 200 and r.json()["disponible"] is False

    corps = {
        "nom": "deja-nous-demo.com",
        "dureeAnnees": 1,
        "titulaire": TITULAIRE,
    }
    r = await client_org.post("/v1/web/domaines", json=corps)
    assert r.status_code == 202
    appels_dns.clear()
    r = await client_org.get("/v1/web/domaines/disponibilite", params={"nom": "deja-nous-demo.com"})
    assert r.status_code == 200 and r.json()["disponible"] is False
    assert appels_dns == ["deja-nous-demo.com"]


async def test_erreurs_domaines(client_org):
    # Branches d'erreur simule-couvrables : 404 sur tous les endpoints détail, filtres
    # liste, disponibilité multi-extensions et domaine pris (avec suggestions).
    for methode, chemin, kwargs in [
        ("get", "/v1/web/domaines/domaine-inexistant", {}),
        ("patch", "/v1/web/domaines/domaine-inexistant", {"json": {"renouvellementAuto": True}}),
        ("post", "/v1/web/domaines/domaine-inexistant/code-auth", {}),
        (
            "post",
            "/v1/web/domaines/domaine-inexistant/renouvellement",
            {"json": {"dureeAnnees": 1}},
        ),
    ]:
        r = await getattr(client_org, methode)(chemin, **kwargs)
        assert r.status_code == 404, (methode, chemin, r.text)

    r = await client_org.get(
        "/v1/web/domaines/disponibilite", params={"nom": "mamarque", "extensions": "com,ci"}
    )
    assert r.status_code == 200 and r.json()["nom"] == "mamarque.com"

    r = await client_org.get("/v1/web/domaines", params={"extension": "com"})
    assert r.status_code == 200
    r = await client_org.get("/v1/web/domaines", params={"renouvellementAuto": "true"})
    assert r.status_code == 200


async def test_commander_cycle(client_org, monkeypatch):
    from synelia.modules.web_domaines import service as domaines_service
    from synelia_openstack.registrar import RegistrarSimule

    appels_dns: list[tuple[str, str, str]] = []

    class RegistrarDnsSpy(RegistrarSimule):
        def configurer_enregistrements_entree(
            self, domaine: str, ip_apex: str, cname_wildcard: str, *, ttl: int = 3600
        ) -> None:
            appels_dns.append((domaine, ip_apex, cname_wildcard))

        def domaine_sous_gestion(self, nom: str) -> bool:
            return True

    monkeypatch.setattr(domaines_service, "amont", RegistrarDnsSpy)

    corps = {
        "nom": "synelia-mon-domaine.ci",
        "dureeAnnees": 1,
        "renouvellementAuto": True,
        "whoisProtege": True,
        "titulaire": TITULAIRE,
    }
    r = await client_org.post("/v1/web/domaines", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["type"] == "domaine.commander" and travail["statut"] == "done"
    assert appels_dns == [
        ("synelia-mon-domaine.ci", "198.244.179.212", "dev01.ovh.smile.ci"),
    ]

    r = await client_org.get("/v1/web/domaines")
    assert r.status_code == 200
    doms = r.json()["donnees"]
    dom = next((d for d in doms if d["nom"] == "synelia-mon-domaine.ci"), None)
    assert dom is not None and dom["extension"] == "ci"
    did = dom["id"]

    r = await client_org.post("/v1/web/domaines", json=corps)
    assert r.status_code == 409 and r.json()["erreur"]["code"] == "nom_deja_pris"

    r = await client_org.get(f"/v1/web/domaines/{did}")
    assert r.status_code == 200
    agg = r.json()
    assert agg["domaine"]["nom"] == "synelia-mon-domaine.ci"

    r = await client_org.patch(f"/v1/web/domaines/{did}", json={"renouvellementAuto": False})
    assert r.status_code == 200 and r.json()["renouvellementAuto"] is False

    r = await client_org.post(f"/v1/web/domaines/{did}/code-auth")
    assert r.status_code == 200 and r.json()["code"]

    r = await client_org.get(f"/v1/web/domaines/{did}")
    expiration_avant = r.json()["domaine"]["expiration"]

    r = await client_org.post(f"/v1/web/domaines/{did}/renouvellement", json={"dureeAnnees": 2})
    assert r.status_code == 202 and r.json()["type"] == "domaine.renouveler"

    # Le renouvellement prolonge depuis l'échéance existante, pas depuis aujourd'hui.
    r = await client_org.get(f"/v1/web/domaines/{did}")
    expiration_apres = r.json()["domaine"]["expiration"]
    assert expiration_apres[:4] == str(int(expiration_avant[:4]) + 2)


async def test_resilier_domaine(client_org):
    corps = {
        "nom": "a-resilier-demo.com",
        "dureeAnnees": 1,
        "titulaire": TITULAIRE,
        "renouvellementAuto": True,
        "whoisProtege": True,
    }
    r = await client_org.post("/v1/web/domaines", json=corps)
    assert r.status_code == 202, r.text
    did = (await client_org.get("/v1/web/domaines")).json()["donnees"][-1]["id"]

    r = await client_org.delete(
        f"/v1/web/domaines/{did}", params={"confirmation": "a-resilier-demo.com"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["type"] == "domaine.resilier"

    r = await client_org.get(f"/v1/web/domaines/{did}")
    assert r.status_code == 404


async def test_transfert(client_org):
    corps = {"nom": "transfert-demo.com", "codeAuth": "ABC123", "renouvellementAuto": True}
    r = await client_org.post("/v1/web/domaines/transferts", json=corps)
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"

    r = await client_org.get("/v1/web/domaines")
    assert r.status_code == 200 and any(
        d["nom"] == "transfert-demo.com" for d in r.json()["donnees"]
    )


async def test_travail_entree_persisted(client_org):
    """Verify that the entree field is properly persisted in travail records."""
    # This test verifies the fix for the bug where entree was not being saved to the database
    from synelia_db.modeles import Travail
    from synelia_db.session import fabrique

    corps = {
        "nom": "entree-test-domain.ci",
        "dureeAnnees": 3,
        "renouvellementAuto": True,
        "whoisProtege": True,
        "titulaire": TITULAIRE,
    }
    r = await client_org.post("/v1/web/domaines", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    travail_id = travail["id"]

    # Verify entree is persisted in the database
    async with fabrique()() as session:
        db_travail = await session.get(Travail, travail_id)
        assert db_travail is not None
        assert db_travail.entree is not None, "entree field should not be null"
        assert db_travail.entree.get("dureeAnnees") == 3, (
            f"entree should contain dureeAnnees=3, got {db_travail.entree}"
        )
        assert db_travail.entree.get("nom") == "entree-test-domain.ci"


async def test_disponibilite_domaine_du_compte_registrar(client_org, monkeypatch):
    from synelia.modules.web_domaines import service as domaines_service
    from synelia_openstack.registrar import RegistrarSimule

    class RegistrarCompte(RegistrarSimule):
        def verifier(self, nom: str) -> bool:
            return False

        def domaine_du_compte(self, nom: str) -> bool:
            return nom == "demo-du-compte.com"

    monkeypatch.setattr(domaines_service, "amont", RegistrarCompte)
    r = await client_org.get("/v1/web/domaines/disponibilite", params={"nom": "demo-du-compte.com"})
    assert r.json()["disponible"] is True
    r = await client_org.get("/v1/web/domaines/disponibilite", params={"nom": "google.com"})
    assert r.json()["disponible"] is False
