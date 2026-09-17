"""Moteur des travaux de provisioning.

Un travail = une exécution de workflow ; une tâche = une étape du catalogue ; échec = la
tâche fautive nommée ; `relance` reprend **à l'étape échouée** ; `annulation` interrompt.

Deux moteurs derrière la même façade :
- `MoteurLocal` (par défaut, Vercel/dev) : exécute les étapes dans le processus, en ligne
  avant de répondre `202` quand `travaux_en_ligne` est vrai (fonctions serverless), sinon
  en tâche de fond.
- `MoteurTemporal` (SYNELIA_TEMPORAL_ADRESSE) : ouvre l'exécution Temporal ; la projection
  `travaux` est mise à jour par les activités. Import paresseux (extra `temporal`).

Un module enregistre son exécuteur par type :

    @executeur("vm.create")
    class ExecuteurVmCreate(Executeur):
        async def etape(self, ctx, travail, index, nom): ...
        async def compenser(self, ctx, travail, index_echoue): ...
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable
from typing import Any

from synelia_contract import workflows
from synelia_db.modeles import Travail
from synelia_db.session import fabrique
from synelia_kernel import erreurs
from synelia_kernel.config import reglages
from synelia_kernel.dates import maintenant
from synelia_kernel.ids import nouvel_id
from synelia_kernel.journal import journal

from synelia import otel
from synelia.deps.contexte import Contexte

log = journal("travaux")


def _enregistrer_metriques(travail: Travail) -> None:
    """Émission temps réel (OTel) en plus de la ligne `travaux` en base — no-op tant que
    `SYNELIA_OTEL_ENDPOINT` n'est pas configuré (cf. `synelia.otel`)."""
    otel.compteur_travaux.add(1, {"type": travail.type, "statut": travail.statut})
    if travail.duree_s is not None:
        otel.histogramme_duree_travaux.record(travail.duree_s, {"type": travail.type})


_EXECUTEURS: dict[str, type[Executeur]] = {}


class PauseHumaine(Exception):
    """Une étape lève ceci pour mettre le travail en pause — pas un échec, une attente réelle
    d'une décision humaine (validation d'un flux d'orchestration, par exemple). `_executer` la
    traite à part : le travail reste `running`, la tâche courante ne passe ni `ok` ni `failed`,
    et rien n'avance tant que `reprendre_apres_pause` n'est pas appelé."""

    def __init__(self, message: str, donnees: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.donnees = donnees or {}


class Executeur:
    """Par défaut : simulation — chaque étape réussit après `simulation_duree_etape_ms`."""

    compensable: bool = False

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        ms = reglages().simulation_duree_etape_ms
        if ms:
            await asyncio.sleep(ms / 1000)
        return None

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        """Appelé une fois toutes les étapes réussies (ex. : passer la ressource à `running`)."""
        return None


def executeur(type_travail: str) -> Callable[[type[Executeur]], type[Executeur]]:
    def _enregistrer(cls: type[Executeur]) -> type[Executeur]:
        _EXECUTEURS[type_travail] = cls
        return cls

    return _enregistrer


def executeur_pour(type_travail: str) -> Executeur:
    return _EXECUTEURS.get(type_travail, Executeur)()


ETAPES_GENERIQUES = [
    {"nom": "Valider la demande", "dureeS": 3},
    {"nom": "Appliquer l'opération", "dureeS": 20},
    {"nom": "Vérifier et journaliser", "dureeS": 5},
]


def _taches(etapes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ordre": i + 1,
            "nom": e["nom"],
            "statut": "pending",
            "dureeS": int(e.get("dureeS", 0)),
            "message": e.get("message"),
        }
        for i, e in enumerate(etapes)
    ]


def vers_contrat(t: Travail) -> dict[str, Any]:
    return {
        "id": t.id,
        "orgId": t.org_id or "",
        "type": t.type,
        "label": t.label,
        "statut": t.statut,
        "taches": [{k: v for k, v in tache.items() if v is not None} for tache in (t.taches or [])],
        "erreur": t.erreur,
        "startedAt": t.started_at,
        "dureeS": t.duree_s,
    }


