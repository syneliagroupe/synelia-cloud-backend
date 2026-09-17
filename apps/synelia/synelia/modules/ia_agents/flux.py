"""Exécution réelle des flux d'orchestration (FONC-02, univers IA & Agents).

Un flux est un arbre (`FluxOrchestration.etapes`, une étape pouvant porter des `branches`
— un routeur — ou un `corps` — une boucle). Le moteur de travaux (`synelia.travaux`) ne
connaît qu'une liste plate de tâches indexées : on ne force pas l'arbre dedans. Seules les
étapes de *premier niveau* deviennent des tâches (`travail.taches[i]`) ; la marche récursive
dans les branches et les corps de boucle a lieu *à l'intérieur* de `etape()` pour cette tâche.

Pas de service Mastra séparé : un exécuteur Python natif, dans ce process, qui appelle
réellement la passerelle LiteLLM (`service.invoquer`) et la recherche documentaire de l'autre
module (`POST /ia/connaissances/{id}/rechercher`). C'est la plomberie la plus simple qui
fonctionne — Mastra reste envisageable plus tard si la portabilité multi-langage devient un
besoin réel, pas une hypothèse.

Ce que chaque type d'étape fait *réellement* — le détail exact vaut mieux qu'un renvoi vague :

- `declencheur` : passe-plat, sa sortie est l'entrée qui a démarré le travail.
- `agent` : appelle réellement `service.invoquer` (ou `invoquer_messages` si le flux a
  `memoirePartagee` — l'historique complet des tours d'agents est alors transmis).
- `outil` : **simulé**, étiqueté comme tel. `OutilAgent` (le contrat existant) ne porte que du
  texte descriptif (signature, authentification) — aucun champ URL/endpoint réel à appeler.
- `connaissance` : appelle réellement l'autre module (recherche vectorielle), en HTTP loopback
  sur ce même process — voir `_executer_connaissance`.
- `routeur` : évalue chaque condition de branche (voir `_condition_vraie` pour la sémantique
  exacte — ce n'est pas un langage d'expression), respecte `modeRoutage` (`premiere` s'arrête au
  premier match ; `toutes` exécute toutes les branches vraies, **séquentiellement** — pas de
  parallélisme asyncio ici, pour ne pas faire courir les mutations de variables partagées).
- `boucle` : itère réellement sur la variable `surItems` (doit être une liste), rejoue `corps`,
  borné par `maxIterations`.
- `humain` : pause réelle. Lève `PauseHumaine` ; le travail reste `running` avec un message
  explicite sur sa tâche. `POST .../reprendre` débloque.
- `code` : `eval` réellement exécuté, dans un espace de noms restreint (`ponytail` : ceci n'est
  PAS un bac à sable — pas d'isolation process/mémoire, juste des builtins retirés. Suffisant
  pour ce labo, pas pour un multi-tenant hostile).
- `reponse` : marque la sortie finale du flux. Son `detail` (le seul champ texte libre du
  contrat pour une étape) sert de gabarit, comme le `detail` d'une étape `agent` sert déjà à
  autre chose ailleurs dans l'interface.
- `anonymisation` / `habilitation` : **passe-plat, pas un garde-fou réel.** Le CLAUDE.md du
  frontend les décrit comme ne se négociant jamais (Presidio, filtrage RBAC documentaire) —
  câbler Presidio et la portée documentaire réelle est un chantier à part, pas fait ici. Ne pas
  laisser croire que ces étapes protègent quoi que ce soit dans cette itération.
- `transfert` : passe-plat (pas de canal omnicanal réel à transférer vers dans ce MVP).
"""

from __future__ import annotations

import ast
import builtins
import operator
import os
import re
import time
from typing import Any

import httpx
from sqlalchemy import select
from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.journal import journal

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.modules.ia_agents import service
from synelia.modules.ia_agents.service import depot_agents
from synelia.securite import emettre_acces
from synelia.travaux import Executeur, PauseHumaine, demarrer_travail, executeur
from synelia.travaux import moteur

log = journal("ia_agents.flux")

