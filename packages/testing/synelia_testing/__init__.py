"""Outils de test : application sur SQLite éphémère, client authentifié, aides."""

from __future__ import annotations

import os
import shutil
import tempfile
import warnings
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx
import pytest

ADMIN_EMAIL = "admin@synelia.cloud"
ADMIN_MDP = "Synelia!2026"

# ── Sondes lab réel + aides de saut ──────────────────────────────────────────
# La suite pytest tourne contre le VRAI lab OpenStack par défaut
# (`SYNELIA_FOURNISSEUR=openstack`) : quand une condition connue du lab rend un
# test voué à l'échec côté amont (pas côté applicatif), le test est SAUTÉ avec
# une raison honnête plutôt que de FAIL. En simulé (`SYNELIA_FOURNISSEUR=simule`)
# ces aides sont sans effet : la couverture y reste complète.

RAISON_OCTAVIA_CASSE = (
    "Octavia cassé lab-wide (LB jamais ACTIVE, statut ERROR — TODO.md ligne 31) : "
    "test désélectionné sur lab réel (TODO.md ligne 47)"
)

RAISON_FIP_EPUISE = (
    "Pool d'IP flottantes external-net épuisé sur le lab réel (0 IP libre visible, "
    "Neutron 409 « No more IP addresses available » — TODO.md ligne 22) : "
    "test désélectionné sur lab réel"
)

RAISON_VM_DEMO_ABSENTE = (
    "Aucun serveur Nova réel nommé vm-demo-web sur le lab "
    "(attachement volume → Nova 404) : test désélectionné sur lab réel"
)

VM_DEMO = "vm-demo-web"


def _sur_lab_reel() -> bool:
    from synelia_openstack.fabrique import mode

    return mode() == "openstack"


def sur_lab_reel() -> bool:
    """Alias public de la sonde de mode : `True` quand la suite tourne contre le
    vrai lab OpenStack (`SYNELIA_FOURNISSEUR=openstack`)."""
    return _sur_lab_reel()


def exiger_lab_reel() -> None:
    """Saute le test quand il ne tourne pas contre le vrai lab : les assertions
    d'impact amont (Nova/Neutron/Cinder/…) n'ont de sens que sur l'infra réelle."""
    import pytest

    if not _sur_lab_reel():
        pytest.skip("réservé au lab réel (SYNELIA_FOURNISSEUR=openstack)")


def corriger_amont(
    monkeypatch, module_service, nom_simule: str, nom_reel: str, attr: str, valeur
) -> None:
    """Patch `attr` sur les DEUX classes d'amont (simulée ET réelle) d'un module.

    Patatcher seulement `XxxSimule` est un faux-positif sur lab réel : `amont()`
    y renvoie `XxxOpenStack` via `fournisseur()`, donc le patch ne touche jamais
    le code exécuté et le test passe « en fumée ». En mode simulé la classe réelle
    existe toujours (importée pour le dispatch) : la patcher aussi est sans effet.
    """
    for nom_classe in (nom_simule, nom_reel):
        cls = getattr(module_service, nom_classe, None)
        if cls is not None:
            monkeypatch.setattr(cls, attr, valeur)


def connexion_lab():
    """Connexion admin au lab réel, ou `None` si indisponible (mode simulé ou
    lab injoignable) — ne lève jamais : l'appelant saute ou dégrade."""
    if not _sur_lab_reel():
        return None
    try:
        from synelia_openstack.fabrique import connexion

        return connexion()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _sonde_fip_libres() -> int | None:
    """Nombre d'IP flottantes libres (non associées) visibles sur le réseau externe
    du lab — `None` si le réseau externe est introuvable (état inconnu, pas « 0 »).
    Lecture seule, mise en cache pour la session."""
    from synelia_openstack.fabrique import connexion

    c = connexion()
    ext = next((n for n in c.network.networks(is_router_external=True)), None)
    if ext is None:
        return None
    return sum(1 for ip in c.network.ips(floating_network_id=ext.id) if not ip.port_id)


@lru_cache(maxsize=1)
def _sonde_vm_demo_presente() -> bool:
    """`True` si un vrai serveur Nova nommé `vm-demo-web` existe sur le lab.
    Lecture seule, mise en cache pour la session."""
    from synelia_openstack.fabrique import connexion

    c = connexion()
    return c.compute.find_server(VM_DEMO, ignore_missing=True) is not None


def ignorer_si_octavia_casse() -> None:
    """Saute le test sur lab réel tant qu'Octavia est cassé ; sans effet en simulé."""
    if _sur_lab_reel():
        pytest.skip(RAISON_OCTAVIA_CASSE)


def ignorer_si_fip_epuise() -> None:
    """Saute les tests qui allouent une IP flottante quand le pool est épuisé sur
    lab réel ; sans effet en simulé. Une sonde en échec ne fait jamais échouer la
    suite : on laisse alors le test parler (pas de saut)."""
    if not _sur_lab_reel():
        return
    try:
        libres = _sonde_fip_libres()
    except Exception as exc:  # noqa: BLE001 — sonde best effort, le test tranche
        warnings.warn(
            f"sonde FIP lab indisponible ({exc!r}) : test exécuté quand même", stacklevel=2
        )
        return
    if libres is not None and libres <= 0:
        pytest.skip(RAISON_FIP_EPUISE)


