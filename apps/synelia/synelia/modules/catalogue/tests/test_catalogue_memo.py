"""Le cache catalogue ne doit pas confondre « jamais calculé » avec un timestamp 0.0."""

import importlib


def test_memo_appelle_la_fabrique_meme_si_l_uptime_est_inferieur_a_10_min(monkeypatch):
    cat = importlib.import_module("synelia.modules.catalogue.router")
    cat._cache.clear()
    monkeypatch.setattr(cat.time, "monotonic", lambda: 42.0)
    assert cat._memo("essai", lambda: ["ok"]) == ["ok"]
    assert cat._memo("essai", lambda: ["ne-doit-pas-recalculer"]) == ["ok"]