depot_flux = Depot(
    "flux_ia", m.FluxOrchestration, libelle="Flux d'orchestration", champs_recherche=("nom",)
)

TYPE_TRAVAIL = "ia.flux.executer"

# ─── Gabarit {{cle}} — substitution directe, pas un moteur de gabarit ──────────

_GABARIT = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def _rendre(texte: str, variables: dict[str, Any]) -> str:
    def _sub(mo: re.Match[str]) -> str:
        val = variables.get(mo.group(1))
        return "" if val is None else str(val)

    return _GABARIT.sub(_sub, texte or "")


# ─── Sémantique d'une condition de branche ─────────────────────────────────────
# Le studio laisse l'utilisateur écrire du texte libre (« categorie parmi facturation,
# résiliation, offre »), pas un DSL. Deux sémantiques, dans cet ordre :
#
# 1. Comparaison simple si un opérateur apparaît : le membre de gauche est cherché dans les
#    variables du flux, comparé au membre de droite (numériquement si possible, sinon en texte).
# 2. Sinon, correspondance par mot-clé : chaque mot de 4 lettres ou plus de la condition (hors
#    quelques mots vides français) est cherché en sous-chaîne, insensible à la casse, dans la
#    dernière sortie du flux et dans les valeurs des variables. Imprécis par nature — c'est la
#    contrepartie d'accepter un champ texte libre plutôt qu'un langage d'expression.

_OPERATEURS: dict[str, Any] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}
_MOTS_VIDES = {"parmi", "dans", "avec", "pour", "vrai", "faux", "quand", "alors"}


def _condition_vraie(condition: str, variables: dict[str, Any], derniere_sortie: Any) -> bool:
    if not condition:
        return False
    for op_txt, fn in _OPERATEURS.items():
        if op_txt in condition:
            gauche, _, droite = condition.partition(op_txt)
            gauche = gauche.strip()
            droite = droite.strip().strip("'\"")
            valeur = variables.get(gauche)
            if valeur is None:
                return False
            try:
                return bool(fn(float(valeur), float(droite)))
            except (TypeError, ValueError):
                return bool(fn(str(valeur), droite))
    signal = " ".join([str(derniere_sortie or ""), *(str(v) for v in variables.values())]).lower()
    mots = [w.strip(",.;:()") for w in condition.lower().split()]
    candidats = [w for w in mots if len(w) >= 4 and w not in _MOTS_VIDES]
    return any(c in signal for c in candidats) if candidats else False


# ─── État d'une exécution, porté par `travail.contexte` ────────────────────────


class _Etat:
    def __init__(self, travail: Travail, *, memoire_partagee: bool = False) -> None:
        self.travail = travail
        self.memoire_partagee = memoire_partagee
        ctx0 = travail.contexte or {}
        self.variables: dict[str, Any] = dict(ctx0.get("variables") or {})
        self.resultats: dict[str, Any] = dict(ctx0.get("resultats") or {})
        self.messages: list[dict[str, str]] = list(ctx0.get("messages") or [])
        self.derniere_sortie: Any = ctx0.get("derniereSortie")
        # Mesures de cet appel d'`etape()` seulement — jamais persistées telles quelles, juste
        # accumulées le temps de calculer les stats réelles avant d'être appliquées au flux.
        self.mesures: list[dict[str, Any]] = []
        self.dernier_cout_fcfa: float | None = None

    def sauver(self) -> None:
        self.travail.contexte = {
            **self.travail.contexte,
            "variables": self.variables,
            "resultats": self.resultats,
            "messages": self.messages,
            "derniereSortie": self.derniere_sortie,
        }


# ─── Dispatch par type d'étape ──────────────────────────────────────────────────


async def _executer_arbre(
    ctx: Contexte, etat: _Etat, travail: Travail, etapes: list[m.EtapeFlux], suffixe: str = ""
) -> Any:
    sortie: Any = None
    for etape in etapes:
        sortie = await _executer_etape_mesuree(ctx, etat, travail, etape, suffixe)
    return sortie