def ignorer_si_vm_demo_absente() -> None:
    """Saute les tests qui attachent à la VM de démo quand Nova ne la connaît pas
    sur lab réel ; sans effet en simulé. Même politique qu'au-dessus : une sonde
    en échec ne saute jamais, le test tranche."""
    if not _sur_lab_reel():
        return
    try:
        presente = _sonde_vm_demo_presente()
    except Exception as exc:  # noqa: BLE001 — sonde best effort, le test tranche
        warnings.warn(
            f"sonde Nova lab indisponible ({exc!r}) : test exécuté quand même", stacklevel=2
        )
        return
    if not presente:
        pytest.skip(RAISON_VM_DEMO_ABSENTE)


def configurer_env() -> str:
    d = tempfile.mkdtemp(prefix="synelia-test-")
    os.environ["SYNELIA_ENV"] = "test"
    os.environ["SYNELIA_DATABASE_URL"] = f"sqlite+aiosqlite:///{d}/test.sqlite3"
    os.environ["SYNELIA_SEED_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["SYNELIA_SEED_ADMIN_MOT_DE_PASSE"] = ADMIN_MDP
    os.environ["SYNELIA_TRAVAUX_EN_LIGNE"] = "1"
    # Conteneur api lab a Temporal + worker : sans ceci `demarrer_travail` préfère Temporal
    # (statut `queued`) et le worker lit Postgres, pas le SQLite éphémère du test → faux échecs.
    os.environ.pop("SYNELIA_TEMPORAL_ADRESSE", None)
    os.environ.pop("SYNELIA_TRAVAUX_WORKER", None)
    os.environ.setdefault("SYNELIA_SEED_DEMO", "true")
    return d


class ClientApi(httpx.AsyncClient):
    """Client ASGI avec `Authorization` posé une fois ; `.v1("/vms")` préfixe les chemins."""

    jeton: str | None = None
    org_id: str | None = None

    def v1(self, chemin: str) -> str:
        return f"/v1{chemin}"

    async def connecter(
        self, email: str = ADMIN_EMAIL, mot_de_passe: str = ADMIN_MDP
    ) -> dict[str, Any]:
        r = await self.post("/v1/auth/connexion", json={"email": email, "motDePasse": mot_de_passe})
        assert r.status_code == 200, r.text
        corps = r.json()
        self.jeton = corps["accessToken"]
        self.org_id = corps.get("organisationActive")
        self.headers["Authorization"] = f"Bearer {self.jeton}"
        return corps


CODE_VERIFICATION_TEST = "123456"


async def forcer_code_verification(email: str, code: str = CODE_VERIFICATION_TEST) -> None:
    """Pose un code de vérification connu pour `email` (tests uniquement) : l'inscription
    réelle émet un code aléatoire envoyé par email, que les tests API ne peuvent pas lire.
    Le code injecté consomme les précédents, comme `_emettre_code` côté routeur."""
    from sqlalchemy import select
    from synelia.securite import hacher_jeton
    from synelia_db import session as db
    from synelia_db.modeles import Utilisateur, VerificationEmail
    from synelia_kernel.dates import dans, maintenant

    async with db.fabrique()() as s:
        u = (
            await s.execute(select(Utilisateur).where(Utilisateur.email == email.lower()))
        ).scalar_one()
        precedents = (
            await s.execute(
                select(VerificationEmail).where(
                    VerificationEmail.utilisateur_id == u.id,
                    VerificationEmail.consommee_le.is_(None),
                )
            )
        ).scalars()
        for p in precedents:
            p.consommee_le = maintenant()
        s.add(
            VerificationEmail(
                utilisateur_id=u.id, code_hash=hacher_jeton(code), expire_le=dans(900)
            )
        )
        await s.commit()


async def inscrire_et_verifier(
    client, email: str, nom: str, mot_de_passe: str, organisation: dict | None = None
) -> dict[str, Any]:
    """Inscription complète côté tests : 202 + code forcé + vérification → session."""
    corps: dict[str, Any] = {
        "email": email,
        "nom": nom,
        "motDePasse": mot_de_passe,
        "accepteConditions": True,
    }
    if organisation is not None:
        corps["organisation"] = organisation
    r = await client.post("/v1/auth/inscription", json=corps)
    assert r.status_code == 202, r.text
    await forcer_code_verification(email)
    r = await client.post(
        "/v1/auth/verification-email",
        json={"email": email, "code": CODE_VERIFICATION_TEST},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[ClientApi]:
    """Application neuve (base SQLite éphémère), admin connecté."""
    d = configurer_env()
    from synelia_kernel import config

    config.reglages.cache_clear()
    from synelia import amorcage
    from synelia_db import session as db

    amorcage._AMORCE = False
    await db.fermer()
    from synelia.app import creer_app

    try:
        app = creer_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with ClientApi(transport=transport, base_url="http://test") as c:
                await c.connecter()
                yield c
        await db.fermer()
    finally:
        shutil.rmtree(d, ignore_errors=True)
