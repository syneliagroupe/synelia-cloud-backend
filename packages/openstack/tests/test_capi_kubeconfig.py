"""Repli kubeconfig CAPI quand Magnum /certificates échoue."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from synelia_openstack.k8s_workload import K8sWorkloadReel


class HttpException(Exception):
    pass


def test_construire_kubeconfig_repli_capí(monkeypatch):
    kube = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "x", "cluster": {"server": "https://k8s:6443"}}],
    }

    cim = SimpleNamespace(
        get_cluster=lambda cluster_id: SimpleNamespace(
            api_address="https://k8s:6443", stack_id="kube-abc"
        ),
        get_cluster_certificate=lambda cluster_id: (_ for _ in ()).throw(
            HttpException("No ClusterCertificate found")
        ),
    )
    conn = SimpleNamespace(container_infrastructure_management=cim)
    monkeypatch.setattr("synelia_openstack.fabrique.connexion_magnum", lambda: conn)
    monkeypatch.setattr(
        "synelia_openstack.capi_kubeconfig.kubeconfig_depuis_capí",
        lambda stack_id: kube if stack_id == "kube-abc" else (_ for _ in ()).throw(RuntimeError()),
    )
    monkeypatch.setattr("synelia_openstack.k8s_workload.time.sleep", lambda _: None)

    assert K8sWorkloadReel()._construire_kubeconfig("cluster-1") == kube  # noqa: SLF001
