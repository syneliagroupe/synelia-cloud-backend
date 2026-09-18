"""Tests de la documentation & formation."""

PUB = {"Authorization": ""}


async def test_bac_a_sable_cycle(client_org):
    r = await client_org.get("/v1/docs/bac-a-sable")
    assert r.status_code == 404

    r = await client_org.post(
        "/v1/docs/bac-a-sable", json={"dureeHeures": 4, "parcoursSlug": "decouverte"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    assert r.json()["type"] == "docs.bac_a_sable"

    r = await client_org.get("/v1/docs/bac-a-sable")
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "actif"

    r = await client_org.delete("/v1/docs/bac-a-sable")
    assert r.status_code == 204

    r = await client_org.get("/v1/docs/bac-a-sable")
    assert r.status_code == 404


async def test_parcours_liste(client_org):
    r = await client_org.get("/v1/docs/parcours")
    assert r.status_code == 200
    assert len(r.json()) == 3


async def test_parcours_obtenir(client_org):
    r = await client_org.get("/v1/docs/parcours/decouverte")
    assert r.status_code == 200, r.text
    assert r.json()["parcours"]["slug"] == "decouverte"
    r = await client_org.get("/v1/docs/parcours/inconnu")
    assert r.status_code == 404


async def test_progression_completion(client_org):
    r = await client_org.post(
        "/v1/docs/parcours/decouverte/modules/mod-premiers-pas/completion", json={"score": 80}
    )
    assert r.status_code == 200, r.text
    assert r.json()["pctComplete"] == 20.0
    assert r.json()["modulesTermines"] == ["mod-premiers-pas"]

    r = await client_org.get("/v1/docs/progression")
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_module_inconnu(client_org):
    r = await client_org.post("/v1/docs/parcours/decouverte/modules/inconnu/completion", json={})
    assert r.status_code == 404


async def test_sections_public(client_org):
    r = await client_org.get("/v1/docs/sections", headers=PUB)
    assert r.status_code == 200
    assert len(r.json()) == 4
