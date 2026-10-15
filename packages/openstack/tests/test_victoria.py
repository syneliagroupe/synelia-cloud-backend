"""Protège `Victoria{Simule,Reel}.lien_logs` : bug réel trouvé en audit du module
observabilité (2026-10-15), vérifié en direct sur dev01 — `GET /observabilite/journaux
?recherche=error%20%26%20disk%231%20100%25` renvoyait `lienVictoriaLogs` avec la recherche
interpolée brute dans l'URL (`?query=error & disk#1 100%`) : `#` tronque l'URL en fragment
(le reste de la recherche n'atteint jamais VictoriaLogs), `&` introduit un faux paramètre
de requête. Le lien de sortie « Ouvrir dans VictoriaLogs » était donc silencieusement cassé
ou incomplet dès que la recherche contenait un caractère réservé d'URL."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from synelia_openstack.victoria import VictoriaReel, VictoriaSimule


def test_lien_logs_echappe_les_caracteres_reserves_url():
    recherche = "error & disk#1 100%"
    for victoria in (VictoriaSimule(), VictoriaReel()):
        lien = victoria.lien_logs(recherche)
        morceaux = urlsplit(lien)
        # Le caractère `#` ne doit pas avoir tronqué l'URL en fragment, et `&` ne doit pas
        # avoir introduit un second paramètre de requête : la recherche complète doit se
        # retrouver intacte dans le paramètre `query` une fois décodée.
        assert morceaux.fragment == ""
        assert parse_qs(morceaux.query)["query"] == [recherche]


def test_lien_logs_sans_recherche_reste_sans_parametre():
    for victoria in (VictoriaSimule(), VictoriaReel()):
        lien = victoria.lien_logs(None)
        assert "?" not in lien
