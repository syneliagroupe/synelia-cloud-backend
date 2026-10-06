from __future__ import annotations

import asyncio
import logging
from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel import erreurs
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule
from synelia_openstack.magnum import MagnumOpenStack, MagnumSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, PauseHumaine, executeur

logger = logging.getLogger(__name__)

depot_cluster = Depot("k8s_cluster", m.ClusterK8s)
depot_pool = Depot("k8s_pool", m.PoolWorkers)

MODULES = {
    "cni": "Réseaux de pods (Calico)",
    "ingress-nginx": "Contrôleur d'entrée HTTP",
    "monitoring": "Surveillance et alerting",
    "cert-manager": "Certificats TLS automatiques",
    "autoscaler": "Autoscaler horizontal de pods",
}


def amont() -> MagnumSimule:
    return fournisseur(MagnumSimule, MagnumOpenStack)


def _compute_amont() -> ComputeSimule:
    """Même bascule globale que `amont()` (un seul mode `openstack`/simulé pour toute la
    plateforme, cf. `synelia_openstack.fabrique.fournisseur`) : les VM Nova d'un cluster
    Magnum réel sont toujours lues par le même connecteur Compute que le module `vms`."""
    return fournisseur(ComputeSimule, ComputeOpenStack)


# Même écart que `vms.service._DELTA_DIAGNOSTICS_S` : deux relevés `diagnostics` espacés de
# cette durée sont nécessaires pour dériver un %CPU/débit réseau depuis des compteurs cumulés
# (cf. `vms.service._diagnostics_vers_valeurs`, réutilisée ici nœud par nœud).
_DELTA_DIAGNOSTICS_S = 0.6


async def metriques_instantanees(ctx: Contexte, cluster: m.ClusterK8s) -> dict[str, Any] | None:
    """CPU/RAM/réseau instantanés du cluster, agrégés depuis les diagnostics Nova/libvirt réels
    (mêmes deux relevés espacés que `vms.service.diagnostics_instantanes`) de chacune des VM
    masters/workers réellement derrière ce cluster (`MagnumOpenStack.cluster_nodes`, retrouvées
    par la stack Heat du cluster Magnum — pas des nœuds Kubernetes fabriqués).

    `None` en simulation, si le cluster n'a pas (encore) de `magnum_cluster_id`, ou si Magnum ne
    connaît encore aucune VM pour ce cluster (juste soumis, stack Heat pas encore posée) :
    l'appelant retombe alors sur un état vide plutôt qu'une valeur inventée — même politique que
    `vms.service.diagnostics_instantanes`. Un dict avec `noeuds` toujours rempli et `agrege` à
    `None` si aucun nœud n'est `ACTIVE` (cluster provisionné mais éteint, ou en train de
    basculer d'état) : la liste des nœuds reste utile même sans agrégat CPU/RAM."""
    if not isinstance(amont(), MagnumOpenStack):
        return None
    secrets = await depot_cluster.secrets(ctx, cluster.id)
    mid = secrets.get("magnum_cluster_id")
    if not mid:
        return None
    noeuds = await asyncio.to_thread(amont().cluster_nodes, mid)
    if not noeuds:
        return None

    from synelia.modules.vms.service import _diagnostics_vers_valeurs

    compute = _compute_amont()
    actifs = [n for n in noeuds if n["statut"].upper() == "ACTIVE"]
    avants: dict[str, dict[str, Any] | None] = {}
    for n in actifs:
        avants[n["id"]] = await asyncio.to_thread(compute.diagnostics, n["id"])
    if actifs:
        await asyncio.sleep(_DELTA_DIAGNOSTICS_S)

    resultat_noeuds: list[dict[str, Any]] = []
    cpu_vals: list[float] = []
    ram_vals: list[float] = []
    reseau_total = 0.0
    for n in noeuds:
        avant = avants.get(n["id"])
        if n["statut"].upper() != "ACTIVE" or avant is None:
            resultat_noeuds.append(dict(n))
            continue
        apres = await asyncio.to_thread(compute.diagnostics, n["id"])
        if apres is None:
            resultat_noeuds.append(dict(n))
            continue
        valeurs = _diagnostics_vers_valeurs(avant, apres, _DELTA_DIAGNOSTICS_S, n["vcpu"] or 1)
        resultat_noeuds.append({**n, "cpu": valeurs["cpu"], "ram": valeurs["ram"]})
        cpu_vals.append(valeurs["cpu"])
        ram_vals.append(valeurs["ram"])
        reseau_total += valeurs["reseau_entrant"]

    agrege = (
        {
            "cpu": sum(cpu_vals) / len(cpu_vals),
            "ram": sum(ram_vals) / len(ram_vals),
            "reseau_entrant": reseau_total,
        }
        if cpu_vals
        else None
    )
    return {"agrege": agrege, "noeuds": resultat_noeuds}


