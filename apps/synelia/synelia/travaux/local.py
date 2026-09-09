"""Worker Local des travaux : poll de la table `travaux`, un seul réplica.

Remplace le `create_task` en processus API (`moteur._executer_detache`) sur dev01
(`SYNELIA_TRAVAUX_WORKER=true`, cf. `docker-compose.dev01.yml`) : un job en vol ne se perd plus
à chaque redéploiement de l'API, il continue dans ce processus séparé.

Un seul réplica : `reprendre_orphelins` suppose qu'un travail `running` sans `attente` dans son
`contexte` n'appartient à personne d'autre — passer à plusieurs réplicas exigerait un bail/
heartbeat par travail, pas fait ici (disproportionné pour ce lab)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from sqlalchemy import select, update
from synelia_db import rls
from synelia_db.modeles import Travail
from synelia_db.session import fabrique, fermer, initialiser_schema
from synelia_kernel.dates import maintenant
from synelia_kernel.journal import journal

from synelia.travaux import moteur, worker_ctx

log = journal("travaux.local")


async def reprendre_orphelins(session: Any) -> int:
    """Au boot : un travail resté `running` **sans** `attente` dans son contexte a été
    interrompu par un redémarrage du worker précédent — il n'y a qu'un réplica, donc s'il
    redémarre, rien n'exécute plus ce travail. Ne **jamais** le relancer automatiquement : un
    `vm.resize` à moitié fait n'est pas idempotent. `contexte` est une colonne JSON (pas JSONB,
    voir `packages/db/synelia_db/modeles/travaux.py`) : le filtre `attente` se fait en Python,
    quelques lignes au plus."""
    lignes = (
        (await session.execute(select(Travail).where(Travail.statut == "running")))
        .scalars()
        .all()
    )
    n = 0
    for travail in lignes:
        if "attente" in (travail.contexte or {}):
            continue
        taches = [dict(t) for t in travail.taches or []]
        for t in taches:
            if t.get("statut") == "running":
                t["statut"] = "failed"
                t["message"] = "Interrompu par un redémarrage du worker"
        travail.taches = taches
        travail.statut = "failed"
        travail.erreur = {
            "message": "Interrompu par un redémarrage du worker",
            "suggestion": "Relancez : la reprise repart de l'étape échouée",
        }
        travail.termine_le = maintenant()
        n += 1
    if n:
        await session.commit()
    return n


async def reclamer(session: Any, n: int) -> list[str]:
    """Réclamation atomique de `n` travaux `queued` au plus, sans `SKIP LOCKED` (le plus ancien
    d'abord) : chaque id est basculé `running` par un `UPDATE ... WHERE statut='queued'`
    conditionnel — un seul worker peut gagner cette course, même si deux réclament en même
    temps. SQLite la supporte aussi (utilisé par les tests)."""
    ids = (
        (
            await session.execute(
                select(Travail.id)
                .where(Travail.statut == "queued")
                .order_by(Travail.started_at)
                .limit(n)
            )
        )
        .scalars()
        .all()
    )
    reclames: list[str] = []
    for id_ in ids:
        resultat = await session.execute(
            update(Travail)
            .where(Travail.id == id_, Travail.statut == "queued")
            .values(statut="running")
            .returning(Travail.id)
        )
        if resultat.first() is not None:
            reclames.append(id_)
    if reclames:
        await session.commit()
    else:
        await session.rollback()
    return reclames


async def executer_un(travail_id: str) -> None:
    """Exécute un travail déjà réclamé (`statut='running'`), dans sa propre session — le worker
    peut réclamer plusieurs travaux en parallèle (`boucle`), chacun avec sa connexion."""
    async with fabrique()() as session:
        travail = await session.get(Travail, travail_id)
        if travail is None:
            return
        # Reproduit le contexte RLS qu'une requête HTTP hérite via contextvars — `create_task`
        # en bénéficiait gratuitement (même boucle asyncio) ; ce worker tourne dans une tâche
        # neuve, il faut le poser explicitement avant la première requête de `_executer`.
        # `session.get(Travail, travail_id)` juste au-dessus a déjà exécuté la première requête
        # de cette transaction (pour connaître `travail.org_id`, ce qu'on ne peut pas savoir
        # avant de lire la ligne) — l'écouteur `begin` de `rls.py` a donc déjà posé `app.org_id`
        # à `''` avant qu'on sache quoi y mettre. `org_id_transaction.set()` seul ne suffit pas
        # sur une transaction déjà ouverte (même bug que `deps/contexte.py::contexte`) : il faut
        # aussi `rls.poser()` pour appliquer la valeur directement.
        rls.org_id_transaction.set(travail.org_id or "")
        await rls.poser(session, travail.org_id or "")
        ctx = worker_ctx.contexte_travail(session, travail)
        taches = travail.taches or []
        # Couvre les trois origines d'un `executer_un` : travail neuf (aucune tâche `ok`) → 0 ;
        # relance (`relancer`, la première tâche en échec est remise `pending`) → cette tâche ;
        # reprise après pause (`reprendre_apres_pause`) → la tâche restée `running`.
        depuis = next((i for i, t in enumerate(taches) if t["statut"] != "ok"), 0)
        try:
            await moteur._executer(ctx, travail, depuis)  # noqa: SLF001
            await session.commit()
        except Exception:  # noqa: BLE001
            # `_executer` avale déjà les échecs d'étape (le travail finit `failed`, proprement).
            # Une exception qui remonte jusqu'ici est un bug hors de ce contrat (connectivité DB,
            # bug dans `contexte_travail`…) : on journalise et on laisse le travail `running` —
            # `reprendre_orphelins` le marquera `failed`, relançable, au prochain boot du worker.
            # Pas de perte silencieuse, pas de double exécution.
            log.error("travail.executer_un_echec", travail=travail_id, exc_info=True)
            await session.rollback()


async def boucle(arret: asyncio.Event, concurrence: int = 8, intervalle_s: float = 1.0) -> None:
    semaphore = asyncio.Semaphore(concurrence)
    en_cours: set[asyncio.Task[None]] = set()

    async def _avec_semaphore(travail_id: str) -> None:
        async with semaphore:
            await executer_un(travail_id)

    while not arret.is_set():
        en_cours = {t for t in en_cours if not t.done()}
        slots_libres = concurrence - len(en_cours)
        if slots_libres > 0:
            async with fabrique()() as session:
                ids = await reclamer(session, slots_libres)
            for travail_id in ids:
                en_cours.add(asyncio.create_task(_avec_semaphore(travail_id)))
        try:
            await asyncio.wait_for(arret.wait(), timeout=intervalle_s)
        except TimeoutError:
            pass
    # Arrêt demandé (SIGTERM/SIGINT) : cesse de réclamer, laisse jusqu'à 55 s aux travaux en
    # vol pour finir — `stop_grace_period: 60s` côté compose (docker-compose.dev01.yml). Ceux
    # qui n'ont pas fini sont tués avec le processus ; `reprendre_orphelins` les marquera
    # `failed` au prochain boot (comportement documenté, préférable à la perte silencieuse
    # d'aujourd'hui).
    if en_cours:
        await asyncio.wait(en_cours, timeout=55)


async def ancrage_quotidien(arret: asyncio.Event) -> None:
    """Tâche compagne de `boucle` : ancrage quotidien du journal d'audit (`synelia.audit.ancrer`,
    §3 de `docs/PLAN-ARCHITECTURE-SUITE.md`) — une fois au boot du worker, puis toutes les 24 h.
    Best-effort de bout en bout : une exception ici (Postgres, SMTP…) est journalisée mais
    n'arrête jamais le worker — le poll des travaux (`boucle`) continue indépendamment."""
    from synelia.audit import ancrer

    while not arret.is_set():
        try:
            async with fabrique()() as session:
                await ancrer(session)
        except Exception:  # noqa: BLE001
            log.error("audit.ancrage_echoue", exc_info=True)
        try:
            await asyncio.wait_for(arret.wait(), timeout=24 * 3600)
        except TimeoutError:
            pass


async def demarrer() -> None:
    """Point d'entrée : `synelia worker` sans `SYNELIA_TEMPORAL_ADRESSE` (voir `__main__.py`)."""
    from synelia.app import routeurs_modules

    routeurs_modules()  # importe les modules → enregistre les exécuteurs (_EXECUTEURS)
    await initialiser_schema()  # sûr : verrou pg_advisory_xact_lock (session.py, étape 1.1)
    async with fabrique()() as session:
        n = await reprendre_orphelins(session)
    log.info("worker.demarre")
    log.info("travaux.orphelins", n=n)

    arret = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, arret.set)

    tache_ancrage = asyncio.create_task(ancrage_quotidien(arret))
    try:
        await boucle(arret)
    finally:
        tache_ancrage.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tache_ancrage
    await fermer()
