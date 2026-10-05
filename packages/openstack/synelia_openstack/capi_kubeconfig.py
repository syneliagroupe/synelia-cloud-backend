"""Kubeconfig admin d'un cluster workload via le k3s de management CAPI (ctrl1).

Repli lorsque Magnum `GET/POST /certificates` est indisponible (Barbican/RPC) : le secret
`{stack_id}-kubeconfig` dans `magnum-system` contient déjà un certificat admin valide.
"""

from __future__ import annotations

from typing import Any

import paramiko
import yaml

from synelia_kernel.config import reglages
from synelia_kernel import erreurs


def kubeconfig_depuis_capí(stack_id: str) -> dict[str, Any]:
    """Lit le kubeconfig admin CAPI pour la stack Heat/Magnum `stack_id` (ex. `kube-pbb4l`)."""
    r = reglages()
    hote = r.capi_management_ssh_host
    mot_de_passe = r.capi_management_ssh_password
    if not hote or not mot_de_passe:
        raise erreurs.amont_indisponible(
            "kubernetes",
            "SYNELIA_CAPI_MANAGEMENT_SSH_HOST/PASSWORD requis pour lire le kubeconfig CAPI.",
        )
    secret = f"{stack_id}-kubeconfig"
    cmd = (
        "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; "
        f"kubectl get secret {secret} -n magnum-system "
        "-o jsonpath='{.data.value}' | base64 -d"
    )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hote,
            username=r.capi_management_ssh_user,
            password=mot_de_passe,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, stderr = client.exec_command(cmd, timeout=60)
        sortie = stdout.read().decode()
        err = stderr.read().decode().strip()
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if code != 0 or not sortie.strip():
        raise erreurs.amont_indisponible(
            "kubernetes",
            f"Impossible de lire {secret} sur le k3s CAPI ({err or 'sortie vide'}).",
        )
    doc = yaml.safe_load(sortie)
    if not isinstance(doc, dict) or doc.get("kind") != "Config":
        raise erreurs.amont_indisponible("kubernetes", "Kubeconfig CAPI illisible.")
    return doc
