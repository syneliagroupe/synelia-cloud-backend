"""Le cache catalogue ne doit pas confondre « jamais calculé » avec un timestamp 0.0."""

import importlib


def test_memo_appelle_la_fabrique_meme_si_l_uptime_est_inferieur_a_10_min(monkeypatch):
    cat = importlib.import_module("synelia.modules.catalogue.router")
    cat._cache.clear()
    monkeypatch.setattr(cat.time, "monotonic", lambda: 42.0)
    assert cat._memo("essai", lambda: ["ok"]) == ["ok"]
    assert cat._memo("essai", lambda: ["ne-doit-pas-recalculer"]) == ["ok"]


async def test_catalogue_gabarits_images_reels_sur_lab(client):
    """Sur lab réel, le catalogue servi par l'API doit refléter Nova/Glance, pas les
    constantes simulées `GABARITS`/`IMAGES` : les ids sont alors des UUID Nova/Glance,
    jamais `s1.small`/`ubuntu-24.04`. En simulé, les constantes sont attendues."""
    from synelia_testing import sur_lab_reel

    r = await client.get("/v1/catalogue/gabarits")
    assert r.status_code == 200
    gabarits = r.json()
    assert gabarits, "catalogue gabarits vide"
    r = await client.get("/v1/catalogue/images")
    assert r.status_code == 200
    images = r.json()
    assert images, "catalogue images vide"
    if sur_lab_reel():
        ids_simules = {"s1.small", "g1.medium", "g1.large", "g1.xlarge", "c1.large", "m1.large"}
        assert not (ids_simules & {g["id"] for g in gabarits}), (
            "lab réel mais catalogue simulé : test en fumée"
        )
        assert not any("amphora" in i["nom"].lower() for i in images)