# États non terminaux : un cluster dans l'un de ces statuts peut avoir évolué côté Magnum
# depuis le dernier relevé et vaut la peine d'être vérifié en direct (cf. `reconcilier_statut`).
STATUTS_NON_TERMINAUX = {"provisioning", "updating"}


def _mapper_statut_magnum(statut_amont: str) -> str | None:
    """Traduit un statut Magnum réel (`CREATE_COMPLETE`, `CREATE_FAILED`,
    `UPDATE_IN_PROGRESS`…) vers le `statut` applicatif du contrat (`running`/`degraded`/
    `provisioning`/`updating`, cf. `ClusterK8s.statut`) — `None` si le statut amont ne
    correspond à aucun état stable connu, auquel cas on ne touche pas la ressource.

    `DELETE_COMPLETE` (renvoyé par `MagnumOpenStack.cluster_statut()` quand Magnum ne connaît
    plus du tout le cluster — jamais créé pour de vrai, ou supprimé hors bande) vaut `degraded` :
    trouvé en direct sur une ligne `paas-shared-cluster2` restée `provisioning` 3 jours, dont le
    `magnum_cluster_id` en secret ne correspondait à aucun cluster Magnum réel (create jamais
    abouti, avant le fix CAPI du 2026-09-07) — avant ce correctif, `DELETE_COMPLETE` ne
    correspondait à aucune branche ci-dessus et `reconcilier_statut` ne touchait donc jamais la
    ressource : elle restait `provisioning` indéfiniment malgré un appel Magnum réel à chaque
    lecture. `ClusterK8s.statut` n'a pas de valeur `erreur`/`absente` (seulement
    `running|degraded|provisioning|updating`, cf. l'invariant `docs/GUIDE-MODULE.md`) —
    `degraded` est l'état sincère le plus proche, même choix que `ExecuteurK8sCreate.compenser`."""
    s = statut_amont.upper()
    if s.endswith("FAILED"):
        return "degraded"
    if s in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "ROLLBACK_COMPLETE", "RESUME_COMPLETE"):
        return "running"
    if s == "CREATE_IN_PROGRESS":
        return "provisioning"
    if s.endswith("IN_PROGRESS"):
        return "updating"
    if s == "DELETE_COMPLETE":
        return "degraded"
    return None


# Fenêtre de sondage Magnum de la dernière étape de `k8s.create` : un échec structurel (403
# Keystone, quota) se voit en quelques secondes, mais un provisioning réussi prend ~6 min sur
# ce lab. Avec 45 s, le travail restait `running` à vie (PauseHumaine) alors que le cluster
# était déjà actif ; le travail s'exécute dans le worker (pas dans la requête HTTP), il peut
# donc attendre la vraie issue — au-delà de 20 min seulement, il se met en pause.
_DELAI_CREATION_MAX_S = 1200.0
_DELAI_CREATION_SONDE_S = 10.0
_GRACE_INCONNU_S = 60.0


