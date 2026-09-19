"""Registre unique des capacités amont — source de vérité pour `x-etat` et `synelia capacites`.

Chaque entrée déclare: la paire Simule/Reel, la variable d'environnement gate,
et l'état courant (reel|persiste|simule|maquette). `reel` et `simule` sont ceux
du PLAN-API §1; `persiste` = écrit en base sans amont (ex. SSO), `maquette`
= non branché du tout (UI seule).

Source: /tmp/report-api-track.md §6.2 + §4 inventaire 15 connecteurs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Etat = Literal["reel", "persiste", "simule", "maquette"]


@dataclass(frozen=True)
class Capacite:
    id: str  # ex. "registrar.domain"
    description: str
    module: str  # fichier sans .py: "registrar"
    classe_simule: str  # "RegistrarSimule"
    classe_reel: str | None  # "RegistrarOpenStack" | None si pas de Reel
    env_gate: str | None  # "SYNELIA_REGISTRAR_URL" | None
    etat: Etat
    detail: str | None = None  # ex. "RegistrarOpenStack hérite vide"


REGISTRE: list[Capacite] = [
    # --- Web Cloud domaine / SSL : stubs volontaires ---
    Capacite(
        "registrar.domain",
        "Disponibilité/commande/transfert de domaine",
        "registrar",
        "RegistrarSimule",
        "RegistrarOpenStack",
        "SYNELIA_REGISTRAR_URL",
        "simule",
        "Héritage vide — aucun partenaire câblé",
    ),
    Capacite(
        "acme.certificat",
        "ACME commande/validation/renouvellement",
        "acme",
        "AcmeSimule",
        "AcmeReel",
        "SYNELIA_ACME_URL",
        "simule",
    ),
    # --- Infra cœur : réel en lab ---
    Capacite(
        "compute.serveur",
        "Nova serveurs/gabarits/images/console",
        "compute",
        "ComputeSimule",
        "ComputeOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "block_storage.volume",
        "Cinder volumes/snapshots",
        "block_storage",
        "BlockStorageSimule",
        "BlockStorageOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "network.reseau",
        "Neutron réseau/SG/FIP, Octavia LB",
        "network",
        "NetworkSimule",
        "NetworkOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "identite.tenancy",
        "Keystone domaine/projet/réseau/quota/FIP/app-credential",
        "identite",
        "IdentiteSimule",
        "IdentiteOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "designate.zone",
        "Designate zones/enregistrements",
        "designate",
        "DesignateSimule",
        "DesignateOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "magnum.cluster",
        "Magnum clusters/pools K8s",
        "magnum",
        "MagnumSimule",
        "MagnumOpenStack",
        None,
        "reel",
    ),
    Capacite(
        "ssh.hebergement",
        "SSH paramiko vers VM zone VPS",
        "ssh",
        "SshSimule",
        "SshReel",
        None,
        "reel",
    ),
    # --- Plateforme / Produits ---
    Capacite(
        "minio.objet",
        "MinIO buckets/objets/IAM",
        "minio",
        "MinioSimule",
        "MinioReel",
        "SYNELIA_MINIO_URL",
        "simule",
        "Reel complet mais gate jamais posée dans CI",
    ),
    Capacite(
        "zimbra.messagerie",
        "Zimbra OSE domaines/boîtes/SOAP",
        "zimbra",
        "ZimbraSimule",
        "ZimbraReel",
        "SYNELIA_ZIMBRA_URL",
        "reel",
        "Reel déployé sur vm-admin",
    ),
    Capacite(
        "k8s.workload",
        "K8s namespaces/déploiements (cluster Magnum)",
        "k8s_workload",
        "K8sSimule",
        "K8sReel",
        "SYNELIA_PAAS_CLUSTER_ID",
        "simule",
        "Un seul cluster, absent par défaut",
    ),
    Capacite(
        "argo.application",
        "Argo CD Applications",
        "plateforme_k8s",
        "ArgoSimule",
        "ArgoReel",
        "SYNELIA_ARGOCD_URL",
        "simule",
        "ArgoReel lève amont_indisponible (non impl)",
    ),
    Capacite(
        "relais_smtp.envoi",
        "Relais SMTP envoyer_test",
        "relais_smtp",
        "RelaisSmtpSimule",
        "RelaisSmtpReel",
        "SYNELIA_RELAIS_SMTP_HOTE",
        "reel",
    ),
    Capacite(
        "victoria.observabilite",
        "VictoriaMetrics/Logs séries/logs",
        "victoria",
        "VictoriaSimule",
        "VictoriaReel",
        "SYNELIA_VICTORIAMETRICS_URL",
        "simule",
        "Dégradé en [] sans URL",
    ),
    Capacite(
        "backup.plan",
        "Karbor/Cinder backup restore",
        "backup",
        "BackupSimule",
        "BackupOpenStack",
        None,
        "simule",
        "Partiel: create_plan_run si dispo",
    ),
    # --- Transverse ---
    Capacite(
        "securite.sso",
        "SSO configuration + test",
        "securite",
        "-",
        "-",
        None,
        "persiste",
        "Persistance Organisation.sso JSONB, pas d'appel IdP — test honnête (false)",
    ),
]


def etat_pour(connecteur_id: str) -> Etat | None:
    return next((c.etat for c in REGISTRE if c.id == connecteur_id), None)


def est_reel(connecteur_id: str) -> bool:
    c = next((x for x in REGISTRE if x.id == connecteur_id), None)
    if not c:
        return False
    if c.etat != "reel":
        return False
    if c.env_gate and not os.environ.get(c.env_gate):
        return False
    return True
