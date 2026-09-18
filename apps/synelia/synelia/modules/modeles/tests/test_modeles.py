import pytest

pytestmark = pytest.mark.anyio


async def test_liste_modeles_seedes(client_org):
    r = await client_org.get("/v1/modeles")
    assert r.status_code == 200
    corps = r.json()
    assert corps["pagination"]["total"] == 8
    slugs = {d["slug"] for d in corps["donnees"]}
    assert {
        "wordpress",
        "nextjs",
        "django",
        "laravel",
        "odoo",
        "n8n",
        "metabase",
        "gitlab",
    } <= slugs


async def test_liste_modeles_stable(client_org):
    await client_org.get("/v1/modeles")
    r = await client_org.get("/v1/modeles")
    assert r.json()["pagination"]["total"] == 8


async def test_filtres_modeles(client_org):
    r = await client_org.get("/v1/modeles", params={"categorie": "web"})
    assert r.status_code == 200
    assert all(d["categorie"] == "web" for d in r.json()["donnees"])
    r = await client_org.get("/v1/modeles", params={"populaire": "true"})
    assert r.status_code == 200
    assert r.json()["donnees"]


async def test_obtenir_modele(client_org):
    r = await client_org.get("/v1/modeles/wordpress")
    assert r.status_code == 200
    assert r.json()["slug"] == "wordpress" and r.json()["solution"] == "WordPress"


async def test_obtenir_modele_inconnu_404(client_org):
    r = await client_org.get("/v1/modeles/inexistant")
    assert r.status_code == 404
    assert r.json()["erreur"]["code"] == "introuvable"


async def test_estimation(client_org):
    r = await client_org.post("/v1/modeles/wordpress/estimation", json={})
    assert r.status_code == 200
    corps = r.json()
    assert corps["devise"] == "XOF"
    assert isinstance(corps["totalMensuel"], int) and corps["totalMensuel"] > 0
    assert corps["lignes"] and all(isinstance(ligne["total"], int) for ligne in corps["lignes"])


async def test_estimation_avec_ressources(client_org):
    r = await client_org.post(
        "/v1/modeles/odoo/estimation",
        json={"ressources": {"cpu": 4, "ramMo": 8192, "diskGo": 100}, "sieges": 10},
    )
    assert r.status_code == 200
    corps = r.json()
    assert corps["totalMensuel"] > 0
    assert any(ligne["libelle"] == "Sièges" for ligne in corps["lignes"])
