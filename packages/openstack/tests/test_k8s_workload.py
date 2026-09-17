"""Protège `K8sWorkloadReel._construire_kubeconfig` : bug réel trouvé en audit du module
observabilité (2026-09-14) — quand Magnum répond une erreur sur `GET /certificates/{id}`
(ex. cluster PaaS dont le certificat a disparu côté Barbican, cas déjà documenté dans
DEMO-TODO.md), l'exception `openstack.exceptions.HttpException` brute remontait telle
quelle jusqu'au routeur, qui la laissait passer en `500 erreur_interne` opaque au lieu du
`424 amont_indisponible` attendu partout ailleurs quand une intégration amont ne répond
pas (`synelia_openstack.erreurs.traduire`, déjà utilisé par `compute.py`/`block_storage.py`
pour le même motif)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from synelia_kernel.erreurs import AppError
from synelia_openstack.k8s_workload import K8sWorkloadReel


class HttpException(Exception):
    """Doublure minimale : `synelia_openstack.erreurs.traduire` identifie l'exception par
    `type(exc).__name__`, jamais par `isinstance` — inutile de dépendre de la vraie classe."""


def test_certificat_manquant_devient_un_424_pas_un_500(monkeypatch):
    cim = SimpleNamespace(
        get_cluster=lambda cluster_id: SimpleNamespace(api_address="https://k8s:6443"),
        get_cluster_certificate=lambda cluster_id: (_ for _ in ()).throw(
            HttpException(f"No ClusterCertificate found for {cluster_id}")
        ),
    )
    monkeypatch.setattr(
        "synelia_openstack.fabrique.connexion",
        lambda: SimpleNamespace(container_infrastructure_management=cim),
    )
    monkeypatch.setattr("synelia_openstack.k8s_workload.time.sleep", lambda _: None)

    with pytest.raises(AppError) as exc_info:
        K8sWorkloadReel()._construire_kubeconfig("cluster-1")  # noqa: SLF001

    assert exc_info.value.statut == 424