def _en_ligne() -> bool:
    if os.environ.get("SYNELIA_TRAVAUX_EN_LIGNE"):
        return os.environ["SYNELIA_TRAVAUX_EN_LIGNE"].lower() in {"1", "true", "oui"}
    return bool(os.environ.get("VERCEL")) or reglages().env == "test"


def _mode_worker() -> bool:
    """Vrai quand ce processus API délègue l'exécution des travaux à `synelia worker`
    (`travaux/local.py`) plutôt que de les exécuter lui-même (`create_task`) — dev01 seulement,
    voir `docker-compose.dev01.yml`. Lu à l'appel (pas mis en cache) pour que les tests puissent
    le basculer par `monkeypatch.setenv`, comme `_en_ligne()`."""
    return os.environ.get("SYNELIA_TRAVAUX_WORKER", "").lower() in {"1", "true", "oui"}


async def demarrer_travail(
    ctx: Contexte,
    type_travail: str,
    cible: str,
    *,
    cible_type: str | None = None,
    cible_id: str | None = None,
    entree: dict[str, Any] | None = None,
    etapes: list[dict[str, Any]] | None = None,
    contexte: dict[str, Any] | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Ouvre le travail, l'exécute (en ligne ou en fond) et renvoie le `TravailProvisioning` du contrat."""
    defs = etapes or workflows.etapes(type_travail) or ETAPES_GENERIQUES
    travail = Travail(
        id=nouvel_id(),
        org_id=org_id or ctx.org_id_ou_none,
        type=type_travail,
        label=workflows.libelle(type_travail, cible),
        statut="queued",
        taches=_taches(defs),
        started_at=maintenant(),
        cible_type=cible_type,
        cible_id=cible_id,
        entree=entree or {},
        contexte=contexte or {},
        demande_par=ctx.utilisateur_id,
        correlation_id=ctx.correlation_id,
    )
    ctx.session.add(travail)
    await ctx.session.flush()

    if reglages().temporal_adresse:
        from synelia.travaux import temporal  # import paresseux

        await temporal.lancer(travail)
        return vers_contrat(travail)

    if _en_ligne():
        await _executer(ctx, travail, depuis=0)
    elif _mode_worker():
        # `synelia worker` (travaux/local.py) rejoue ce travail hors requête HTTP : il a besoin
        # du même acteur que `create_task` aujourd'hui pour que les lignes d'audit écrites par
        # les exécuteurs portent l'e-mail de l'utilisateur, pas un principal générique
        # « worker ». Aucun secret dans ce qui est sérialisé (pas de jeton, pas de mot de
        # passe). `vers_contrat` n'expose pas `contexte` — le principal reste interne.
        p = ctx.principal
        if p is not None:
            travail.contexte = {
                **travail.contexte,
                "principal": {
                    "utilisateur_id": p.utilisateur_id,
                    "email": p.email,
                    "nom": p.nom,
                    "org_id": p.org_id,
                    "role": p.role,
                    "equipe": p.equipe,
                    "role_equipe": p.role_equipe,
                },
            }
        travail.statut = "queued"
        await ctx.session.commit()
    else:
        await ctx.session.commit()
        asyncio.get_running_loop().create_task(_executer_detache(travail.id, ctx))
    return vers_contrat(travail)


async def _executer_detache(travail_id: str, ctx_origine: Contexte) -> None:
    async with fabrique()() as session:
        travail = await session.get(Travail, travail_id)
        if travail is None:
            return
        ctx = Contexte(
            request=ctx_origine.request,
            session=session,
            reglages=ctx_origine.reglages,
            correlation_id=ctx_origine.correlation_id,
            principal=ctx_origine.principal,
        )
        await _executer(ctx, travail, depuis=0)
        await session.commit()


async def _executer(ctx: Contexte, travail: Travail, depuis: int) -> None:
    ex = executeur_pour(travail.type)
    taches = [dict(t) for t in travail.taches]
    travail.statut = "running"
    travail.essai = (travail.essai or 0) + 1
    debut = maintenant()
    for i in range(depuis, len(taches)):
        taches[i]["statut"] = "running"
        travail.taches = copy.deepcopy(taches)
        # `commit`, pas `flush` : ce travail tourne dans sa propre transaction/session
        # (`_executer_detache`) qui ne se termine qu'à la toute fin de `_executer` — sans
        # commit intermédiaire, un `GET /travaux/{id}` sur une autre connexion (tout appelant
        # HTTP normal) ne voit RIEN bouger tant que le travail entier n'est pas fini : un
        # redimensionnement ou une création de cluster de plusieurs minutes affiche « queued »
        # figé de bout en bout, sans la progression étape par étape pourtant promise par
        # l'écran (constaté en direct via `vm.resize`). `expire_on_commit=False` sur la
        # session (`synelia_db.session.fabrique`) rend ce commit sûr : les attributs déjà
        # chargés sur `travail`/`ctx` restent lisibles ensuite sans requête implicite.
        await ctx.session.commit()
        try:
            message = await ex.etape(ctx, travail, i, taches[i]["nom"])
        except asyncio.CancelledError:
            taches[i]["statut"] = "failed"
            travail.taches = copy.deepcopy(taches)
            travail.statut = "rolled_back"
            await ctx.session.flush()
            _enregistrer_metriques(travail)
            raise
        except PauseHumaine as pause:
            # Ni un succès ni un échec : le travail reste `running`, la tâche courante affiche
            # le message d'attente, et `attente` (interne, absent du contrat `TravailProvisioning`)
            # porte de quoi reprendre exactement là où l'exécution s'est arrêtée.
            taches[i]["message"] = pause.message
            travail.taches = copy.deepcopy(taches)
            travail.contexte = {**travail.contexte, "attente": {"etapeIndex": i, **pause.donnees}}
            await ctx.session.flush()
            return
        except Exception as exc:  # noqa: BLE001
            message = (
                exc.message if isinstance(exc, erreurs.AppError) else str(exc) or type(exc).__name__
            )
            log.warning(
                "travail.etape_echouee",
                travail=travail.id,
                type=travail.type,
                etape=i + 1,
                erreur=message,
            )
            taches[i]["statut"] = "failed"
            taches[i]["message"] = message
            travail.taches = copy.deepcopy(taches)
            travail.statut = "failed"
            travail.erreur = {
                "message": f"Étape {i + 1} « {taches[i]['nom']} » : {message}",
                "correlationId": ctx.correlation_id,
                "suggestion": "Corrigez la cause puis relancez : la reprise repart de l'étape échouée.",
            }
            if ex.compensable:
                try:
                    await ex.compenser(ctx, travail, i)
                    travail.statut = "rolled_back"
                except Exception as exc2:  # noqa: BLE001
                    log.error("travail.compensation_echouee", travail=travail.id, erreur=str(exc2))
            travail.termine_le = maintenant()
            travail.duree_s = int((travail.termine_le - debut).total_seconds())
            await ctx.session.flush()
            _enregistrer_metriques(travail)
            return
        taches[i]["statut"] = "ok"
        if message:
            taches[i]["message"] = message
        travail.taches = copy.deepcopy(taches)
        await ctx.session.commit()  # même motif que ci-dessus : rend l'étape « ok » visible tout de suite.
    try:
        await ex.terminer(ctx, travail)
    except Exception as exc:  # noqa: BLE001
        # Toutes les étapes affichées (progression UI à durée fixe) ont réussi, mais
        # `terminer()` est l'endroit où l'effet réel a lieu (ex. appliquer un déploiement K8s,
        # supprimer un namespace) : une erreur ici ne doit jamais être avalée en un simple log
        # pendant que le travail se déclare quand même « done » — l'appelant croirait l'opération
        # effectuée alors qu'elle a échoué. Même traitement que l'échec d'une étape.
        message = exc.message if isinstance(exc, erreurs.AppError) else str(exc) or type(exc).__name__
        log.error("travail.terminaison_echouee", travail=travail.id, erreur=message)
        travail.statut = "failed"
        travail.erreur = {
            "message": f"Finalisation « {travail.label} » : {message}",
            "correlationId": ctx.correlation_id,
            "suggestion": "Corrigez la cause puis relancez.",
        }
        travail.termine_le = maintenant()
        travail.duree_s = int((travail.termine_le - travail.started_at).total_seconds())
        await ctx.session.flush()
        _enregistrer_metriques(travail)
        return
    travail.statut = "done"
    travail.erreur = None
    travail.termine_le = maintenant()
    travail.duree_s = int((travail.termine_le - travail.started_at).total_seconds())
    await ctx.session.flush()
    _enregistrer_metriques(travail)


async def relancer(ctx: Contexte, travail: Travail) -> Travail:
    if travail.statut not in {"failed", "rolled_back"}:
        raise erreurs.conflit(
            "Seul un travail en échec peut être relancé.", code="travail_non_relancable"
        )
    taches = [dict(t) for t in travail.taches]
    depuis = next((i for i, t in enumerate(taches) if t["statut"] == "failed"), 0)
    for t in taches[depuis:]:
        t["statut"] = "pending"
        t.pop("message", None)
    travail.taches = copy.deepcopy(taches)
    travail.erreur = None
    travail.statut = "queued"
    await ctx.session.flush()
    if reglages().temporal_adresse:
        from synelia.travaux import temporal

        await temporal.relancer(travail)
        return travail
    if _en_ligne():
        await _executer(ctx, travail, depuis=depuis)
    elif _mode_worker():
        travail.statut = "queued"
        await ctx.session.commit()
    else:
        await ctx.session.commit()
        asyncio.get_running_loop().create_task(_executer_detache(travail.id, ctx))
    return travail


async def reprendre_apres_pause(ctx: Contexte, travail: Travail, depuis: int) -> Travail:
    """Ré-entre dans l'étape `depuis` après une `PauseHumaine` — contrairement à `relancer`, le
    travail n'est pas en échec : il est resté `running`. L'appelant (le domaine, pas le moteur)
    a déjà écrit la décision quelque part que l'étape saura relire pour ne pas se repauser."""
    if reglages().temporal_adresse:
        from synelia.travaux import temporal

        await temporal.relancer(travail)
        return travail
    if _en_ligne():
        await _executer(ctx, travail, depuis=depuis)
    elif _mode_worker():
        # La présence de `attente` dans `contexte` signifie « en pause » pour le worker (1.4) ;
        # elle doit disparaître dès que l'humain a tranché. Réassignation d'un nouveau dict :
        # `contexte` est une colonne JSON, une mutation en place n'est pas détectée par
        # SQLAlchemy. La tâche courante reste `running` — le worker recalcule le point de
        # reprise (`executer_un`, règle 1.4).
        travail.contexte = {k: v for k, v in travail.contexte.items() if k != "attente"}
        travail.statut = "queued"
        await ctx.session.commit()
    else:
        await ctx.session.commit()
        asyncio.get_running_loop().create_task(_executer_detache(travail.id, ctx))
    return travail


async def annuler(ctx: Contexte, travail: Travail, *, motif: str | None = None) -> Travail:
    """`motif` : raison métier précise à afficher (ex. suppression de la ressource référencée
    pendant que ce travail était en cours/en pause) — par défaut le message générique
    d'annulation utilisateur, inchangé pour l'appel existant (`POST /travaux/{id}/annulation`)."""
    if travail.statut in {"done", "rolled_back"}:
        raise erreurs.conflit("Ce travail est déjà terminé.", code="travail_termine")
    message_tache = motif or "Annulé à la demande de l'utilisateur."
    taches = [dict(t) for t in travail.taches]
    for t in taches:
        if t["statut"] in {"pending", "running"}:
            t["statut"] = "failed"
            t["message"] = message_tache
    travail.taches = copy.deepcopy(taches)
    ex = executeur_pour(travail.type)
    if ex.compensable:
        idx = next((i for i, t in enumerate(taches) if t["statut"] == "failed"), len(taches) - 1)
        try:
            await ex.compenser(ctx, travail, idx)
        except Exception as exc:  # noqa: BLE001
            log.error("travail.compensation_echouee", travail=travail.id, erreur=str(exc))
    travail.statut = "rolled_back"
    travail.erreur = {
        "message": motif or "Travail annulé.",
        "correlationId": ctx.correlation_id,
        "suggestion": "Relancez l'opération depuis l'écran d'origine si nécessaire.",
    }
    travail.termine_le = maintenant()
    await ctx.session.flush()
    _enregistrer_metriques(travail)
    return travail
