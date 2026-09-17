from __future__ import annotations

from functools import lru_cache
from typing import Any

from synelia_kernel.config import reglages

_instances: dict[type, Any] = {}

# Sans timeout explicite, keystoneauth1/requests attend indéfiniment une réponse : une seule
# connexion TCP bloquée (constaté en direct — un `stop_server` resté accroché a gelé tout le
# process, toutes les requêtes HTTP confondues, jusqu'au redémarrage manuel du conteneur) peut
# alors figer tout le processus (un seul worker, les appels amont sont synchrones). Chaque appel
# HTTP individuel vers l'amont (Nova/Neutron/Cinder/Keystone) est déjà bref : `wait_for_server` et
# consorts répètent de courtes requêtes en boucle, pas une seule requête tenue ouverte longtemps.
_TIMEOUT_API_S = 45


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
        conn = openstack.connect(
            cloud=r.os_cloud, region_name=region or r.os_region, api_timeout=_TIMEOUT_API_S
        )
    else:
        conn = openstack.connect(
            load_yaml_config=False,
            load_envvars=False,  # jamais les OS_* du shell : une credential ne doit pas recevoir de scope
            auth_type="v3applicationcredential",
            auth_url=r.os_auth_url,
            application_credential_id=r.os_application_credential_id,
            application_credential_secret=r.os_application_credential_secret,
            region_name=region or r.os_region,
            api_timeout=_TIMEOUT_API_S,
        )
    for service, url in r.os_endpoint_overrides.items():
        conn.config.config[f"{service}_endpoint_override"] = url
    return conn


def connexion_avec(application_credential_id: str, secret: str, region: str | None = None) -> Any:
    """Connexion scellée au projet d'un Espace Cloud, par son *application credential*."""
    import openstack  # type: ignore[import-not-found]

    r = reglages()
    conn = openstack.connect(
        load_yaml_config=False,
        load_envvars=False,
        auth_type="v3applicationcredential",
        auth_url=r.os_auth_url,
        application_credential_id=application_credential_id,
        application_credential_secret=secret,
        region_name=region or r.os_region,
        api_timeout=_TIMEOUT_API_S,
    )
    for service, url in r.os_endpoint_overrides.items():
        conn.config.config[f"{service}_endpoint_override"] = url
    return conn
