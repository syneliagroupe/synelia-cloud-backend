"""Recherche globale : le lien renvoyé doit être une route réelle du frontend.

Régression du bug où `/recherche` pluralisait naïvement le type
(`k8s_cluster` → `/app/k8s_clusterss/…`) au lieu de la vraie route.
"""

from synelia.modules.transverses.router import _href_resultat


class _Faux:
    def __init__(self, type: str, id: str, parent_id: str | None = None) -> None:
        self.type = type
        self.id = id
        self.parent_id = parent_id


def test_href_resultat_types_connus():
    assert _href_resultat(_Faux("vm", "v1")) == "/app/vms/v1"
    assert _href_resultat(_Faux("k8s_cluster", "c1")) == "/app/kubernetes/c1"
    assert _href_resultat(_Faux("projet_service", "s1", parent_id="p1")) == (
        "/app/applications/projets/p1/s1"
    )
    # Type inconnu : la pluralisation naïve reste le filet de sécurité.
    assert _href_resultat(_Faux("truc_jamais_vu", "x1")) == "/app/truc_jamais_vus/x1"
