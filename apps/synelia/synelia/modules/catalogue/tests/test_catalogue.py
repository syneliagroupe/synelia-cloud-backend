"""Catalogue IaaS : gabarits et images système, avec le cache de 10 minutes."""

import time

from synelia.modules.catalogue.router import _cache, _memo


async def test_lister_gabarits_et_images(client):
    r = await client.get("/v1/catalogue/gabarits")
    assert r.status_code == 200
    gabarits = r.json()
    assert gabarits and all(g["id"] and g["nom"] for g in gabarits)

    r = await client.get("/v1/catalogue/images")
    assert r.status_code == 200
    assert r.json()


def test_memo_interroge_l_amont_sur_une_machine_fraichement_demarree(monkeypatch):
    """Régression : `time.monotonic()` compte depuis le démarrage de la machine. Avec un
    horodatage sentinelle `0.0` pour « jamais lu », un cache vide passait pour frais sur un hôte
    démarré depuis moins de 10 minutes (runner de CI) : le catalogue renvoyait alors la valeur
    vide de la sentinelle sans jamais appeler l'amont."""
    monkeypatch.setattr(time, "monotonic", lambda: 12.0)
    _cache.pop("images-test", None)
    appels: list[int] = []

    def fabrique() -> list[str]:
        appels.append(1)
        return ["ubuntu-24.04"]

    try:
        assert _memo("images-test", fabrique) == ["ubuntu-24.04"]
        assert appels == [1], "l'amont doit être interrogé quand le cache est vide"

        assert _memo("images-test", fabrique) == ["ubuntu-24.04"]
        assert appels == [1], "la seconde lecture doit venir du cache"
    finally:
        _cache.pop("images-test", None)
