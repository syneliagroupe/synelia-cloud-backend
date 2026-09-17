"""Configuration : chaque variable typée et validée au démarrage (pydantic-settings)."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _url_base_par_defaut() -> str:
    # Sur Vercel seul /tmp est inscriptible : la base SQLite y vit, éphémère par instance.
    if os.environ.get("VERCEL"):
        return "sqlite+aiosqlite:////tmp/synelia.sqlite3"
    return "sqlite+aiosqlite:///./synelia.sqlite3"


class Reglages(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SYNELIA_", env_file=".env", extra="ignore")

    env: Literal["local", "test", "preview", "production"] = "local"
    nom: str = "Synelia Cloud — API"
    version: str = "1.0.0"
    prefixe_api: str = "/v1"
    docs_actives: bool = True
    url_publique: str = "http://localhost:4000"
    url_frontend: str = "http://localhost:3000"
    cors_origines: list[str] = Field(default_factory=lambda: ["*"])

    database_url: str = Field(default_factory=_url_base_par_defaut)
    echo_sql: bool = False
    rls_active: bool = True

    # Jetons : EdDSA (Ed25519). Sans clé fournie, une clé est dérivée du secret (dev) ;
    # en production, SYNELIA_JWT_CLE_PRIVEE (PEM) est attendue.
    secret: str = "changez-moi-en-production"
    jwt_cle_privee: str | None = None
    jwt_emetteur: str = "https://api.synelia.cloud"
    acces_duree_s: int = 900
    rafraichissement_duree_s: int = 30 * 24 * 3600
    emprunt_duree_s: int = 1800

    cle_maitre: str | None = None  # base64 32 octets ; dérivée de `secret` sinon

    # Orchestration et amont
    temporal_adresse: str | None = None
    temporal_espace: str = "default"
    valkey_url: str | None = None
    fournisseur: Literal["simule", "openstack"] = "simule"
    os_cloud: str | None = None
    os_auth_url: str | None = None
    os_application_credential_id: str | None = None
    os_application_credential_secret: str | None = None
    os_region: str = "RegionOne"
    os_endpoint_overrides: dict[str, str] = Field(default_factory=dict)
    simulation_duree_etape_ms: int = 0

    # Zone VPS partagée (web_hebergement) : un unique Espace Cloud « plateforme » (org_id
    # NULL, jamais visible depuis /espaces côté client), réseau + load balancer Octavia
    # partagés par toutes les VM d'hébergement (routage L7 par Host()) — voir
    # `espaces.service.semer_zone_vps`/`depot_plateforme` et
    # `web_hebergement.service.zone_vps_secrets`. `vps_zone_org_id` ne sert plus qu'à sceller
    # l'organisation admin le temps d'un provisioning initial (repli sans bootstrap manuel,
    # cf. `semer_zone_vps`) ; une fois la ligne créée elle devient org-less, cette variable
    # n'est alors plus consultée.
    vps_zone_espace_id: str | None = None
    vps_zone_org_id: str | None = None

    # Audit : ancrage quotidien hors-rôle (SYNELIA_AUDIT_ANCRAGE_EMAIL), cf. `synelia.audit.ancrer`
    # et §3 de docs/PLAN-ARCHITECTURE-SUITE.md. Optionnelle : sans elle, seul le journal
    # structuré (`audit.ancrage`, logs Docker) sert d'ancrage — pas d'adresse inventée ici.
    audit_ancrage_email: str | None = None

    # Amorçage
    seed_admin_email: str | None = "admin@synelia.cloud"
    seed_admin_mot_de_passe: str | None = "Synelia!2026"
    seed_organisation: str = "Synelia (démo)"
    seed_demo: bool = True

    @property
    def est_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def est_postgres(self) -> bool:
        return "postgres" in self.database_url


@lru_cache
def reglages() -> Reglages:
    return Reglages()
