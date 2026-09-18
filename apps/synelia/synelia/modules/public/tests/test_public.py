"""Tests de la vitrine publique (CtxPublic, sans authentification)."""

PUB = {"Authorization": ""}


async def test_catalogue_services(client_org):
    r = await client_org.get("/v1/public/catalogue/services", headers=PUB)
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["pagination"]["total"] == 13
    assert corps["donnees"][0]["slug"] == "drive-pro"


async def test_catalogue_service_slug(client_org):
    r = await client_org.get("/v1/public/catalogue/services/drive-pro", headers=PUB)
    assert r.status_code == 200, r.text
    assert r.json()["nom"] == "Drive Pro"
    r = await client_org.get("/v1/public/catalogue/services/inconnu", headers=PUB)
    assert r.status_code == 404


async def test_contact(client_org):
    r = await client_org.post(
        "/v1/public/contact",
        headers=PUB,
        json={"nom": "Awa", "email": "awa@exemple.ci", "sujet": "commercial", "message": "Bonjour"},
    )
    assert r.status_code == 201, r.text
    assert "reference" in r.json()


async def test_devis(client_org):
    r = await client_org.post(
        "/v1/public/devis",
        headers=PUB,
        json={
            "contact": {
                "nom": "Awa",
                "email": "awa@exemple.ci",
                "sujet": "commercial",
                "message": "Dev",
            },
            "besoin": "Espace cloud",
        },
    )
    assert r.status_code == 201, r.text
    assert "reference" in r.json()


async def test_couverture(client_org):
    r = await client_org.get("/v1/public/couverture?ville=Abidjan", headers=PUB)
    assert r.status_code == 200
    for item in r.json():
        assert item["ville"] == "Abidjan"


async def test_datacenters(client_org):
    r = await client_org.get("/v1/public/datacenters", headers=PUB)
    assert r.status_code == 200
    codes = {d["code"] for d in r.json()}
    assert "ABJ-01" in codes and "GBM-01" in codes


async def test_disponibilite_domaine(client_org):
    r = await client_org.get("/v1/public/disponibilite-domaine?nom=synelia.ci", headers=PUB)
    assert r.status_code == 200
    assert r.json()["disponible"] is False
    r = await client_org.get("/v1/public/disponibilite-domaine?nom=maboite.ci", headers=PUB)
    assert r.status_code == 200
    assert r.json()["disponible"] is True


async def test_etudes_cas(client_org):
    r = await client_org.get("/v1/public/etudes-cas", headers=PUB)
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] == 3


async def test_offres(client_org):
    r = await client_org.get("/v1/public/offres", headers=PUB)
    assert r.status_code == 200
    assert r.json()["pagination"]["total"] >= 1


async def test_offres_slug(client_org):
    r = await client_org.get("/v1/public/offres/espace-standard", headers=PUB)
    assert r.status_code == 200, r.text
    assert r.json()["categorie"] == "espace_cloud"
    r = await client_org.get("/v1/public/offres/inconnu", headers=PUB)
    assert r.status_code == 404


async def test_pages_legales(client_org):
    r = await client_org.get("/v1/public/pages-legales", headers=PUB)
    assert r.status_code == 200
    slugs = {p["slug"] for p in r.json()}
    assert {"cgu", "confidentialite", "mentions-legales", "sla"} <= slugs


async def test_page_legale_slug(client_org):
    r = await client_org.get("/v1/public/pages-legales/cgu", headers=PUB)
    assert r.status_code == 200
    assert r.json()["slug"] == "cgu"
    r = await client_org.get("/v1/public/pages-legales/inconnu", headers=PUB)
    assert r.status_code == 404


async def test_simulateur(client_org):
    r = await client_org.post(
        "/v1/public/simulateur",
        headers=PUB,
        json={"vcpu": 2, "ramGo": 4, "stockageGo": 80},
    )
    assert r.status_code == 200, r.text
    assert r.json()["devise"] == "XOF"
    assert r.json()["totalMensuel"] > 0


async def test_sla(client_org):
    r = await client_org.get("/v1/public/sla", headers=PUB)
    assert r.status_code == 200
    assert len(r.json()) >= 1


async def test_souverainete(client_org):
    r = await client_org.get("/v1/public/souverainete", headers=PUB)
    assert r.status_code == 200
    assert "Côte d'Ivoire" in r.json()["juridiction"]


async def test_statut(client_org):
    r = await client_org.get("/v1/public/statut", headers=PUB)
    assert r.status_code == 200
    assert len(r.json()["services"]) >= 1


async def test_incident_public(client_org):
    r = await client_org.get("/v1/public/statut/incidents/inconnu", headers=PUB)
    assert r.status_code == 404


async def test_tarifs(client_org):
    r = await client_org.get("/v1/public/tarifs", headers=PUB)
    assert r.status_code == 200
    assert len(r.json()["familles"]) >= 1
    assert "vcpu_heure" in r.json()["tarifsUnitaires"]
