"""Magnum : clusters et pools Kubernetes."""

from __future__ import annotations

import os
from typing import Any

from synelia_kernel.ids import nouvel_id


class MagnumSimule:
    def creer_cluster(self, **kw: Any) -> dict[str, Any]:
        return {"id": f"k8s-{nouvel_id()[:8]}", "statut": "CREATE_COMPLETE"}

    def creer_pool(self, cluster_id: str, **kw: Any) -> None:
        return None

    def modifier_pool(self, cluster_id: str, pool_nom: str, **kw: Any) -> None:
        return None

    def supprimer_pool(self, cluster_id: str, pool_nom: str) -> None:
        return None

    def monter_version(self, cluster_id: str, version: str) -> None:
        return None

    def installer_modules(self, cluster_id: str, modules: list[str]) -> None:
        return None

    def activer_reparateur(self, cluster_id: str) -> None:
        return None

    def supprimer_cluster(self, cluster_id: str) -> None:
        return None

    def cluster_statut(self, cluster_id: str) -> str:
        return "CREATE_COMPLETE"

    def cluster_nodes(self, cluster_id: str) -> list[dict[str, Any]]:
        return []


class MagnumOpenStack(MagnumSimule):
    def _c(self):
        from synelia_openstack.fabrique import connexion

        return connexion()

    def _modele_tpl(self, c) -> Any:
        """Un seul modèle public existe sur ce lab (`k8s-capi`) ; on prend le premier."""
        tpl = next(iter(c.container_infra.cluster_templates()), None)
        if tpl is None:
            raise RuntimeError("Aucun modèle de cluster Magnum disponible.")
        return tpl

    def creer_cluster(self, **kw: Any) -> dict[str, Any]:
        c = self._c()
        tpl = None
        modele_id = kw.get("modele_tpl")
        if modele_id:
            tpl = c.container_infra.get_cluster_template(modele_id)
        else:
            tpl = self._modele_tpl(c)
            modele_id = tpl.id
        # Le driver CAPI lit `kube_tag` dans les labels du CLUSTER (avec une valeur
        # par défaut obsolète s'ils sont absents) : on hérite ceux du modèle, qui
        # suivent la version Kubernetes réellement bakée dans l'image (`v1.33.12`).
        etiquettes = dict(getattr(tpl, "labels", None) or {})
        # Provider Octavia réellement activé sur le cloud cible. Le driver CAPI
        # (`magnum_cluster_api/utils.py`) défaut à `amphorav2` — provider VEXXHOST
        # absent d'un Octavia vanilla : chaque `Service type=LoadBalancer` échoue
        # alors en 400 « Provider 'amphorav2' is not enabled » (constaté en direct
        # sur ce lab, où seul `amphora` est activé). Surchargeable par label de
        # modèle, ou par `SYNELIA_PAAS_OCTAVIA_PROVIDER`.
        etiquettes.setdefault(
            "octavia_provider", os.environ.get("SYNELIA_PAAS_OCTAVIA_PROVIDER", "amphora")
        )
        attrs: dict[str, Any] = {
            "name": kw["nom"],
            "cluster_template_id": modele_id,
            "master_count": kw.get("master_count", 1),
            "node_count": max(1, sum(p.get("nodes", 0) for p in kw.get("pools", []))),
            "create_timeout": 60,
            "labels": etiquettes,
        }
        # Le modèle public fixe un réseau par défaut (`demo-net`) : on le remplace par le
        # réseau réel de l'Espace Cloud cible, sinon le cluster atterrit sur le mauvais réseau.
        if kw.get("reseau_id"):
            attrs["fixed_network"] = kw["reseau_id"]
        if kw.get("cle_ssh"):
            attrs["keypair"] = kw["cle_ssh"]
        cl = c.container_infra.create_cluster(**attrs)
        return {"id": cl.id, "statut": cl.status}

    def creer_pool(self, cluster_id: str, **kw: Any) -> None:
        self._c().container_infra.create_nodegroup(
            cluster_id, name=kw["nom"], flavor=kw.get("flavor"), node_count=kw.get("nodes", 1)
        )

    def modifier_pool(self, cluster_id: str, pool_nom: str, **kw: Any) -> None:
        self._c().container_infra.update_nodegroup(cluster_id, pool_nom, dict(kw))

    def supprimer_pool(self, cluster_id: str, pool_nom: str) -> None:
        self._c().container_infra.delete_nodegroup(cluster_id, pool_nom, ignore_missing=True)

    def monter_version(self, cluster_id: str, version: str) -> None:
        self._c().container_infra.update_cluster(cluster_id, version=version)

    def installer_modules(self, cluster_id: str, modules: list[str]) -> None:
        return None

    def activer_reparateur(self, cluster_id: str) -> None:
        return None

    def supprimer_cluster(self, cluster_id: str) -> None:
        self._c().container_infra.delete_cluster(cluster_id, ignore_missing=True)

    def cluster_statut(self, cluster_id: str) -> str:
        """Statut Magnum réel du cluster (`CREATE_COMPLETE`, `CREATE_FAILED`,
        `UPDATE_IN_PROGRESS`…) — `DELETE_COMPLETE` si Magnum ne le connaît plus du tout
        (supprimé hors bande) : même garde que `ComputeOpenStack.statut_serveur` pour Nova."""
        c = self._c()
        cl = c.container_infra.find_cluster(cluster_id, ignore_missing=True)
        return str(cl.status) if cl else "DELETE_COMPLETE"

    def cluster_nodes(self, cluster_id: str) -> list[dict[str, Any]]:
        """VM Nova réelles (masters + workers) derrière un cluster — retrouvées par la
        convention de nommage du driver CAPI (`<stack_id>-...`), pas par
        `node_addresses`/`master_addresses` : ces deux champs restent vides tant que la tâche
        périodique de synchronisation Magnum n'a pas abouti, y compris pour un cluster dont les
        VM tournent déjà réellement (même contournement que `_adresse_api_repli` dans
        `k8s_workload.py`, vérifié en direct sur ce lab). `[]` si le cluster est introuvable ou
        n'a pas encore de stack Heat (cluster tout juste soumis, avant que Magnum n'ait
        provisionné la moindre VM)."""
        c = self._c()
        cl = c.container_infra.find_cluster(cluster_id, ignore_missing=True)
        if cl is None or not cl.stack_id:
            return []
        prefixe = f"{cl.stack_id}-"
        noeuds = []
        for s in c.compute.servers(details=True):
            if not s.name or not s.name.startswith(prefixe):
                continue
            flavor = s.flavor
            vcpu = getattr(flavor, "vcpus", None)
            if vcpu is None and isinstance(flavor, dict):
                vcpu = flavor.get("vcpus")
            noeuds.append({"id": s.id, "vcpu": int(vcpu or 0), "statut": str(s.status)})
        return noeuds