def _suivre_derniere_sortie(etat: _Etat, sortie: Any) -> None:
    if isinstance(sortie, str):
        etat.derniere_sortie = sortie
        # Accessible dans un gabarit sans connaître l'id de l'étape qui l'a produite.
        etat.variables["derniereSortie"] = sortie


async def _executer_etape_mesuree(
    ctx: Contexte, etat: _Etat, travail: Travail, etape: m.EtapeFlux, suffixe: str = ""
) -> Any:
    cle = etape.id + suffixe
    if cle in etat.resultats and etape.type != "humain":
        sortie = etat.resultats[cle].get("sortie")
        _suivre_derniere_sortie(etat, sortie)
        return sortie

    debut = time.perf_counter()
    etat.dernier_cout_fcfa = None
    pause = False
    erreur_msg: str | None = None
    try:
        sortie = await _dispatch(ctx, etat, travail, etape, suffixe)
    except PauseHumaine:
        pause = True
        raise
    except erreurs.AppError as exc:
        erreur_msg = exc.message
        raise
    except Exception as exc:  # noqa: BLE001
        erreur_msg = str(exc) or type(exc).__name__
        raise
    finally:
        if not pause:
            etat.mesures.append(
                {
                    "etapeId": etape.id,
                    "latenceMs": int((time.perf_counter() - debut) * 1000),
                    "erreur": erreur_msg is not None,
                    "coutFcfa": etat.dernier_cout_fcfa,
                }
            )

    _suivre_derniere_sortie(etat, sortie)
    etat.resultats[cle] = {**(etat.resultats.get(cle) or {}), "sortie": sortie}
    etat.sauver()
    return sortie


async def _dispatch(
    ctx: Contexte, etat: _Etat, travail: Travail, etape: m.EtapeFlux, suffixe: str
) -> Any:
    if etape.type == "declencheur":
        return etat.variables.get("entree")

    if etape.type == "agent":
        return await _executer_agent(ctx, etat, etape)

    if etape.type == "outil":
        return _executer_outil(etape)

    if etape.type == "connaissance":
        return await _executer_connaissance(ctx, etat, etape)

    if etape.type == "routeur":
        return await _executer_routeur(ctx, etat, travail, etape, suffixe)

    if etape.type == "boucle":
        return await _executer_boucle(ctx, etat, travail, etape, suffixe)

    if etape.type == "humain":
        return _executer_humain(etat, etape, suffixe)

    if etape.type == "code":
        return _executer_code(etat, etape)

    if etape.type == "reponse":
        texte = _rendre(etape.detail, etat.variables)
        etat.variables["sortie"] = texte
        return texte

    if etape.type in ("anonymisation", "habilitation", "transfert"):
        return etat.derniere_sortie if etat.derniere_sortie is not None else etat.variables.get("entree")

    return None


async def _executer_agent(ctx: Contexte, etat: _Etat, etape: m.EtapeFlux) -> str:
    if not etape.agentId:
        raise erreurs.validation(
            f"Étape « {etape.nom} » : aucun agent référencé.", champs={"agentId": "requis"}
        )
    agent = await depot_agents.obtenir(ctx, etape.agentId)
    base = etat.derniere_sortie if etat.derniere_sortie is not None else etat.variables.get("entree", "")
    message = _rendre(str(base), etat.variables)
    fragments = etat.variables.get("_fragmentsConnaissance")
    if fragments:
        message = f"{message}\n\nContexte documentaire :\n{fragments}"

    if etat.memoire_partagee:
        etat.messages.append({"role": "user", "content": message})
        resultat = await service.invoquer_messages(ctx, agent, etat.messages)
        etat.messages.append({"role": "assistant", "content": resultat["reponse"]})
    else:
        resultat = await service.invoquer(ctx, agent, message)

    etat.variables[f"sortie_{etape.id}"] = resultat["reponse"]
    etat.dernier_cout_fcfa = resultat.get("coutFcfa")
    return resultat["reponse"]