async def _attendre_issue_creation(mid: str) -> str:
    """Sonde le statut Magnum du cluster tout juste soumis jusqu'à un statut terminal connu de
    `_mapper_statut_magnum` (`running`/`degraded`) ou expiration du délai borné ci-dessus.
    Renvoie le dernier statut amont vu dans tous les cas (jamais d'exception : un sondage
    illisible n'est qu'une lecture ratée, pas une preuve d'échec) — c'est l'appelant qui décide
    de la suite (succès, échec réel, ou toujours en cours) à partir de la valeur renvoyée."""
    statut = ""
    delai = 0.0
    while delai < _DELAI_CREATION_MAX_S:
        try:
            statut = await asyncio.to_thread(amont().cluster_statut, mid)
        except Exception as exc:  # noqa: BLE001 — lecture best effort, on ressonde
            logger.debug("sondage Magnum %s impossible : %s", mid, exc)
        else:
            # Magnum peut ignorer un cluster à l'instant où on vient de le créer (constaté :
            # `DELETE_COMPLETE` à la milliseconde, cluster bien présent ensuite) : pas terminal
            # pendant la minute de grâce.
            inconnu_trop_tot = statut == "DELETE_COMPLETE" and delai < _GRACE_INCONNU_S
            if not inconnu_trop_tot and _mapper_statut_magnum(statut) in ("running", "degraded"):
                return statut
        await asyncio.sleep(_DELAI_CREATION_SONDE_S)
        delai += _DELAI_CREATION_SONDE_S
    return statut


async def reconcilier_statut(ctx: Contexte, cluster: m.ClusterK8s) -> m.ClusterK8s:
    """Relit le statut réel du cluster côté Magnum et met à jour la ressource si l'amont a
    évolué depuis le dernier relevé, avant de la renvoyer.

    `POST /kubernetes` marque son travail `done` en ~2 s sans attendre Magnum (correct : un
    provisioning réel prend plusieurs minutes, on ne bloque pas le travail dessus, cf.
    `ExecuteurK8sCreate.terminer`) — mais rien ne rafraîchissait plus jamais l'état depuis
    l'amont ensuite : `GET /kubernetes/{id}` restait figé sur `provisioning` indéfiniment
    (constaté en direct — statut resté `provisioning` en base bien après que `openstack coe
    cluster show` rapportait `CREATE_COMPLETE`). Choix « reconcile-on-read » plutôt qu'un
    réconciliateur périodique séparé : aucune tâche planifiée n'existe encore ailleurs dans
    cette application (la métrologie horaire évoquée par `docs/PLAN-DIRECTEUR.md` n'est pas
    câblée), et ce module ne doit pas toucher `app.py`/`travaux/moteur.py` pour en introduire
    une — le motif « l'état réel peut dériver de la base, on le rafraîchit à la demande » est
    déjà celui utilisé ailleurs dans l'app (`statut_serveur` avant un SSH, par ex.)."""
    if cluster.statut not in STATUTS_NON_TERMINAUX:
        return cluster
    secrets = await depot_cluster.secrets(ctx, cluster.id)
    mid = secrets.get("magnum_cluster_id")
    if not mid:
        return cluster
    statut_amont = await asyncio.to_thread(amont().cluster_statut, mid)
    nouveau = _mapper_statut_magnum(statut_amont)
    if nouveau and nouveau != cluster.statut:
        return await depot_cluster.definir_statut(ctx, cluster.id, nouveau)
    return cluster


def kubeconfig_reel(magnum_cluster_id: str) -> dict[str, str] | None:
    """Kubeconfig admin réel du cluster (CA + certificat client signés par Magnum), ou `None`
    en mode simulé — l'appelant retombe alors sur un kubeconfig factice."""
    if not isinstance(amont(), MagnumOpenStack):
        return None
    from synelia_openstack.k8s_workload import construire_kubeconfig

    return construire_kubeconfig(magnum_cluster_id)


