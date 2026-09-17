from __future__ import annotations

from functools import lru_cache
from typing import Any

from synelia_kernel.config import reglages

_instances: dict[type, Any] = {}


def mode() -> str:
    r = reglages()
    if r.fournisseur == "openstack" and (r.os_cloud or r.os_auth_url):
        return "openstack"
    return "simule"


def fournisseur[T](simule: type[T], reel: type[T]) -> T:
    """Choisit l'implémentation selon la configuration ; une instance par classe."""
    cls = reel if mode() == "openstack" else simule
    if cls not in _instances:
        _instances[cls] = cls()
    return _instances[cls]


@lru_cache
def connexion(region: str | None = None) -> Any:
    """`openstack.connect()` admin (bootstrap) — import paresseux : openstacksdk est un extra."""
    import openstack  # type: ignore[import-not-found]

    r = reglages()
    if r.os_cloud:
        conn = openstack.connect(cloud=r.os_cloud, region_name=region or r.os_region)
    else:
        conn = openstack.connect(
            auth_type="v3applicationcredential",
            auth_url=r.os_auth_url,
            application_credential_id=r.os_application_credential_id,
            application_credential_secret=r.os_application_credential_secret,
            region_name=region or r.os_region,
        )
    for service, url in r.os_endpoint_overrides.items():
        conn.config.config[f"{service}_endpoint_override"] = url
    return conn