def _executer_outil(etape: m.EtapeFlux) -> dict[str, Any]:
    # ponytail : `OutilAgent` (le contrat déjà en place) ne porte que du texte descriptif —
    # signature, authentification — aucun champ URL/endpoint réel. Rien de cheap-to-support à
    # appeler pour de vrai ici ; résultat simulé, étiqueté sans ambiguïté.
    return {
        "simule": True,
        "note": f"Outil « {etape.nom} » ({etape.outilId or '—'}) : aucun endpoint réel déclaré dans le contrat, résultat simulé.",
    }


async def _executer_connaissance(ctx: Contexte, etat: _Etat, etape: m.EtapeFlux) -> dict[str, Any]:
    if not etape.connaissanceId:
        return {"fragments": [], "note": "Étape connaissance sans connaissanceId : rien à chercher."}
    base = etat.derniere_sortie if etat.derniere_sortie is not None else etat.variables.get("entree", "")
    requete = _rendre(str(base), etat.variables)
    port = os.environ.get("PORT", "4000")
    url = f"http://127.0.0.1:{port}/v1/ia/connaissances/{etape.connaissanceId}/rechercher"
    jeton = ctx.entete("Authorization")
    if not jeton and ctx.principal and ctx.principal.utilisateur_id:
        # Hors requête HTTP (`synelia worker`, `travaux/local.py`) : la fausse requête que le
        # worker construit (`worker_ctx.contexte_travail`) n'a aucun en-tête à transmettre à cet
        # appel loopback interne — on émet un jeton d'accès de courte durée pour le même
        # principal que celui qui a demandé ce flux, uniquement pour cet appel.
        jeton = "Bearer " + emettre_acces(
            {
                "sub": ctx.principal.utilisateur_id,
                "org": ctx.principal.org_id,
                "role": ctx.principal.role,
            },
            duree_s=60,
        )
    headers = {"Authorization": jeton} if jeton else {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json={"query": requete, "topK": 5}, headers=headers)
    except httpx.HTTPError as exc:
        raise erreurs.amont_indisponible("connaissances", str(exc)) from exc
    if r.status_code == 404:
        log.warning("flux.connaissance_indisponible", etape=etape.id, base=etape.connaissanceId)
        return {
            "fragments": [],
            "note": "Base de connaissances non disponible sur cette passerelle (404).",
        }
    if r.status_code >= 400:
        raise erreurs.amont_indisponible("connaissances", f"HTTP {r.status_code} : {r.text[:200]}")
    donnees = r.json()
    fragments = donnees.get("fragments") or []
    etat.variables["_fragmentsConnaissance"] = "\n".join(f"- {f.get('texte', '')}" for f in fragments)
    return donnees


async def _executer_routeur(
    ctx: Contexte, etat: _Etat, travail: Travail, etape: m.EtapeFlux, suffixe: str
) -> Any:
    branches = etape.branches or []
    par_defaut = next((b for b in branches if b.parDefaut), None)
    candidates = [b for b in branches if not b.parDefaut]
    retenues = [b for b in candidates if _condition_vraie(b.condition, etat.variables, etat.derniere_sortie)]
    if etape.modeRoutage != "toutes":
        retenues = retenues[:1]
    if not retenues and par_defaut is not None:
        retenues = [par_defaut]

    resultats: dict[str, Any] = {}
    for b in retenues:  # séquentiel — voir la note de tête de module sur `modeRoutage: toutes`
        resultats[b.nom] = await _executer_arbre(ctx, etat, travail, b.etapes, suffixe)
    etat.variables["_derniereBranche"] = retenues[0].nom if retenues else None
    return {"branchesRetenues": [b.nom for b in retenues], "resultats": resultats}


async def _executer_boucle(
    ctx: Contexte, etat: _Etat, travail: Travail, etape: m.EtapeFlux, suffixe: str
) -> list[Any]:
    items = etat.variables.get(etape.surItems or "")
    if not isinstance(items, list):
        items = []
    max_iter = etape.maxIterations or len(items)
    resultats = []
    for i, item in enumerate(items[:max_iter]):
        etat.variables["item"] = item
        etat.variables["indexBoucle"] = i
        resultats.append(await _executer_arbre(ctx, etat, travail, etape.corps or [], f"{suffixe}[{i}]"))
    return resultats


