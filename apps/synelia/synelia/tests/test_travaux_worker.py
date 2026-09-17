"""Worker Local des travaux (`travaux/local.py`, mode `SYNELIA_TRAVAUX_WORKER=1`, dev01) :
réclamation atomique d'un travail `queued`, exécution avec le principal réel de l'utilisateur
qui l'a demandé (pas un principal générique « worker »), reprise des orphelins au boot."""

from __future__ import annotations

from synelia_db.modeles import Travail
from synelia_db.session import fabrique

from synelia.travaux import local, worker_ctx


async def test_worker_reclame_execute_avec_le_principal_reel(client, monkeypatch):
    monkeypatch.setenv("SYNELIA_TRAVAUX_EN_LIGNE", "0")
    monkeypatch.setenv("SYNELIA_TRAVAUX_WORKER", "1")

    corps = {
        "code": "worker-abj",
        "offerId": "offre-standard",
        "site": "ABJ",
        "cidr": "10.30.0.0/16",
        "quota": {"vcpu": 16, "ramGo": 64, "stockageTo": 2},
    }
    r = await client.post("/v1/espaces", json=corps)
    assert r.status_code == 202, r.text
    travail = r.json()
    assert travail["statut"] == "queued"
    travail_id = travail["id"]

    async with fabrique()() as session:
        # Le travail est `queued`, pas `running` : rien à reprendre au boot.
        assert await local.reprendre_orphelins(session) == 0

        ligne = await session.get(Travail, travail_id)
        # Étape 1.3 : le principal réel (pas un principal générique) est sérialisé dans
        # `contexte`, sans secret, pour que le worker rejoue le travail avec le même acteur
        # qu'un `create_task` en processus API.
        principal = ligne.contexte["principal"]
        assert principal["email"] == "admin@synelia.cloud"
        assert principal["equipe"] is True and principal["role_equipe"] == "super_admin"

        # `contexte_travail` (worker_ctx.py) reconstruit un Contexte fidèle à ce principal —
        # jamais `email="worker"` tant qu'un principal a été sérialisé.
        ctx = worker_ctx.contexte_travail(session, ligne)
        assert ctx.principal is not None
        assert ctx.principal.email == "admin@synelia.cloud"
        assert ctx.principal.est_admin_plateforme is True

        ids = await local.reclamer(session, 10)
        assert ids == [travail_id]

        # Réclamé : `reclamer` ne le rendra plus tant qu'il reste `running`.
        assert await local.reclamer(session, 10) == []

    await local.executer_un(travail_id)

    r = await client.get(f"/v1/travaux/{travail_id}")
    assert r.status_code == 200
    assert r.json()["statut"] == "done"


async def test_reprendre_orphelins_ignore_une_pause_humaine(client):
    async with fabrique()() as session:
        session.add(
            Travail(
                id="orphelin-sans-attente",
                type="test.orphelin",
                label="Orphelin",
                statut="running",
                taches=[{"ordre": 1, "nom": "x", "statut": "running", "dureeS": 0}],
                contexte={},
            )
        )
        session.add(
            Travail(
                id="orphelin-en-pause",
                type="test.orphelin",
                label="En pause",
                statut="running",
                taches=[{"ordre": 1, "nom": "x", "statut": "running", "dureeS": 0}],
                contexte={"attente": {"etapeIndex": 0}},
            )
        )
        await session.commit()

        n = await local.reprendre_orphelins(session)
        assert n == 1

        sans_attente = await session.get(Travail, "orphelin-sans-attente")
        assert sans_attente.statut == "failed"
        assert sans_attente.erreur["message"] == "Interrompu par un redémarrage du worker"
        assert sans_attente.taches[0]["statut"] == "failed"

        en_pause = await session.get(Travail, "orphelin-en-pause")
        assert en_pause.statut == "running"


async def test_reclamer_est_atomique_meme_avec_deux_appels_concurrents(client):
    async with fabrique()() as session:
        session.add(
            Travail(
                id="a-reclamer",
                type="test.reclamer",
                label="À réclamer",
                statut="queued",
                taches=[],
                contexte={},
            )
        )
        await session.commit()

    async with fabrique()() as s1, fabrique()() as s2:
        ids1 = await local.reclamer(s1, 10)
        ids2 = await local.reclamer(s2, 10)
    # Un seul des deux appels a gagné la course (`UPDATE ... WHERE statut='queued'` conditionnel).
    assert sorted(ids1 + ids2) == ["a-reclamer"]
