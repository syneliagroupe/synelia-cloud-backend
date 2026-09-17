"""Charges de travail Kubernetes (namespaces/déploiements) sur le cluster Magnum du PaaS.

Même motif `Simule`/`Reel` que partout ailleurs dans `synelia_openstack`. Le réel ne
construit un `kubernetes.client.ApiClient` qu'à la demande, à partir d'un kubeconfig
assemblé nous-mêmes — jamais de fichier kubeconfig persistant sur disque.

Cette version Magnum (driver `magnum_cluster_api`) n'expose PAS de route
`GET /clusters/{id}/config` (contrairement à ce qu'un commentaire précédent supposait par
analogie avec `openstack coe cluster config` : ce sous-commande construit en réalité le
kubeconfig côté client, elle n'appelle aucune route dédiée côté serveur — vérifié par
inspection du contrôleur Pecan de Magnum, seules `/certificates` et `/clusters/{id}`
existent). On reproduit donc la même mécanique que le client OpenStack officiel :
`GET /clusters/{id}` pour l'adresse de l'API, `GET /certificates/{id}` pour le certificat
de la CA, puis une CSR générée localement signée via `POST /certificates` pour obtenir un
certificat client (`O=system:masters`, autorisé admin par le CA Kubernetes de kubeadm).

Le cluster ciblé est désigné par la variable d'environnement `SYNELIA_PAAS_CLUSTER_ID`
(id du cluster côté Magnum), lue à chaque appel : ce module ne modélise volontairement
qu'un seul cluster PaaS pour cette itération.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from synelia_kernel import erreurs

ENV_CLUSTER_ID = "SYNELIA_PAAS_CLUSTER_ID"
ENV_OBSERVABILITE_NAMESPACE = "SYNELIA_OBSERVABILITE_NAMESPACE"

_GROUPE_VMRULE = "operator.victoriametrics.com"
_VERSION_VMRULE = "v1beta1"
_PLURIEL_VMRULE = "vmrules"

# kubeconfig assemblé (CA + certificat client signé) mis en cache par cluster : chaque appel
# à `_kubeconfig` fait deux allers-retours vers Magnum (`GET`/`POST /certificates`) qui, sur ce
# lab, transitent par une RPC magnum-conductor + Barbican intermittente (cf. `_avec_reprises`).
# Un composant applique généralement plusieurs opérations (namespace, déploiement, service) à la
# suite : sans cache, chacune redemande un tout nouveau certificat client, ce qui multiplie
# inutilement l'exposition à cette lenteur. Le certificat signé reste valide plusieurs années
# côté Magnum, donc le cache vit pour la durée du processus (pas de TTL : un changement de
# cluster désigné en cours de vie du processus impose un redémarrage, comme pour les autres
# connexions mises en cache de `synelia_openstack.fabrique`).
_KUBECONFIG_CACHE: dict[str, dict[str, Any]] = {}


def _avec_reprises[T](appel: Callable[[], T], *, tentatives: int = 3) -> T:
    """Ré-essaie un appel Magnum sujet à des échecs 502/504 intermittents (RPC + Barbican).

    Observé sur ce lab : `GET`/`POST /certificates` (RPC magnum-api -> magnum-conductor,
    puis Barbican) échoue par intermittence après ~60s d'attente, y compris deux appels
    identiques consécutifs sans rien changer côté appelant — puis repasse en ~1s. Ce n'est
    pas propre à un chemin réseau particulier (constaté aussi bien en direct sur l'hôte que
    depuis le conteneur), donc pas quelque chose que cet appelant peut corriger autrement
    qu'en retentant.
    """
    derniere_exception: Exception | None = None
    for tentative in range(1, tentatives + 1):
        try:
            return appel()
        except Exception as exc:  # noqa: BLE001 — relayé si toutes les tentatives échouent
            derniere_exception = exc
            if tentative < tentatives:
                time.sleep(2)
    assert derniere_exception is not None
    raise derniere_exception


def _attendre_disparition(lire: Callable[[], Any], *, attente_s: float = 60.0) -> None:
    """Attend la disparition réelle d'une ressource K8s après un DELETE.

    Même raison que `ComputeOpenStack.supprimer_serveur`/`BlockStorageOpenStack.supprimer` :
    l'API Kubernetes accepte un DELETE avant même que la ressource ait fini de se terminer
    (finalizers, terminaison de pods...) — sans attendre sa disparition effective, l'appelant
    marquerait la suppression « ok » alors que la ressource (et sa charge) survit encore.
    """
    from kubernetes import client as k8s_client

    debut = time.monotonic()
    while time.monotonic() - debut < attente_s:
        try:
            lire()
        except k8s_client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc
        time.sleep(2)
    raise erreurs.amont_indisponible(
        "kubernetes", "La ressource n'a pas disparu dans le délai imparti après suppression."
    )


class K8sWorkloadSimule:
    """Aucun cluster réel : simule namespaces et déploiements, instantanément."""

    def creer_namespace(self, nom: str) -> None:
        return None

    def supprimer_namespace(self, nom: str) -> None:
        return None

    def appliquer_deployment(
        self,
        namespace: str,
        nom: str,
        image: str,
        *,
        replicas: int = 1,
        env: dict[str, str] | None = None,
        ports: list[int] | None = None,
        cpu: float | None = None,
        ram_mo: int | None = None,
    ) -> None:
        return None

    def supprimer_deployment(self, namespace: str, nom: str) -> None:
        return None

    def appliquer_regle_alerte(
        self,
        regle_id: str,
        *,
        groupe: str,
        alerte: str,
        expr: str,
        duree: str,
        labels: dict[str, str],
        annotations: dict[str, str],
    ) -> None:
        return None

    def supprimer_regle_alerte(self, regle_id: str) -> None:
        return None


class K8sWorkloadReel(K8sWorkloadSimule):
    """`kubernetes-client` vers le cluster Magnum désigné par `SYNELIA_PAAS_CLUSTER_ID`."""

    def _cluster_id(self) -> str:
        cluster_id = os.environ.get(ENV_CLUSTER_ID)
        if not cluster_id:
            raise erreurs.amont_indisponible(
                "kubernetes", f"{ENV_CLUSTER_ID} non configuré : aucun cluster PaaS ciblé."
            )
        return cluster_id

    def _kubeconfig(self, cluster_id: str) -> dict[str, Any]:
        if cluster_id not in _KUBECONFIG_CACHE:
            _KUBECONFIG_CACHE[cluster_id] = self._construire_kubeconfig(cluster_id)
        return _KUBECONFIG_CACHE[cluster_id]

    def _construire_kubeconfig(self, cluster_id: str) -> dict[str, Any]:
        import base64

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        from synelia_openstack.erreurs import traduire
        from synelia_openstack.fabrique import connexion

        c = connexion()
        cim = c.container_infrastructure_management
        try:
            cluster = cim.get_cluster(cluster_id)
            # Le certificat CA et la signature de CSR passent par une RPC magnum-api ->
            # magnum-conductor puis par Barbican : sur ce lab, ce chemin échoue par
            # intermittence (504/502 après ~60s, y compris en direct sur l'hôte, hors de tout
            # problème réseau côté appelant) — quelques tentatives suffisent presque toujours
            # à obtenir une réponse rapide.
            ca = _avec_reprises(lambda: cim.get_cluster_certificate(cluster_id))
            adresse_api = cluster.api_address or self._adresse_api_repli(c, cluster)

            cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            csr = (
                x509.CertificateSigningRequestBuilder()
                .subject_name(
                    x509.Name(
                        [
                            x509.NameAttribute(NameOID.COMMON_NAME, "synelia-paas"),
                            # Requis pour que kubeadm/le CA Kubernetes autorise ce certificat
                            # client en tant qu'admin (groupe RBAC `system:masters`).
                            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "system:masters"),
                        ]
                    )
                )
                .sign(cle, hashes.SHA256())
            )
            signe = _avec_reprises(
                lambda: cim.create_cluster_certificate(
                    cluster_uuid=cluster_id,
                    csr=csr.public_bytes(serialization.Encoding.PEM).decode(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — relayé en `424`, pas un `500` opaque
            raise traduire(exc, "cluster PaaS") from None
        cle_pem = cle.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        def b64(texte: str) -> str:
            return base64.b64encode(texte.encode()).decode()

        return {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": cluster_id,
                    "cluster": {
                        "server": adresse_api,
                        "certificate-authority-data": b64(ca.pem),
                    },
                }
            ],
            "users": [
                {
                    "name": "synelia-paas",
                    "user": {
                        "client-certificate-data": b64(signe.pem),
                        "client-key-data": b64(cle_pem),
                    },
                }
            ],
            "contexts": [
                {
                    "name": "defaut",
                    "context": {"cluster": cluster_id, "user": "synelia-paas"},
                }
            ],
            "current-context": "defaut",
        }

    def _adresse_api_repli(self, c: Any, cluster: Any) -> str:
        """`cluster.api_address` de secours : synchronisation Magnum incomplète pour ce cluster.

        Pour un cluster piloté par le driver `magnum_cluster_api` (CAPI), Magnum lui-même
        renseigne `api_address` via sa tâche périodique de synchronisation de statut — sur ce
        lab, cette tâche échoue pour des raisons indépendantes (webhook CAPI/verrou), si bien
        que le champ reste `None` même quand le cluster Kubernetes sous-jacent est parfaitement
        opérationnel. On retrouve la même adresse en interrogeant nous-mêmes le load-balancer
        Octavia du kube-apiserver (nommé par convention CAPI) et sa flottante éventuelle —
        exactement ce que Magnum aurait fini par renseigner.
        """
        nom_lb = f"k8s-clusterapi-cluster-magnum-system-{cluster.stack_id}-kubeapi"
        lb = next((x for x in c.load_balancer.load_balancers() if x.name == nom_lb), None)
        if lb is None:
            raise erreurs.amont_indisponible(
                "kubernetes",
                f"Adresse de l'API introuvable : ni `api_address` Magnum ni load-balancer "
                f"« {nom_lb} ».",
            )
        flottante = next(
            (ip.floating_ip_address for ip in c.network.ips(port_id=lb.vip_port_id)), None
        )
        return f"https://{flottante or lb.vip_address}:6443"

    def _api_client(self) -> Any:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        kubeconfig = self._kubeconfig(self._cluster_id())
        configuration = k8s_client.Configuration()
        k8s_config.load_kube_config_from_dict(
            kubeconfig, client_configuration=configuration, persist_config=False
        )
        return k8s_client.ApiClient(configuration)

    def creer_namespace(self, nom: str) -> None:
        from kubernetes import client as k8s_client

        api = k8s_client.CoreV1Api(self._api_client())
        try:
            api.create_namespace(
                k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=nom))
            )
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 409:  # déjà présent : idempotent
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc

    def supprimer_namespace(self, nom: str) -> None:
        from kubernetes import client as k8s_client

        api = k8s_client.CoreV1Api(self._api_client())
        try:
            api.delete_namespace(nom)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc
            return  # déjà absent avant même la vérification : succès
        # Un namespace passe par `Terminating` (finalizers, purge des objets qu'il contient)
        # avant de disparaître réellement : attendre ici évite de rapporter un succès prématuré.
        _attendre_disparition(lambda: api.read_namespace(nom), attente_s=120.0)

    def appliquer_deployment(
        self,
        namespace: str,
        nom: str,
        image: str,
        *,
        replicas: int = 1,
        env: dict[str, str] | None = None,
        ports: list[int] | None = None,
        cpu: float | None = None,
        ram_mo: int | None = None,
    ) -> None:
        from kubernetes import client as k8s_client

        api_client = self._api_client()
        ports = ports or [8080]
        ressources = None
        if cpu or ram_mo:
            quantites = {
                **({"cpu": str(cpu)} if cpu else {}),
                **({"memory": f"{ram_mo}Mi"} if ram_mo else {}),
            }
            ressources = k8s_client.V1ResourceRequirements(requests=quantites, limits=quantites)
        conteneur = k8s_client.V1Container(
            name=nom,
            image=image,
            env=[k8s_client.V1EnvVar(name=k, value=v) for k, v in (env or {}).items()],
            ports=[k8s_client.V1ContainerPort(container_port=p) for p in ports],
            resources=ressources,
        )
        gabarit = k8s_client.V1PodTemplateSpec(
            metadata=k8s_client.V1ObjectMeta(labels={"app": nom}),
            spec=k8s_client.V1PodSpec(containers=[conteneur]),
        )
        spec = k8s_client.V1DeploymentSpec(
            replicas=replicas,
            selector=k8s_client.V1LabelSelector(match_labels={"app": nom}),
            template=gabarit,
        )
        deployment = k8s_client.V1Deployment(
            metadata=k8s_client.V1ObjectMeta(name=nom, namespace=namespace), spec=spec
        )
        apps = k8s_client.AppsV1Api(api_client)
        try:
            apps.create_namespaced_deployment(namespace, deployment)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status == 409:
                apps.replace_namespaced_deployment(nom, namespace, deployment)
            else:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc

        service = k8s_client.V1Service(
            metadata=k8s_client.V1ObjectMeta(name=nom, namespace=namespace),
            spec=k8s_client.V1ServiceSpec(
                selector={"app": nom},
                type="ClusterIP",
                ports=[
                    k8s_client.V1ServicePort(port=p, target_port=p, name=f"port-{p}")
                    for p in ports
                ],
            ),
        )
        core = k8s_client.CoreV1Api(api_client)
        try:
            core.create_namespaced_service(namespace, service)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status == 409:
                core.replace_namespaced_service(nom, namespace, service)
            else:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc

    def supprimer_deployment(self, namespace: str, nom: str) -> None:
        from kubernetes import client as k8s_client

        api_client = self._api_client()
        apps = k8s_client.AppsV1Api(api_client)
        core = k8s_client.CoreV1Api(api_client)
        try:
            apps.delete_namespaced_deployment(nom, namespace)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc
        else:
            # Le Deployment accepte le DELETE avant que ses ReplicaSets/pods aient fini de
            # se terminer — sans attendre sa disparition, l'appelant marquerait « ok » alors
            # que les pods (donc la charge facturée) tournent encore.
            _attendre_disparition(
                lambda: apps.read_namespaced_deployment(nom, namespace), attente_s=90.0
            )
        try:
            core.delete_namespaced_service(nom, namespace)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc
        else:
            _attendre_disparition(
                lambda: core.read_namespaced_service(nom, namespace), attente_s=30.0
            )

    def _nom_vmrule(self, regle_id: str) -> str:
        import re

        slug = re.sub(r"[^a-z0-9-]", "-", regle_id.lower()).strip("-") or "regle"
        return f"synelia-regle-{slug}"[:253]

    def appliquer_regle_alerte(
        self,
        regle_id: str,
        *,
        groupe: str,
        alerte: str,
        expr: str,
        duree: str,
        labels: dict[str, str],
        annotations: dict[str, str],
    ) -> None:
        """Crée/remplace un `VMRule` (opérateur victoria-metrics-k8s-stack) : `vmalert` le
        sélectionne automatiquement (`selectAllByDefault: true`) sans redéploiement."""
        from kubernetes import client as k8s_client

        namespace = os.environ.get(ENV_OBSERVABILITE_NAMESPACE, "observability")
        nom = self._nom_vmrule(regle_id)
        corps = {
            "apiVersion": f"{_GROUPE_VMRULE}/{_VERSION_VMRULE}",
            "kind": "VMRule",
            "metadata": {
                "name": nom,
                "namespace": namespace,
                "labels": {"synelia.cloud/regle-alerte": regle_id},
            },
            "spec": {
                "groups": [
                    {
                        "name": groupe,
                        "rules": [
                            {
                                "alert": alerte,
                                "expr": expr,
                                "for": duree,
                                "labels": labels,
                                "annotations": annotations,
                            }
                        ],
                    }
                ]
            },
        }
        custom = k8s_client.CustomObjectsApi(self._api_client())
        try:
            custom.create_namespaced_custom_object(
                _GROUPE_VMRULE, _VERSION_VMRULE, namespace, _PLURIEL_VMRULE, corps
            )
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc
            try:
                custom.replace_namespaced_custom_object(
                    _GROUPE_VMRULE, _VERSION_VMRULE, namespace, _PLURIEL_VMRULE, nom, corps
                )
            except k8s_client.exceptions.ApiException as exc2:
                raise erreurs.amont_indisponible("kubernetes", str(exc2)) from exc2

    def supprimer_regle_alerte(self, regle_id: str) -> None:
        from kubernetes import client as k8s_client

        namespace = os.environ.get(ENV_OBSERVABILITE_NAMESPACE, "observability")
        custom = k8s_client.CustomObjectsApi(self._api_client())
        try:
            custom.delete_namespaced_custom_object(
                _GROUPE_VMRULE,
                _VERSION_VMRULE,
                namespace,
                _PLURIEL_VMRULE,
                self._nom_vmrule(regle_id),
            )
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise erreurs.amont_indisponible("kubernetes", str(exc)) from exc


def construire_kubeconfig(cluster_id: str) -> dict[str, Any]:
    """Kubeconfig admin pour un cluster Magnum arbitraire — pas seulement celui désigné par
    `SYNELIA_PAAS_CLUSTER_ID` : réutilisé par le module `kubernetes` pour exposer un vrai
    `GET /kubernetes/{id}/kubeconfig` sur un cluster provisionné par un client, avec la même
    mécanique (CSR signée par Magnum) que celle du cluster PaaS interne."""
    return K8sWorkloadReel()._construire_kubeconfig(cluster_id)  # noqa: SLF001


_SIMULE = K8sWorkloadSimule()


def obtenir() -> K8sWorkloadSimule:
    """Choisit `K8sWorkloadReel` seulement si `SYNELIA_PAAS_CLUSTER_ID` est configuré.

    Contrairement aux amonts purement OpenStack (Magnum, Nova…), ce choix ne dépend
    **pas** du mode `fournisseur` global : sans cette variable, le PaaS reste en
    simulation même quand le reste de la plateforme tourne en réel — pour ne pas
    faire échouer la création de projets/composants tant qu'aucun cluster PaaS
    n'a été désigné.
    """
    if not os.environ.get(ENV_CLUSTER_ID):
        return _SIMULE
    from synelia_openstack.fabrique import fournisseur

    return fournisseur(K8sWorkloadSimule, K8sWorkloadReel)