def _executer_humain(etat: _Etat, etape: m.EtapeFlux, suffixe: str) -> str:
    cle = etape.id + suffixe
    deja = etat.resultats.get(cle)
    if deja and deja.get("decision"):
        return deja["decision"]
    raise PauseHumaine(
        f"En attente d'une validation humaine : {etape.detail or etape.nom}",
        {"cle": cle, "etapeId": etape.id, "question": etape.detail or etape.nom},
    )


_BUILTINS_AUTORISES = {
    n: getattr(builtins, n)
    for n in ("str", "int", "float", "bool", "len", "abs", "round", "min", "max", "sum", "sorted", "list", "dict")
}


def _executer_code(etat: _Etat, etape: m.EtapeFlux) -> Any:
    # ponytail : `eval` d'une seule expression, builtins réduits à une poignée de fonctions pures
    # de transformation de données — pas un bac à sable réel (pas d'isolation process/mémoire/CPU).
    # Suffisant pour une transformation simple dans ce labo ; ne PAS réutiliser tel quel face à du
    # code non fiable en production multi-tenant.
    code = etape.detail or ""
    espace = {"variables": dict(etat.variables), "entree": etat.derniere_sortie}
    try:
        arbre = ast.parse(code, mode="eval")
        return eval(  # noqa: S307
            compile(arbre, "<etape-code>", "eval"), {"__builtins__": _BUILTINS_AUTORISES}, espace
        )
    except Exception as exc:  # noqa: BLE001
        raise erreurs.validation(f"Étape code « {etape.nom} » invalide : {exc}") from exc


def _resume_pour_tache(etape: m.EtapeFlux, sortie: Any) -> str:
    if etape.type == "routeur" and isinstance(sortie, dict):
        branches = ", ".join(sortie.get("branchesRetenues") or []) or "aucune"
        detail = "; ".join(
            f"{nom} → {str(val)[:160]}" for nom, val in (sortie.get("resultats") or {}).items()
        )
        return f"Branche(s) retenue(s) : {branches}. {detail}"
    if isinstance(sortie, str):
        return sortie[:500]
    return str(sortie)[:500] if sortie is not None else "ok"


# ─── Stats réelles persistées sur le flux ──────────────────────────────────────


