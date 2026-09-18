"""Espace Cloud « plateforme » de la zone VPS partagée : amorçage reproductible et
visibilité admin-only (org_id NULL).

Env dédié (`SYNELIA_VPS_ZONE_ESPACE_ID`/`SYNELIA_VPS_ZONE_ORG_ID`) posé avant la création de
l'application pour que `synelia.amorcage.amorcer()` déclenche `espaces.service.semer_zone_vps`
en mode « provisioning depuis zéro » (ligne absente sur la base SQLite éphémère du test),
exactement le chemin de reprise après sinistre décrit dans la docstring de
`_provisionner_zone_vps` — jamais emprunté sur le lab réel (la ligne y existe déjà), mais
couvert ici en fournisseur simulé."""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator

import httpx
import pytest

DES = "/v1"
ESPACE_ID_TEST = "test-espace-vps-zone"
ORG_ID_TEST = "test-org-vps-zone"


@pytest.fixture
async def client_zone_vps() -> AsyncIterator[httpx.AsyncClient]:
    import asyncio

    from synelia_kernel import config
    from synelia_testing import ClientApi, configurer_env

    d = configurer_env()
    os.environ["SYNELIA_VPS_ZONE_ESPACE_ID"] = ESPACE_ID_TEST
    os.environ["SYNELIA_VPS_ZONE_ORG_ID"] = ORG_ID_TEST
    config.reglages.cache_clear()
    from synelia_db import session as db

    from synelia import amorcage

    amorcage._AMORCE = False
    await db.fermer()
    from synelia.app import creer_app

    try:
        app = creer_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with ClientApi(transport=transport, base_url="http://test") as c:
                c.transport = transport  # pour ouvrir un second client (organisation neuve)
                await c.connecter()
                yield c
        await db.fermer()
    finally:
        # Nettoyage du réseau Neutron réel si on tourne contre OpenStack réel
        # (pas de fuite en test, qui évite sinon la création cumulée de milliers
        # de réseaux orphelins « vps-zone-net » jamais supprimés).
        try:
            async with db.fabrique()() as session:
                from synelia_db.modeles import Ressource
                from synelia_kernel.chiffrement import dechiffrer
                from synelia_openstack import fournisseur
                from synelia_openstack.identite import IdentiteOpenStack, IdentiteSimule

                ligne = await session.get(Ressource, ESPACE_ID_TEST)
                if ligne is not None and ligne.type == "espace" and ligne.secrets:
                    # `Ressource.secrets` est chiffré en base (AES-256-GCM) — jamais lisible
                    # en clair directement, il faut déchiffrer chaque valeur individuellement.
                    secrets_clairs = {k: dechiffrer(v) for k, v in ligne.secrets.items()}
                    reseau_id = secrets_clairs.get("reseau_id")
                    routeur_id = secrets_clairs.get("routeur_id")
                    projet_id = secrets_clairs.get("projet_id")
                    amont = fournisseur(IdentiteSimule, IdentiteOpenStack)
                    if isinstance(amont, IdentiteOpenStack):
                        # Suppression réelle : on tourne contre OpenStack
                        if reseau_id and routeur_id:
                            await asyncio.to_thread(amont.supprimer_reseau, reseau_id, routeur_id)
                        if projet_id:
                            await asyncio.to_thread(amont.supprimer_projet, projet_id)
        except Exception:
            # L'échec du nettoyage ne doit pas faire échouer le test
            pass
        os.environ.pop("SYNELIA_VPS_ZONE_ESPACE_ID", None)
        os.environ.pop("SYNELIA_VPS_ZONE_ORG_ID", None)
        config.reglages.cache_clear()
        shutil.rmtree(d, ignore_errors=True)


async def test_zone_vps_provisionnee_et_masquee(client_zone_vps):
    client = client_zone_vps

    # 1) Amorçage : la ligne a été provisionnée pour de vrai (exécuteur `espace.create` réel,
    #    fournisseur simulé) et bascule en `org_id NULL` — invisible depuis /espaces, même
    #    pour un contexte actif sur l'organisation qui l'a portée le temps du provisioning.
    r = await client.get(f"{DES}/espaces")
    assert r.status_code == 200
    assert all(e["code"] != "vps-zone" for e in r.json()["donnees"])

    r = await client.get(f"{DES}/espaces", headers={"X-Organisation-Id": ORG_ID_TEST})
    assert r.status_code == 200, r.text
    assert all(e["code"] != "vps-zone" for e in r.json()["donnees"])

    # 2) Toujours invisible d'une organisation cliente fraîchement inscrite
    # (inscription + vérification d'email : le helper rejoue ce flux).
    from synelia_testing import inscrire_et_verifier

    corps = await inscrire_et_verifier(
        client,
        "nouvel-org@example.com",
        "Nouvel utilisateur",
        "MotDePasse!2026",
        organisation={"nom": "Nouvelle Org Zone VPS", "pays": "CI"},
    )
    nouvel_org = httpx.AsyncClient(transport=client.transport, base_url="http://test")
    try:
        nouvel_org.headers["Authorization"] = f"Bearer {corps['accessToken']}"
        r2 = await nouvel_org.get(f"{DES}/espaces")
        assert r2.status_code == 200
        assert all(e["code"] != "vps-zone" for e in r2.json()["donnees"])
        r2 = await nouvel_org.get(f"{DES}/tableau-de-bord")
        assert r2.status_code in (200, 404)
        if r2.status_code == 200:
            assert r2.json().get("espaces", 0) == 0
    finally:
        await nouvel_org.aclose()

    # 3) Visible côté admin, nulle part ailleurs.
    r = await client.get(f"{DES}/admin/espaces")
    assert r.status_code == 200, r.text
    admin_espaces = r.json()
    assert any(e["code"] == "vps-zone" and e["id"] == ESPACE_ID_TEST for e in admin_espaces)

    # 4) La lecture des secrets (consommée par web_hebergement/projets) fonctionne toujours de
    #    bout en bout via le dépôt « plateforme », sans org override. Le domaine doit
    #    désormais être enregistré au préalable (plus de nom provisoire implicite).
    from synelia_testing import enregistrer_domaine

    await enregistrer_domaine(client, "zone-vps.test")
    r = await client.post(
        f"{DES}/web/hebergements", json={"palier": "pro", "site": "ABJ", "domaine": "zone-vps.test"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["statut"] == "done"
    r = await client.get(f"{DES}/web/hebergements")
    assert r.status_code == 200
    elements = [h for h in r.json()["donnees"] if h.get("domaine") == "zone-vps.test"]
    assert len(elements) == 1 and elements[0]["statut"] == "en_ligne"


async def test_zone_vps_seed_idempotent(client_zone_vps):
    """Deuxième amorçage (redémarrage) sur la même base : aucune ré-écriture, aucune erreur."""
    from sqlalchemy import select
    from synelia_db.modeles import Ressource
    from synelia_db.session import fabrique

    from synelia.modules.espaces.service import semer_zone_vps

    async with fabrique()() as s:
        avant = (
            await s.execute(select(Ressource).where(Ressource.id == ESPACE_ID_TEST))
        ).scalar_one()
        assert avant.org_id is None
        await semer_zone_vps(s)
        await s.commit()
        apres = (
            await s.execute(select(Ressource).where(Ressource.id == ESPACE_ID_TEST))
        ).scalar_one()
        assert apres.org_id is None
        assert apres.donnees["code"] == "vps-zone"
