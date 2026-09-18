"""Bootstrap PaaS d'un cluster Kubernetes : registre Zot + opérateurs de bases.

Chaque étape est un index du travail `paas.bootstrap` : elle apparaît dans le
centre de tâches (`/app/taches`), est suivie par Temporal (workflow
`TravailWorkflow` → activité `executer_etapes`) et rejouable individuellement.

Deux mécanismes :
- **Helm** pour les charts (Zot, opérateurs) — binaire embarqué dans l'image,
  exécuté dans un sous-processus avec un `KUBECONFIG` temporaire construit
  depuis Magnum (`construire_kubeconfig`), jamais persisté sur disque.
- **client k8s Python** pour le DaemonSet de confiance containerd (pas besoin de
  `kubectl`), idempotent (create ou replace).

Référence : `tools/paas-bootstrap.sh` + `docs/runbooks/paas-bootstrap.md` (mêmes
étapes, mêmes pièges). Ce module en est la version pilotée par la plateforme.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from synelia_kernel import erreurs

from synelia.deps.contexte import Contexte
from synelia.travaux import Executeur, executeur

# Namespaces et noms fixes (cf. runbook).
NS_REGISTRE = "registry"
NS_CNPG = "cnpg-system"
NS_MARIADB = "mariadb-system"
NS_REDIS = "redis-operator"
NS_ELASTIC = "elastic-system"
NS_PSMDB = "psmdb-system"
STORAGE_CLASS = "block-ssd"
TRUST_DS = "registry-trust"

# (nom affiché, durée indicative) — l'index est l'étape exécutée.
ETAPES: list[tuple[str, int]] = [
    ("Installer le registre Zot", 120),
    ("Marquer la classe de stockage par défaut", 5),
    ("Configurer la confiance containerd des nœuds", 60),
    ("Installer l'opérateur CloudNativePG", 90),
    ("Installer l'opérateur MariaDB", 90),
    ("Installer l'opérateur Redis", 60),
    ("Installer l'opérateur Elastic (ECK)", 90),
    ("Installer l'opérateur MongoDB (Percona)", 90),
    ("Vérifier les opérateurs", 10),
]


def _env_helm(kubeconfig: str) -> dict[str, str]:
    # Homed under the per-step kubeconfig's own unique temp path (never a
    # fixed /tmp/helm/* — this executor runs as a Temporal-backed travail:
    # retries and concurrent bootstraps of different clusters are the normal
    # case, not an edge case, so a shared cache dir would let one run's helm
    # invocation race or corrupt another's.
    base = f"{kubeconfig}.helm"
    env = dict(os.environ)
    env.update(
        {
            "HELM_CACHE_HOME": f"{base}/cache",
            "HELM_CONFIG_HOME": f"{base}/config",
            "HELM_DATA_HOME": f"{base}/data",
        }
    )
    return env


def _helm(*args: str, kubeconfig: str, check: bool = True) -> subprocess.CompletedProcess:
    """Exécute `helm` avec le kubeconfig temporaire. Synchrone (appelé via to_thread)."""
    base = f"{kubeconfig}.helm"
    for d in (f"{base}/cache", f"{base}/config", f"{base}/data"):
        Path(d).mkdir(parents=True, exist_ok=True)
    cmd = ["helm", "--kubeconfig", kubeconfig, *args]
    res = subprocess.run(
        cmd, capture_output=True, text=True, env=_env_helm(kubeconfig), check=False
    )
    if check and res.returncode != 0:
        raise erreurs.amont_indisponible("helm", f"{' '.join(args)} → {res.stderr.strip()[:500]}")
    return res


def _helm_repo_add(nom: str, url: str, kubeconfig: str) -> None:
    _helm("repo", "add", nom, url, kubeconfig=kubeconfig, check=False)


def _kubeconfig(magnum_cluster_id: str) -> tuple[str, dict[str, Any]]:
    """Écrit un kubeconfig temporaire du cluster PaaS et renvoie (chemin, config)."""
    import yaml
    from synelia_openstack.k8s_workload import construire_kubeconfig

    kc = construire_kubeconfig(magnum_cluster_id)
    fd, chemin = tempfile.mkstemp(prefix="synelia-paas-", suffix=".kubeconfig")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(kc, f)
    os.chmod(chemin, 0o600)
    return chemin, kc


def _api_client(kubeconfig: dict[str, Any]) -> Any:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    configuration = k8s_client.Configuration()
    k8s_config.load_kube_config_from_dict(
        kubeconfig, client_configuration=configuration, persist_config=False
    )
    return k8s_client.ApiClient(configuration)


def _apply_object(api_client: Any, corps: dict[str, Any]) -> None:
    """Crée ou remplace un objet k8s générique (idempotent)."""
    from kubernetes import client as k8s_client

    kind = corps["kind"]
    ns = corps["metadata"].get("namespace")
    nom = corps["metadata"]["name"]
    api = k8s_client.CustomObjectsApi(api_client)
    group = corps["apiVersion"].split("/")[0]
    version = corps["apiVersion"].split("/")[1]
    plural = {
        "DaemonSet": "daemonsets",
    }.get(kind, kind.lower() + "s")
    if kind == "DaemonSet":
        apps = k8s_client.AppsV1Api(api_client)
        try:
            apps.create_namespaced_daemon_set(ns, corps)
        except k8s_client.exceptions.ApiException as exc:
            if exc.status != 409:
                raise
            apps.replace_namespaced_daemon_set(nom, ns, corps)
        return
    try:
        api.create_namespaced_custom_object(group, version, ns, plural, corps)
    except k8s_client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise
        api.replace_namespaced_custom_object(group, version, ns, plural, nom, corps)


def _registre_adresse(api_client: Any) -> str:
    from kubernetes import client as k8s_client

    core = k8s_client.CoreV1Api(api_client)
    svc = core.read_namespaced_service("zot", NS_REGISTRE)
    ingress = (svc.status.load_balancer.ingress or [None])[0]
    if ingress is None or not ingress.ip:
        raise erreurs.amont_indisponible("octavia", "Zot n'a pas obtenu de VIP.")
    return f"{ingress.ip}:5000"


# ─── Étapes ──────────────────────────────────────────────────────────────────


def _etape_zot(kc: str) -> str:
    _helm_repo_add("cnpg", "https://cloudnative-pg.github.io/charts", kc)
    values = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    values.write(
        "persistence: true\n"
        "pvc:\n"
        f"  storage: 8Gi\n  storageClassName: {STORAGE_CLASS}\n"
        "service:\n  type: LoadBalancer\n  port: 5000\n"
    )
    values.close()
    _helm(
        "upgrade",
        "--install",
        "zot",
        "oci://ghcr.io/project-zot/helm-charts/zot",
        "-n",
        NS_REGISTRE,
        "--create-namespace",
        "-f",
        values.name,
        "--timeout",
        "8m",
        kubeconfig=kc,
    )
    return "Registre Zot installé (chart oci://ghcr.io/project-zot/helm-charts/zot)."


def _etape_storageclass(kc: str, api_client: Any) -> str:
    from kubernetes import client as k8s_client

    storage = k8s_client.StorageV1Api(api_client)
    corps = {
        "metadata": {
            "name": STORAGE_CLASS,
            "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
        }
    }
    storage.patch_storage_class(STORAGE_CLASS, corps)
    return f"Classe {STORAGE_CLASS} marquée par défaut."


def _etape_confiance(kc: str, api_client: Any) -> str:
    adresse = _registre_adresse(api_client)
    script = (
        "set -e\n"
        f'HOST="{adresse}"\n'
        'DIR="/host/etc/containerd/certs.d/${HOST}"\n'
        'mkdir -p "$DIR"\n'
        'printf \'server = "http://%s"\\n\\n[host."http://%s"]\\n  capabilities = '
        '["pull", "resolve", "push"]\\n\' "$HOST" "$HOST" > "$DIR/hosts.toml"\n'
        'if ! grep -q "io.containerd.cri.v1.images\'.registry" '
        "/host/etc/containerd/config.toml; then\n"
        "  printf \"\\n[plugins.'io.containerd.cri.v1.images'.registry]\\n"
        "  config_path = '/etc/containerd/certs.d'\\n\" "
        ">> /host/etc/containerd/config.toml\n"
        "fi\n"
        "if [ ! -f /host/tmp/.registry-trust-done ]; then\n"
        "  nsenter -t 1 -m -u -i -n -p -- systemctl restart containerd || true\n"
        "  touch /host/tmp/.registry-trust-done\n"
        "fi\n"
        "sleep infinity\n"
    )
    _apply_object(
        api_client,
        {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": TRUST_DS, "namespace": NS_REGISTRE, "labels": {"app": TRUST_DS}},
            "spec": {
                "selector": {"matchLabels": {"app": TRUST_DS}},
                "template": {
                    "metadata": {"labels": {"app": TRUST_DS}},
                    "spec": {
                        "hostPID": True,
                        "tolerations": [{"operator": "Exists"}],
                        "containers": [
                            {
                                "name": "trust",
                                "image": "alpine:3.20",
                                "securityContext": {"privileged": True},
                                "command": ["/bin/sh", "-c"],
                                "args": [script],
                                "volumeMounts": [
                                    {
                                        "name": "host",
                                        "mountPath": "/host",
                                        "mountPropagation": "Bidirectional",
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
                    },
                },
            },
        },
    )
    return f"Confiance containerd configurée pour {adresse}."


def _installer_operateur(
    kc: str, *, release: str, chart: str, namespace: str, extra: list[str] | None = None
) -> None:
    args = [
        "upgrade",
        "--install",
        release,
        chart,
        "-n",
        namespace,
        "--create-namespace",
        "--wait",
        "--timeout",
        "8m",
    ]
    if extra:
        args += extra
    _helm(*args, kubeconfig=kc)


def _etape_operateur(nom: str, kc: str) -> str:
    if nom == "cnpg":
        _helm_repo_add("cnpg", "https://cloudnative-pg.github.io/charts", kc)
        _installer_operateur(kc, release="cnpg", chart="cnpg/cloudnative-pg", namespace=NS_CNPG)
    elif nom == "mariadb":
        _helm_repo_add(
            "mariadb-operator", "https://mariadb-operator.github.io/mariadb-operator", kc
        )
        _installer_operateur(
            kc,
            release="mariadb-operator-crds",
            chart="mariadb-operator/mariadb-operator-crds",
            namespace=NS_MARIADB,
        )
        _installer_operateur(
            kc,
            release="mariadb-operator",
            chart="mariadb-operator/mariadb-operator",
            namespace=NS_MARIADB,
        )
    elif nom == "redis":
        _helm_repo_add("ot-helm", "https://ot-container-kit.github.io/helm-charts/", kc)
        _installer_operateur(
            kc, release="redis-operator", chart="ot-helm/redis-operator", namespace=NS_REDIS
        )
    elif nom == "eck":
        _helm_repo_add("elastic", "https://helm.elastic.co", kc)
        _installer_operateur(
            kc,
            release="eck-operator",
            chart="elastic/eck-operator",
            namespace=NS_ELASTIC,
            extra=["--skip-crds"],
        )
    elif nom == "psmdb":
        _helm_repo_add("percona", "https://percona.github.io/percona-helm-charts", kc)
        _installer_operateur(
            kc,
            release="psmdb-operator",
            chart="percona/psmdb-operator",
            namespace=NS_PSMDB,
            extra=["--set", "watchAllNamespaces=true"],
        )
    else:  # pragma: no cover - garde-fou
        raise ValueError(f"opérateur inconnu: {nom}")
    return f"Opérateur {nom} installé."


def _etape_verifier(api_client: Any) -> str:
    from kubernetes import client as k8s_client

    core = k8s_client.CoreV1Api(api_client)
    attendus = {
        NS_CNPG: "cnpg-cloudnative-pg",
        NS_MARIADB: "mariadb-operator",
        NS_REDIS: "redis-operator",
        NS_ELASTIC: "elastic-operator",
        NS_PSMDB: "psmdb-operator",
    }
    prets = []
    for ns, prefixe in attendus.items():
        pods = core.list_namespaced_pod(ns).items
        if any(p.metadata.name.startswith(prefixe) and p.status.phase == "Running" for p in pods):
            prets.append(ns)
    return f"Opérateurs actifs : {', '.join(prets) or 'aucun'}."


def _executer_etape(index: int, kc: str, api_client: Any) -> str:
    if index == 0:
        return _etape_zot(kc)
    if index == 1:
        return _etape_storageclass(kc, api_client)
    if index == 2:
        return _etape_confiance(kc, api_client)
    if 3 <= index <= 7:
        return _etape_operateur(["cnpg", "mariadb", "redis", "eck", "psmdb"][index - 3], kc)
    if index == 8:
        return _etape_verifier(api_client)
    raise ValueError(f"étape {index} inconnue")


@executeur("paas.bootstrap")
class ExecuteurPaasBootstrap(Executeur):
    """Étapes de bootstrap PaaS suivies par Temporal (une étape = un index)."""

    async def etape(self, ctx: Contexte, travail: Any, index: int, nom: str) -> str | None:
        magnum_id = (travail.contexte or {}).get("magnum_cluster_id")
        if not magnum_id:
            raise erreurs.validation("Cluster PaaS cible inconnu (magnum_cluster_id absent).")

        def _run() -> str:
            chemin, kc = _kubeconfig(magnum_id)
            try:
                api_client = _api_client(kc)
                return _executer_etape(index, chemin, api_client)
            finally:
                try:
                    os.unlink(chemin)
                except OSError:
                    pass
                shutil.rmtree(f"{chemin}.helm", ignore_errors=True)

        # helm/k8s : appels synchrones (sous-processus, réseau) → hors boucle asyncio.
        return await asyncio.to_thread(_run)

    async def terminer(self, ctx: Contexte, travail: Any) -> None:
        from synelia.modules.kubernetes import service as s

        cluster = await s.depot_cluster.obtenir(ctx, travail.cible_id or "")
        await s.depot_cluster.remplacer(
            ctx, travail.cible_id or "", cluster.model_copy(update={"statut": "running"})
        )