@executeur("k8s.create")
class ExecuteurK8sCreate(Executeur):
    compensable = True

    async def etape(self, ctx: Contexte, travail: Travail, index: int, nom: str) -> str | None:
        if index == 0:
            cluster = await depot_cluster.obtenir(ctx, travail.cible_id or "")
            entree = travail.entree or {}
            from synelia.modules.espaces.service import depot as depot_espaces

            secrets_espace = await depot_espaces.secrets(ctx, cluster.espaceId)
            # `amont().creer_cluster` (openstacksdk Magnum, synchrone) est déchargé via
            # `asyncio.to_thread` : même garde que `vms.service`, sans quoi un appel amont lent
            # gèlerait la boucle asyncio — donc l'API entière, tous tenants confondus.
            cl = await asyncio.to_thread(
                amont().creer_cluster,
                nom=cluster.nom,
                pools=entree.get("pools") or [],
                master_count=cluster.controlPlane.nodes,
                reseau_id=secrets_espace.get("reseau_id"),
            )
            await depot_cluster.definir_secrets(ctx, cluster.id, {"magnum_cluster_id": cl["id"]})
            c = dict(travail.contexte)
            c["statut_amont"] = cl["statut"]
            travail.contexte = c
            return f"Cluster Magnum soumis ({cl['id']}, {cl['statut']})"
        if index == len(travail.taches) - 1:
            # Dernière étape du catalogue (« Publier le kubeconfig ») : avant ce correctif, le
            # travail passait `ok`/`done` ici sans jamais reconsulter Magnum depuis la
            # soumission (étape 0) — y compris quand la création amont échouait vite (403
            # Keystone d'imbrication d'AC, limitation de plateforme connue), ce que seule une
            # lecture ultérieure de la ressource révélait. On resonde donc Magnum ici, borné à
            # `_DELAI_CREATION_MAX_S` (cf. commentaire sur la constante) :
            #  - statut terminal `running` : l'étape réussit franchement (cluster confirmé actif).
            #  - statut terminal `degraded` (`*_FAILED`, cluster disparu) : on fait échouer
            #    l'étape (`compenser` supprime le cluster amont, la ressource passe `degraded`,
            #    le travail `rolled_back`) — plus de faux `done` sur un échec réel.
            #  - toujours non terminal après la fenêtre bornée (provisioning réel en cours,
            #    plausible : un vrai Heat/CAPI prend plusieurs minutes) : ni succès ni échec
            #    avéré, donc `PauseHumaine` — le travail reste `running`, cette étape reste
            #    honnêtement non `ok` plutôt que de mentir sur une complétion pas encore connue.
            #    Un `GET /kubernetes/{id}` (`reconcilier_statut`) continue de rafraîchir la
            #    ressource entre-temps ; `POST /travaux/{id}/annulation` reste disponible pour
            #    clore ce travail si l'opérateur ne veut pas attendre davantage.
            secrets = await depot_cluster.secrets(ctx, travail.cible_id or "")
            mid = secrets.get("magnum_cluster_id")
            if not mid:
                return None
            statut_amont = await _attendre_issue_creation(mid)
            c = dict(travail.contexte)
            c["statut_amont"] = statut_amont
            travail.contexte = c
            mappe = _mapper_statut_magnum(statut_amont)
            if mappe == "degraded":
                raise erreurs.amont_indisponible(
                    "magnum",
                    f"La création du cluster Magnum a échoué (statut `{statut_amont}`).",
                )
            if mappe is None or mappe in ("provisioning", "updating"):
                raise PauseHumaine(
                    f"Cluster Magnum toujours `{statut_amont}` après "
                    f"{int(_DELAI_CREATION_MAX_S)} s de sondage — provisioning réel "
                    "probablement en cours, statut définitif pas encore connu."
                )
            return f"Cluster Magnum confirmé actif (`{statut_amont}`)"
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        # N'est atteint que si la dernière étape a confirmé `running` ci-dessus (sinon : échec/
        # rollback, ou `PauseHumaine` qui n'appelle jamais `terminer`) — `statut_amont` ici est
        # donc toujours une valeur `*_COMPLETE` en pratique, mais on garde le même mappage que
        # `reconcilier_statut` par défense en profondeur plutôt que de supposer `running` en dur.
        statut_amont = str(travail.contexte.get("statut_amont", ""))
        statut = "running" if statut_amont.endswith("COMPLETE") else "provisioning"
        await depot_cluster.definir_statut(ctx, travail.cible_id or "", statut)
        # Migrer les pools initiaux du ClusterK8s vers depot_pool pour une source de vérité
        # unique (tous les pools, qu'ils soient créés avec le cluster ou après, vivent dans
        # depot_pool — cf. bug fixé : pools initiaux inaccessibles via PATCH/DELETE).
        cluster = await depot_cluster.obtenir(ctx, travail.cible_id or "")
        for pool in cluster.pools or []:
            await depot_pool.creer(ctx, pool, parent_id=travail.cible_id, id_=nouvel_id())
        # Vider ClusterK8s.pools puisque la lecture assemble désormais les pools depuis depot_pool.
        cluster_sans_pools = cluster.model_copy(update={"pools": []})
        await depot_cluster.remplacer(ctx, travail.cible_id or "", cluster_sans_pools)

    async def compenser(self, ctx: Contexte, travail: Travail, index_echoue: int) -> None:
        secrets = await depot_cluster.secrets(ctx, travail.cible_id or "")
        mid = secrets.get("magnum_cluster_id")
        if mid:
            await asyncio.to_thread(amont().supprimer_cluster, mid)
        # `ClusterK8s.statut` n'a pas de valeur `erreur` dans le contrat (seulement `running`/
        # `degraded`/`provisioning`/`updating`) : écrire `erreur` ici cassait la prochaine
        # lecture (`Depot._vers_modele` valide `r.donnees` contre le modèle Pydantic, qui
        # rejette la valeur hors énumération) — `degraded` est l'état le plus proche d'un
        # cluster dont la création amont a échoué.
        await depot_cluster.definir_statut(ctx, travail.cible_id or "", "degraded")


