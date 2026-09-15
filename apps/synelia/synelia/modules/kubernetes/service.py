from __future__ import annotations

import asyncio
from typing import Any

from synelia_contract import modeles as m
from synelia_db.modeles import Travail
from synelia_kernel.ids import nouvel_id
from synelia_openstack import fournisseur
from synelia_openstack.compute import ComputeOpenStack, ComputeSimule
from synelia_openstack.magnum import MagnumOpenStack, MagnumSimule

from synelia.depot import Depot
from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

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
        return None

    async def terminer(self, ctx: Contexte, travail: Travail) -> None:
        # Le simulé renvoie CREATE_COMPLETE instantanément ; le réel (Heat/CAPI) prend bien
        # plus longtemps qu'une étape de travail, donc on ne bloque pas dessus et le cluster
        # reste `provisioning` côté plateforme jusqu'à ce que `reconcilier_statut` (appelé à
        # chaque lecture, cf. router) confirme CREATE_COMPLETE côté Magnum.
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
