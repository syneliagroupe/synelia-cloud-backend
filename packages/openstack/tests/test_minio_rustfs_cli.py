"""Adaptation CLI RustFS (`rc` symlinké en `mc`) pour IAM et policies anonymes."""

from __future__ import annotations

from synelia_openstack.minio import MinioReel


def test_adapter_anonymous_set_vers_rc(monkeypatch):
    m = MinioReel.__new__(MinioReel)
    m._cli_rustfs = True
    assert m._adapter_args_mc(("anonymous", "set", "none", "syn/bkt")) == [
        "bucket",
        "anonymous",
        "set",
        "private",
        "syn/bkt",
    ]
    assert m._adapter_args_mc(("anonymous", "set", "download", "syn/bkt")) == [
        "bucket",
        "anonymous",
        "set",
        "download",
        "syn/bkt",
    ]


def test_adapter_passe_minio_mc_inchange(monkeypatch):
    m = MinioReel.__new__(MinioReel)
    m._cli_rustfs = False
    args = ("admin", "user", "add", "syn", "k", "s")
    assert m._adapter_args_mc(args) == list(args)
