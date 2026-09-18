"""Protège `ComputeOpenStack._deja_dans_etat_cible` / `action()` : bug réel trouvé en démo
(2026-09-08) — `vm.power.stop` remontait un 409 brut « Cannot 'stop' instance ... while it is in
vm_state stopped » quand la base croyait la VM `running` mais Nova l'avait déjà `SHUTOFF`
(dérive DB/Nova, ex. coupure lab avec redémarrage des invités). Un `stop`/`start` déjà atteint
côté Nova doit être un succès sincère (DB resynchronisée), pas une erreur — mais un vrai conflit
(état différent, ou 409 sur un `redemarrage`, sans équivalent « déjà atteint ») doit continuer de
remonter tel quel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from synelia_openstack.compute import ComputeOpenStack
from synelia_testing import connexion_lab, sur_lab_reel


class ConflictException(Exception):
    """Doublure minimale : le code de production identifie le conflit Nova par
    `type(exc).__name__ == "ConflictException"` (même motif que `synelia_openstack.erreurs`),
    jamais par `isinstance` — inutile de dépendre de la vraie classe `openstack.exceptions.*`
    pour ce test."""


class AutreException(Exception):
    pass


def _connexion(statut_reel: str):
    return SimpleNamespace(
        compute=SimpleNamespace(get_server=lambda serveur_id: SimpleNamespace(status=statut_reel))
    )


def test_arret_deja_shutoff_est_un_succes():
    compute = ComputeOpenStack()
    exc = ConflictException("Cannot 'stop' instance abc while it is in vm_state stopped")
    assert compute._deja_dans_etat_cible(exc, "arret", _connexion("SHUTOFF"), "abc") is True


def test_demarrage_deja_active_est_un_succes():
    compute = ComputeOpenStack()
    exc = ConflictException("Cannot 'start' instance abc while it is in vm_state active")
    assert compute._deja_dans_etat_cible(exc, "demarrage", _connexion("ACTIVE"), "abc") is True


def test_conflit_dans_un_autre_etat_reste_une_erreur():
    # Le message ne mentionne pas l'état visé par CETTE action (ex. verrouillé/task_state en
    # cours) : un vrai conflit, ne pas absorber.
    compute = ComputeOpenStack()
    exc = ConflictException("Cannot 'stop' instance abc while it is in vm_state building")
    assert compute._deja_dans_etat_cible(exc, "arret", _connexion("BUILDING"), "abc") is False


def test_message_correspond_mais_relecture_nova_contredit():
    # Le message dit « stopped » mais un GET frais montre autre chose (état changé entre-temps,
    # ou format de message trompeur) : pas de succès affirmé sans confirmation réelle.
    compute = ComputeOpenStack()
    exc = ConflictException("Cannot 'stop' instance abc while it is in vm_state stopped")
    assert compute._deja_dans_etat_cible(exc, "arret", _connexion("ACTIVE"), "abc") is False


def test_redemarrage_n_a_pas_d_etat_deja_atteint():
    # Pas d'entrée pour `redemarrage` dans `_VM_STATE_CIBLE` : un 409 dessus est toujours un
    # vrai conflit (aucun « état déjà redémarré » n'existe côté Nova).
    compute = ComputeOpenStack()
    exc = ConflictException("Cannot 'reboot' instance abc while it is in vm_state active")
    assert compute._deja_dans_etat_cible(exc, "redemarrage", _connexion("ACTIVE"), "abc") is False


def test_autre_type_d_exception_nest_jamais_absorbe():
    compute = ComputeOpenStack()
    exc = AutreException("Cannot 'stop' instance abc while it is in vm_state stopped")
    assert compute._deja_dans_etat_cible(exc, "arret", _connexion("SHUTOFF"), "abc") is False


def test_lab_nova_glance_reels():
    """Impact OpenStack réel : les gabarits viennent des flavors Nova du lab (publics
    seulement, pas l'amphora Octavia privée), les images de Glance (sans l'appliance
    `amphora` taguée). Sans lab joignable, saut honnête plutôt que faux-positif."""
    if not sur_lab_reel():
        pytest.skip("réservé au lab réel (SYNELIA_FOURNISSEUR=openstack)")
    if connexion_lab() is None:
        pytest.skip("lab réel injoignable")
    compute = ComputeOpenStack()
    gabarits = compute.gabarits()
    assert gabarits, "Nova ne renvoie aucun flavor public"
    assert all(g["vcpu"] >= 1 and g["ramGo"] >= 1 for g in gabarits)
    images = compute.images()
    assert images, "Glance ne renvoie aucune image publique"
    assert not any("amphora" in str(i.get("nom", "")).lower() for i in images), (
        "l'image amphora Octavia fuit dans le catalogue"
    )
    capacite = compute.capacite_plateforme()
    assert capacite is not None and capacite["hosts"] >= 1, (
        "capacité hyperviseurs illisible sur lab réel"
    )