@executeur("k8s.delete")
class ExecuteurK8sDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        secrets = await depot_cluster.secrets(ctx, travail.cible_id or "")
        mid = secrets.get("magnum_cluster_id")
        if mid:
            await asyncio.to_thread(amont().supprimer_cluster, mid)
        await depot_pool.supprimer_enfants(ctx, travail.cible_id or "")
        await depot_cluster.supprimer(ctx, travail.cible_id or "", logique=True)


@executeur("k8s.upgrade")
class ExecuteurK8sUpgrade(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        cluster = await depot_cluster.obtenir(ctx, travail.cible_id or "")
        await depot_cluster.remplacer(
            ctx,
            travail.cible_id or "",
            cluster.model_copy(
                update={
                    "version": travail.entree.get("version") or cluster.version,
                    "statut": "running",
                }
            ),
        )


@executeur("k8s.pool.create")
class ExecuteurK8sPoolCreate(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        pool = m.PoolWorkers.model_validate(travail.entree)
        await depot_pool.creer(ctx, pool, parent_id=travail.cible_id, id_=nouvel_id())


@executeur("k8s.pool.roll")
class ExecuteurK8sPoolRoll(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        nom = travail.contexte.get("nom", "")
        nouveau = m.PoolWorkers.model_validate(travail.entree)
        for r in await depot_pool.lignes(ctx, parent_id=travail.cible_id or ""):
            if (r.donnees or {}).get("nom") == nom:
                r.donnees = nouveau.model_dump(mode="json")
                await ctx.session.flush()
                break


@executeur("k8s.pool.delete")
class ExecuteurK8sPoolDelete(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        nom = travail.contexte.get("nom", "")
        for r in await depot_pool.lignes(ctx, parent_id=travail.cible_id or ""):
            if (r.donnees or {}).get("nom") == nom:
                await ctx.session.delete(r)
                await ctx.session.flush()
                break


@executeur("k8s.modules")
class ExecuteurK8sModules(Executeur):
    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        cluster = await depot_cluster.obtenir(ctx, travail.cible_id or "")
        modules = travail.entree.get("modules") or cluster.modules
        await depot_cluster.remplacer(
            ctx,
            travail.cible_id or "",
            cluster.model_copy(update={"modules": modules, "statut": "running"}),
        )