def _agreger(mesures: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    par_id: dict[str, list[dict[str, Any]]] = {}
    for mesure in mesures:
        par_id.setdefault(mesure["etapeId"], []).append(mesure)
    agg: dict[str, dict[str, Any]] = {}
    for eid, ms in par_id.items():
        n = len(ms)
        couts = [x["coutFcfa"] for x in ms if x.get("coutFcfa") is not None]
        agg[eid] = {
            "exec": n,
            "latenceMs": int(sum(x["latenceMs"] for x in ms) / n),
            "erreurs": sum(1 for x in ms if x["erreur"]),
            "coutFcfa": (sum(couts) / len(couts)) if couts else None,
        }
    return agg


def _maj_stats_recursif(
    etapes: list[m.EtapeFlux], agg: dict[str, dict[str, Any]]
) -> list[m.EtapeFlux]:
    nouvelles = []
    for etape in etapes:
        e = etape
        patch: dict[str, Any] = {}
        info = agg.get(e.id)
        if info:
            n_avant = e.executions24h
            n_total = n_avant + info["exec"]
            patch["executions24h"] = n_total
            patch["latenceMs"] = (
                int((e.latenceMs * n_avant + info["latenceMs"] * info["exec"]) / n_total)
                if n_total
                else e.latenceMs
            )
            erreurs_avant = round(e.tauxErreurPct / 100 * n_avant)
            patch["tauxErreurPct"] = (
                round((erreurs_avant + info["erreurs"]) / n_total * 100, 2) if n_total else 0.0
            )
            if info["coutFcfa"] is not None:
                patch["coutPourMille"] = round(info["coutFcfa"] * 1000, 2)
        branches = (
            [b.model_copy(update={"etapes": _maj_stats_recursif(b.etapes, agg)}) for b in e.branches]
            if e.branches
            else None
        )
        corps = _maj_stats_recursif(e.corps, agg) if e.corps else None
        if patch or branches is not None or corps is not None:
            update = dict(patch)
            if branches is not None:
                update["branches"] = branches
            if corps is not None:
                update["corps"] = corps
            e = e.model_copy(update=update)
        nouvelles.append(e)
    return nouvelles


async def _appliquer_mesures(ctx: Contexte, flux: m.FluxOrchestration, mesures: list[dict[str, Any]]) -> None:
    if not mesures:
        return
    agg = _agreger(mesures)
    etapes = _maj_stats_recursif(flux.etapes, agg)
    await depot_flux.remplacer(ctx, flux.id, flux.model_copy(update={"etapes": etapes}))


# ─── Point d'entrée : démarrer un travail ───────────────────────────────────────


def _preparer_variables(
    flux: m.FluxOrchestration, entree: str, overrides: dict[str, Any] | None
) -> dict[str, Any]:
    variables: dict[str, Any] = {v.cle: v.valeur for v in flux.variables}
    variables["entree"] = entree
    if overrides:
        variables.update(overrides)
    return variables


async def demarrer_execution(
    ctx: Contexte, flux: m.FluxOrchestration, entree: str, overrides: dict[str, Any] | None
) -> dict[str, Any]:
    taches = [{"nom": e.nom, "dureeS": 0} for e in flux.etapes]
    return await demarrer_travail(
        ctx,
        TYPE_TRAVAIL,
        flux.nom,
        cible_type="flux_ia",
        cible_id=flux.id,
        entree={"entree": entree, "variables": overrides or {}},
        etapes=taches,
        contexte={"variables": _preparer_variables(flux, entree, overrides), "resultats": {}, "messages": []},
    )


async def annuler_executions_en_cours(
    ctx: Contexte, flux_id: str, *, motif: str
) -> list[Travail]:
    """Suppression d'un flux (`DELETE /ia/flux/{id}`) : tout travail encore `queued`/`running`
    qui le référence — notamment une exécution en pause `humain` (`PauseHumaine`, voir le
    module) — resterait sinon un zombie éternel : le flux 404 désormais, `POST
    .../reprendre` n'a plus de flux à relire (`depot_flux.obtenir` lèverait), et rien d'autre
    ne repasse jamais sur ce travail pour le faire terminer. On le bascule ici vers `rolled_back`
    (même convention que `moteur.annuler`, l'annulation utilisateur d'un travail) avec un motif
    explicite, dans la même transaction que la suppression du flux — pas un correctif dans
    l'IHM. Un flux qui existe encore et dont un travail est réellement en attente d'approbation
    n'est jamais concerné : cette fonction n'est appelée qu'après confirmation de suppression."""
    q = select(Travail).where(
        Travail.type == TYPE_TRAVAIL,
        Travail.cible_id == flux_id,
        Travail.statut.in_(("queued", "running")),
    )
    travaux = list((await ctx.session.execute(q)).scalars())
    for travail in travaux:
        await moteur.annuler(ctx, travail, motif=motif)
    return travaux


@executeur(TYPE_TRAVAIL)
class ExecuteurFluxExecuter(Executeur):
    """Chaque tâche = une étape de premier niveau de `FluxOrchestration.etapes`. La marche
    récursive dans les branches/boucles a lieu dans `_dispatch`, à l'intérieur du même appel."""

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        flux = await depot_flux.obtenir(ctx, travail.cible_id)
        etat = _Etat(travail, memoire_partagee=flux.memoirePartagee)
        etape = flux.etapes[index]
        try:
            sortie = await _executer_etape_mesuree(ctx, etat, travail, etape)
        finally:
            if etat.mesures:
                await _appliquer_mesures(ctx, flux, etat.mesures)
        return _resume_pour_tache(etape, sortie)
